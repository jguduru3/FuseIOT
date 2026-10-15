"""
FuseIoT HTTP server: REST API, installable web app, health checks and metrics.

Endpoints (roles: viewer < operator < admin)
--------------------------------------------
Public:
    GET  /  /static/*  /sw.js            web app (PWA)
    GET  /health  /ready  /metrics       monitoring
    GET  /api/auth/status                whether login is required
    POST /api/auth/login                 {username, password} -> session token

viewer:
    GET  /api/auth/me                    who am I
    POST /api/auth/logout                end this session
    GET  /api/devices[/{id}[/state|/history]]   (``?fresh=1`` polls the device)
    GET  /api/stats  /api/rules  /api/schedules
    GET  /api/events                     Server-Sent Events (state + online/offline)
    GET|POST|DELETE /api/tokens[/{id}]   own API tokens;  POST /api/me/password
operator:
    POST /api/devices/{id}/commands/{command}
    POST /api/schedules/{name}/run
admin:
    POST /api/rules/{name}/enable|disable   POST /api/schedules/{name}/enable|disable
    GET  /api/notifications                 POST /api/notifications/test {channel}
    GET|POST /api/users   POST|DELETE /api/users/{name}
"""
import json
import queue
import threading
import time
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, unquote, urlparse

from ..auth import Authenticator, Principal, UserStore
from ..exceptions import (
    AuthenticationError,
    AuthorizationError,
    ConfigurationError,
    FuseIoTError,
    StateError,
)
from ..logging_config import get_logger
from ..result import CommandResult, CommandStatus

logger = get_logger("server")

MAX_BODY_BYTES = 64 * 1024
_DASHBOARD_CSP = (
    "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; connect-src 'self'; manifest-src 'self'; worker-src 'self'; "
    "frame-ancestors 'none'"
)
_LOOPBACK = ("127.0.0.1", "localhost", "::1")
_STATIC_TYPES = {
    ".js": "text/javascript; charset=utf-8",
    ".webmanifest": "application/manifest+json",
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json",
}

_STATUS_FOR_RESULT = {
    CommandStatus.CONFIRMED: 200,
    CommandStatus.ACKNOWLEDGED: 200,
    CommandStatus.TIMEOUT: 504,
    CommandStatus.FAILED: 502,
    CommandStatus.RATE_LIMITED: 429,
    CommandStatus.CIRCUIT_OPEN: 503,
}


class ApiError(Exception):
    def __init__(self, status: int, message: str, **details: Any):
        super().__init__(message)
        self.status = status
        self.message = message
        self.details = details


def _json_default(value: Any) -> Any:
    if hasattr(value, "to_dict"):
        return value.to_dict()
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if hasattr(value, "value"):
        return value.value
    return str(value)


def _asset(path: str) -> Optional[bytes]:
    """Read a packaged dashboard asset; None if missing or the path is unsafe."""
    parts = [p for p in path.split("/") if p]
    if not parts or any(p in (".", "..") or "\\" in p for p in parts):
        return None
    try:
        node = resources.files("fuseiot.dashboard")
        for part in parts:
            node = node.joinpath(part)
        return node.read_bytes() if node.is_file() else None
    except (FileNotFoundError, ModuleNotFoundError, OSError):
        return None


def _dashboard_html() -> bytes:
    return _asset("templates/index.html") or b"<h1>FuseIoT</h1><p>Dashboard asset missing.</p>"


class FuseIoTServer:
    """
    Serve a :class:`~fuseiot.hub.Hub` over HTTP.

    Args:
        hub: Hub with devices registered.
        host: Bind address. Non-loopback addresses require authentication
            (``api_token`` or at least one user account) unless ``allow_insecure``.
        port: TCP port (0 picks a free port).
        api_token: Service token with admin rights (``Authorization: Bearer``).
        users: :class:`~fuseiot.auth.UserStore` enabling logins and roles.
        rule_engine / scheduler / monitor / notifier: optional automation parts to expose.
    """

    def __init__(
        self,
        hub: Any,
        host: str = "127.0.0.1",
        port: int = 8080,
        api_token: Optional[str] = None,
        rule_engine: Any = None,
        allow_insecure: bool = False,
        enable_dashboard: bool = True,
        users: Optional[UserStore] = None,
        scheduler: Any = None,
        monitor: Any = None,
        notifier: Any = None,
    ):
        self.auth = Authenticator(users=users, service_token=api_token)
        if host not in _LOOPBACK and not self.auth.enabled and not allow_insecure:
            raise ConfigurationError(
                f"Refusing to serve the device API on {host} without authentication. "
                "Set FUSEIOT_API_TOKEN (or api_token), create a user with 'fuseiot user add', "
                "bind to 127.0.0.1, or pass allow_insecure=True / --insecure if the network is trusted."
            )
        self.hub = hub
        self.api_token = api_token
        self.users = users
        self.rule_engine = rule_engine
        self.scheduler = scheduler
        self.monitor = monitor
        self.notifier = notifier if notifier is not None else getattr(hub, "notifier", None)
        self.enable_dashboard = enable_dashboard
        self.ready = False
        self.started_at = time.time()
        self._dashboard = _dashboard_html() if enable_dashboard else b""
        self._subscribers: List[queue.Queue[Optional[str]]] = []
        self._subs_lock = threading.Lock()
        self._unsubscribe = None
        if getattr(hub, "event_bus", None) is not None:
            self._unsubscribe = hub.event_bus.subscribe_all(self._broadcast)
        if monitor is not None and hasattr(monitor, "listeners"):
            monitor.listeners.append(self._broadcast_status)
        self.metrics = hub.metrics
        self.metrics.describe("fuseiot_commands_total", "Device commands by device, command and status")
        self.metrics.describe("fuseiot_command_latency_ms", "Command latency including confirmation")
        self.metrics.describe("fuseiot_devices", "Registered devices")
        self.metrics.describe("fuseiot_devices_connected", "Devices whose transport is connected")
        self.metrics.describe("fuseiot_http_requests_total", "API requests by route and status")

        self._httpd = ThreadingHTTPServer((host, port), self._make_handler())
        self._httpd.daemon_threads = True
        self._thread: Optional[threading.Thread] = None
        self._serving = False

    # ------------------------------------------------------------- lifecycle

    @property
    def host(self) -> str:
        return str(self._httpd.server_address[0])

    @property
    def port(self) -> int:
        return int(self._httpd.server_address[1])

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def start(self, poll_interval: float = 0.5) -> "FuseIoTServer":
        """Serve in a background thread (``poll_interval`` bounds shutdown latency)."""
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True, name="fuseiot-http",
                                        kwargs={"poll_interval": poll_interval})
        self._serving = True
        self._thread.start()
        self.ready = True
        logger.info("server_started", url=self.url, auth=self.auth.enabled)
        return self

    def serve_forever(self) -> None:
        self._serving = True
        self.ready = True
        logger.info("server_started", url=self.url, auth=self.auth.enabled)
        self._httpd.serve_forever()

    def stop(self) -> None:
        self.ready = False
        with self._subs_lock:
            for q in self._subscribers:
                q.put(None)
        if self._unsubscribe:
            self._unsubscribe()
        if self.monitor is not None and self._broadcast_status in getattr(self.monitor, "listeners", []):
            self.monitor.listeners.remove(self._broadcast_status)
        if self._serving:
            self._httpd.shutdown()  # only valid while serve_forever() is running
            self._serving = False
        self._httpd.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
        logger.info("server_stopped")

    def __enter__(self) -> "FuseIoTServer":
        return self.start()

    def __exit__(self, *exc: Any) -> None:
        self.stop()

    # ---------------------------------------------------------------- events

    def _push(self, event: str, data: str) -> None:
        with self._subs_lock:
            for q in self._subscribers:
                try:
                    q.put_nowait(f"event: {event}\ndata: {data}\n\n")
                except queue.Full:
                    pass

    def _broadcast(self, event: Any) -> None:
        self._push("state", json.dumps(event.to_dict(), default=_json_default))

    def _broadcast_status(self, device_id: str, status: Dict[str, Any]) -> None:
        self._push("status", json.dumps({"device_id": device_id, "status": status}, default=_json_default))

    # ------------------------------------------------------------- handlers

    def _device_online(self, device: Any) -> bool:
        if self.monitor is not None:
            status = self.monitor.get(device.id)
            if status is not None and status.online is not None:
                return bool(status.online)
        return bool(device.protocol.is_connected)

    def health(self) -> Tuple[int, Dict[str, Any]]:
        devices = self.hub.devices()
        offline = [d.id for d in devices if not self._device_online(d)]
        connected = len(devices) - len(offline)
        if not devices or not offline:
            status, code = "healthy", 200
        elif connected == 0:
            status, code = "unhealthy", 503
        else:
            status, code = "degraded", 200
        return code, {
            "status": status,
            "devices": len(devices),
            "devices_connected": connected,
            "disconnected": offline,
            "offline_devices": offline,
            "uptime_seconds": round(time.time() - self.started_at, 1),
            "version": self.hub.stats()["version"],
        }

    def prometheus(self) -> str:
        devices = self.hub.devices()
        self.metrics.gauge("fuseiot_devices", len(devices))
        up = {d.id: self._device_online(d) for d in devices}
        self.metrics.gauge("fuseiot_devices_connected", sum(up.values()))
        for device_id, online in up.items():
            self.metrics.gauge("fuseiot_device_up", 1 if online else 0, {"device": device_id})
        return self.metrics.export_prometheus()

    def device_info(self, device: Any, fresh: bool = False) -> Dict[str, Any]:
        info = device.describe()
        try:
            info["state"] = device.read_state(fresh=True) if fresh else device.last_known_state()
            info["state_error"] = None
        except StateError as e:
            info["state"] = device.last_known_state()
            info["state_error"] = e.message
        info["metrics"] = device.get_metrics()
        breaker = device.config.circuit_breaker
        info["circuit"] = breaker.get_stats() if breaker is not None else None
        status = self.monitor.get(device.id) if self.monitor is not None else None
        info["status"] = status.to_dict() if status is not None else None
        return info

    def run_command(self, device: Any, command: str, params: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
        if command not in device.commands:
            raise ApiError(400, f"Unknown command {command!r} for {device.category}",
                           allowed=list(device.commands))
        method = getattr(device, command)
        try:
            result = method(**params)
        except TypeError as e:
            raise ApiError(400, f"Invalid parameters for {command}: {e}") from e
        except (ValueError, FuseIoTError) as e:
            raise ApiError(400, str(e)) from e
        if not isinstance(result, CommandResult):  # pragma: no cover - defensive
            return 200, {"result": result}
        if not result.success and self.notifier is not None:
            self.notifier.alert("command_failed", title=f"{device.config.name or device.id}: {command} failed",
                                message=str(result.error), key=f"{device.id}:{command}")
        return _STATUS_FOR_RESULT.get(result.status, 500), result.to_dict()

    def schedules_info(self) -> Dict[str, Any]:
        if self.scheduler is None:
            return {"schedules": [], "location": None, "sun": None}
        loc = self.scheduler.location
        sun = None
        if loc.has_coordinates:
            from ..automation.sun import sunrise, sunset

            today = loc.now().date()
            rise = sunrise(today, float(loc.latitude), float(loc.longitude), loc.tz)
            fall = sunset(today, float(loc.latitude), float(loc.longitude), loc.tz)
            sun = {"date": date.isoformat(today), "sunrise": rise.isoformat() if rise else None,
                   "sunset": fall.isoformat() if fall else None}
        return {
            "schedules": self.scheduler.list_schedules(),
            "location": {"timezone": str(loc.tz), "latitude": loc.latitude, "longitude": loc.longitude},
            "sun": sun,
        }

    # ------------------------------------------------------------ http layer

    def _make_handler(self) -> type:
        server = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            server_version = "FuseIoT"
            sys_version = ""

            def log_message(self, format: str, *args: Any) -> None:
                return  # structured access logging happens in _route

            # ---------------------------------------------------- helpers
            def _send(self, status: int, body: bytes, content_type: str,
                      headers: Optional[Dict[str, str]] = None) -> None:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Referrer-Policy", "no-referrer")
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)

            def _json(self, status: int, payload: Any) -> None:
                self._send(status, json.dumps(payload, default=_json_default).encode("utf-8"),
                           "application/json")

            def _bearer(self, query: Dict[str, List[str]], allow_query_token: bool) -> str:
                header = self.headers.get("Authorization", "")
                supplied = header[7:].strip() if header.startswith("Bearer ") else ""
                if not supplied and allow_query_token:
                    # Only for /api/events: EventSource cannot set headers. Elsewhere a
                    # token in the URL would leak into proxy and access logs.
                    supplied = (query.get("token") or [""])[0]
                return supplied

            def _principal(self, query: Dict[str, List[str]], allow_query_token: bool = False) -> Principal:
                try:
                    return server.auth.resolve(self._bearer(query, allow_query_token))
                except AuthenticationError:
                    raise ApiError(401, "Missing or invalid API token") from None

            def _body(self) -> Dict[str, Any]:
                length = int(self.headers.get("Content-Length") or 0)
                if length > MAX_BODY_BYTES:
                    raise ApiError(413, "Request body too large")
                if not length:
                    return {}
                try:
                    data = json.loads(self.rfile.read(length))
                except json.JSONDecodeError as e:
                    raise ApiError(400, f"Invalid JSON body: {e}") from e
                if not isinstance(data, dict):
                    raise ApiError(400, "JSON body must be an object of parameters")
                return data

            def _device(self, device_id: str) -> Any:
                if device_id not in server.hub:
                    raise ApiError(404, f"Device not found: {device_id}")
                return server.hub[device_id]

            @staticmethod
            def _require(principal: Principal, role: str) -> None:
                if not principal.can(role):
                    raise ApiError(403, f"This action needs the {role} role (you are {principal.role})")

            def _users(self) -> UserStore:
                if server.users is None:
                    raise ApiError(404, "User accounts are not enabled (set users_db in the config)")
                return server.users

            # --------------------------------------------------- routing
            def _route(self, method: str) -> None:
                start = time.monotonic()
                parsed = urlparse(self.path)
                query = parse_qs(parsed.query)
                parts = [unquote(p) for p in parsed.path.strip("/").split("/") if p]
                route = "/" + "/".join(parts[:2]) if parts else "/"
                status = 500
                try:
                    status = self._dispatch(method, parts, query)
                except ApiError as e:
                    status = e.status
                    self._json(e.status, {"error": e.message, **e.details})
                except Exception as e:  # never leak a stack trace to clients
                    logger.exception("api_unhandled_error", path=parsed.path, error=str(e))
                    status = 500
                    self._json(500, {"error": "Internal server error"})
                finally:
                    server.metrics.increment("fuseiot_http_requests_total",
                                             labels={"route": route, "method": method, "status": str(status)})
                    logger.info("http_request", method=method, path=parsed.path, status=status,
                                ms=round((time.monotonic() - start) * 1000, 1))

            def _public(self, method: str, parts: List[str]) -> Optional[int]:
                if method not in ("GET", "HEAD"):
                    return None
                if not parts or parts == ["index.html"]:
                    if not server.enable_dashboard:
                        raise ApiError(404, "Dashboard disabled")
                    self._send(200, server._dashboard, "text/html; charset=utf-8",
                               {"Content-Security-Policy": _DASHBOARD_CSP})
                    return 200
                if server.enable_dashboard and (parts[0] == "static" or parts == ["sw.js"]):
                    path = "static/sw.js" if parts == ["sw.js"] else "/".join(parts)
                    suffix = "." + path.rsplit(".", 1)[-1] if "." in path else ""
                    data = _asset(path) if suffix in _STATIC_TYPES else None
                    if data is None:
                        raise ApiError(404, "Not found")
                    headers = {"Service-Worker-Allowed": "/"} if parts == ["sw.js"] else {}
                    self._send(200, data, _STATIC_TYPES[suffix], headers)
                    return 200
                if parts == ["health"]:
                    code, payload = server.health()
                    self._json(code, payload)
                    return code
                if parts == ["ready"]:
                    code = 200 if server.ready else 503
                    self._json(code, {"ready": server.ready})
                    return code
                if parts == ["metrics"]:
                    self._send(200, server.prometheus().encode("utf-8"),
                               "text/plain; version=0.0.4; charset=utf-8")
                    return 200
                return None

            def _dispatch(self, method: str, parts: List[str], query: Dict[str, List[str]]) -> int:
                handled = self._public(method, parts)
                if handled is not None:
                    return handled
                if not parts or parts[0] != "api":
                    raise ApiError(404, "Not found")
                rest = parts[1:]

                # Unauthenticated auth endpoints
                if rest == ["auth", "status"] and method == "GET":
                    self._json(200, {"auth_required": server.auth.enabled,
                                     "users_enabled": server.users is not None and server.users.has_users(),
                                     "version": server.hub.stats()["version"]})
                    return 200
                if rest == ["auth", "login"] and method == "POST":
                    body = self._body()
                    try:
                        token, expires, principal = server.auth.login(
                            str(body.get("username", "")), str(body.get("password", "")), self.client_address[0])
                    except AuthorizationError as e:
                        raise ApiError(429, e.message, **e.details) from None
                    except AuthenticationError as e:
                        raise ApiError(401, e.message) from None
                    except ConfigurationError as e:
                        raise ApiError(404, e.message) from None
                    self._json(200, {"token": token, "expires_at": expires, "user": principal.to_dict()})
                    return 200

                principal = self._principal(query, allow_query_token=rest == ["events"])
                return self._authenticated(method, rest, query, principal)

            def _authenticated(self, method: str, rest: List[str], query: Dict[str, List[str]],
                               principal: Principal) -> int:
                fresh = (query.get("fresh") or ["0"])[0].lower() in ("1", "true", "yes")

                # ---- session / account
                if rest == ["auth", "me"] and method == "GET":
                    self._json(200, principal.to_dict())
                    return 200
                if rest == ["auth", "logout"] and method == "POST":
                    if principal.token_kind == "session" and server.users is not None:  # noqa: S105
                        server.users.revoke_token(self._bearer(query, False))
                    self._json(200, {"ok": True})
                    return 200
                if rest == ["me", "password"] and method == "POST":
                    users = self._users()
                    body = self._body()
                    try:
                        users.authenticate(principal.username, str(body.get("current", "")))
                        users.set_password(principal.username, str(body.get("new", "")))
                    except AuthenticationError:
                        raise ApiError(400, "Current password is incorrect") from None
                    except ConfigurationError as e:
                        raise ApiError(400, e.message) from None
                    self._json(200, {"ok": True})
                    return 200
                if rest[:1] == ["tokens"]:
                    users = self._users()
                    if principal.token_kind not in ("session", "api"):
                        raise ApiError(400, "Tokens belong to user accounts; sign in as a user")
                    if method == "GET" and len(rest) == 1:
                        self._json(200, {"tokens": users.list_tokens(principal.username)})
                        return 200
                    if method == "POST" and len(rest) == 1:
                        body = self._body()
                        name = str(body.get("name", "")).strip() or "api"
                        ttl_days = body.get("ttl_days")
                        token, expires = users.issue_token(
                            principal.username, name=name, kind="api",
                            ttl=float(ttl_days) * 86400 if ttl_days else None)
                        self._json(200, {"token": token, "name": name, "expires_at": expires})
                        return 200
                    if method == "DELETE" and len(rest) == 2:
                        if not users.revoke_token_id(principal.username, rest[1]):
                            raise ApiError(404, "Token not found")
                        self._json(200, {"ok": True})
                        return 200

                # ---- users (admin)
                if rest[:1] == ["users"]:
                    self._require(principal, "admin")
                    users = self._users()
                    try:
                        if method == "GET" and len(rest) == 1:
                            self._json(200, {"users": users.list_users()})
                            return 200
                        if method == "POST" and len(rest) == 1:
                            body = self._body()
                            users.add_user(str(body.get("username", "")), str(body.get("password", "")),
                                           str(body.get("role", "viewer")))
                            self._json(200, {"ok": True})
                            return 200
                        if method == "POST" and len(rest) == 2:
                            body = self._body()
                            if "role" in body:
                                users.set_role(rest[1], str(body["role"]))
                            if "disabled" in body:
                                users.set_disabled(rest[1], bool(body["disabled"]))
                            if "password" in body:
                                users.set_password(rest[1], str(body["password"]))
                            self._json(200, {"ok": True})
                            return 200
                        if method == "DELETE" and len(rest) == 2:
                            users.remove_user(rest[1])
                            self._json(200, {"ok": True})
                            return 200
                    except ConfigurationError as e:
                        raise ApiError(400, e.message) from None

                # ---- devices
                if method == "GET" and rest == ["devices"]:
                    self._json(200, {"devices": [server.device_info(d) for d in server.hub.devices()]})
                    return 200
                if method == "GET" and rest == ["stats"]:
                    self._json(200, server.hub.stats())
                    return 200
                if method == "GET" and rest == ["events"]:
                    self._stream_events()
                    return 200
                if len(rest) >= 2 and rest[0] == "devices":
                    device = self._device(rest[1])
                    if method == "GET" and len(rest) == 2:
                        self._json(200, server.device_info(device, fresh=fresh))
                        return 200
                    if method == "GET" and rest[2:] == ["state"]:
                        try:
                            self._json(200, {"device_id": device.id, "state": device.read_state(fresh=fresh)})
                        except StateError as e:
                            raise ApiError(502, e.message) from e
                        return 200
                    if method == "GET" and rest[2:] == ["history"]:
                        if server.hub.history is None:
                            raise ApiError(404, "History is disabled (set history_enabled: true)")
                        try:
                            limit = int((query.get("limit") or ["100"])[0])
                        except ValueError:
                            raise ApiError(400, "limit must be an integer") from None
                        limit = max(1, min(limit, 10000))
                        self._json(200, {"device_id": device.id,
                                         "history": server.hub.history.get_history(device.id, limit=limit)})
                        return 200
                    if method == "POST" and len(rest) == 4 and rest[2] == "commands":
                        self._require(principal, "operator")
                        code, payload = server.run_command(device, rest[3], self._body())
                        self._json(code, payload)
                        return code

                # ---- rules
                if rest[:1] == ["rules"] and server.rule_engine is not None:
                    if method == "GET" and len(rest) == 1:
                        self._json(200, {"rules": server.rule_engine.list_rules()})
                        return 200
                    if method == "POST" and len(rest) == 3 and rest[2] in ("enable", "disable"):
                        self._require(principal, "admin")
                        if not server.rule_engine.set_enabled(rest[1], rest[2] == "enable"):
                            raise ApiError(404, f"Rule not found: {rest[1]}")
                        self._json(200, {"name": rest[1], "enabled": rest[2] == "enable"})
                        return 200

                # ---- schedules
                if rest[:1] == ["schedules"]:
                    if method == "GET" and len(rest) == 1:
                        self._json(200, server.schedules_info())
                        return 200
                    if method == "POST" and len(rest) == 3 and server.scheduler is not None:
                        name, action = rest[1], rest[2]
                        if server.scheduler.get(name) is None:
                            raise ApiError(404, f"Schedule not found: {name}")
                        if action == "run":
                            self._require(principal, "operator")
                            self._json(200, {"name": name, "ok": server.scheduler.run_now(name)})
                            return 200
                        if action in ("enable", "disable"):
                            self._require(principal, "admin")
                            server.scheduler.set_enabled(name, action == "enable")
                            self._json(200, {"name": name, "enabled": action == "enable"})
                            return 200

                # ---- notifications (admin)
                if rest[:1] == ["notifications"]:
                    self._require(principal, "admin")
                    notifier = server.notifier
                    if method == "GET" and len(rest) == 1:
                        self._json(200, notifier.describe() if notifier is not None
                                   else {"channels": {}, "alerts": {}, "cooldown": None, "recent": []})
                        return 200
                    if method == "POST" and rest[1:] == ["test"]:
                        channel = str(self._body().get("channel", ""))
                        if notifier is None or not notifier.has_channel(channel):
                            raise ApiError(400, f"Unknown notification channel: {channel!r}")
                        ok = notifier.send(channel, "This is a test notification from FuseIoT.",
                                           title="FuseIoT test", priority="default")
                        if not ok:
                            raise ApiError(502, "Sending failed; check the server log for details")
                        self._json(200, {"ok": True})
                        return 200

                raise ApiError(404, "Not found")

            def _stream_events(self) -> None:
                q: queue.Queue[Optional[str]] = queue.Queue(maxsize=1000)
                with server._subs_lock:
                    server._subscribers.append(q)
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Connection", "close")
                self.send_header("X-Accel-Buffering", "no")
                self.end_headers()
                self.close_connection = True
                try:
                    self.wfile.write(b": connected\n\n")
                    self.wfile.flush()
                    while True:
                        try:
                            item = q.get(timeout=15)
                        except queue.Empty:
                            self.wfile.write(b": keepalive\n\n")
                            self.wfile.flush()
                            continue
                        if item is None:
                            break
                        self.wfile.write(item.encode("utf-8"))
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                    pass
                finally:
                    with server._subs_lock:
                        if q in server._subscribers:
                            server._subscribers.remove(q)

            def do_GET(self) -> None:
                self._route("GET")

            def do_HEAD(self) -> None:
                self._route("HEAD")

            def do_POST(self) -> None:
                self._route("POST")

            def do_DELETE(self) -> None:
                self._route("DELETE")

        return Handler


def run_server(hub: Any, host: str = "127.0.0.1", port: int = 8080, **kwargs: Any) -> FuseIoTServer:
    """Start a server in the background and return it."""
    return FuseIoTServer(hub, host=host, port=port, **kwargs).start()
