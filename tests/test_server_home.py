"""Server home-automation features: logins, roles, tokens, users, schedules, notifications,
PWA assets, monitor-driven health and SSE status events."""
import http.client
import json

import pytest

from fuseiot import Switchable
from fuseiot.auth import UserStore
from fuseiot.automation.schedule import Scheduler
from fuseiot.exceptions import ConfigurationError
from fuseiot.monitoring.device_monitor import DeviceMonitor
from fuseiot.notify import LogChannel, NotificationManager
from fuseiot.server import app as app_module
from fuseiot.server.app import FuseIoTServer
from tests.helpers import start_fast
from tests.test_server import call, call_json

PW = "correct horse"
SERVICE = "service-token"


class Broken(LogChannel):
    def send(self, *a, **kw):
        raise RuntimeError("down")


@pytest.fixture
def users(tmp_path, fast_scrypt):
    store = UserStore(str(tmp_path / "users.db"))
    store.add_user("admin", PW, "admin")
    store.add_user("op", PW, "operator")
    store.add_user("viewer", PW, "viewer")
    yield store
    store.close()


@pytest.fixture
def home(hub, add_virtual):
    add_virtual("switchable", Switchable, "lamp")
    add_virtual("switchable", Switchable, "fan")
    return hub


@pytest.fixture
def channel():
    return LogChannel("log")


@pytest.fixture
def notifier(channel):
    return NotificationManager({"log": channel, "broken": Broken("broken")},
                               alerts={"device_offline": ["log"]}, cooldown=0)


@pytest.fixture
def scheduler(home):
    s = Scheduler.from_config(home, [
        {"name": "evening", "at": "sunset", "then": {"device": "lamp", "command": "on"}},
        {"name": "night", "at": "23:30", "then": {"device": "lamp", "command": "off"}},
    ], {"latitude": 51.5, "longitude": -0.12, "timezone": "Europe/London"})
    yield s
    s.stop()


@pytest.fixture
def monitor(home):
    m = DeviceMonitor(home, interval=60, offline_after=1)
    yield m
    m.stop()


@pytest.fixture
def make_server():
    servers = []

    def _make(hub, **kw):
        kw.setdefault("port", 0)
        srv = FuseIoTServer(hub, **kw)
        start_fast(srv, srv._httpd)
        servers.append(srv)
        return srv

    yield _make
    for srv in servers:
        srv.stop()


@pytest.fixture
def server(make_server, home, users, scheduler, monitor, notifier):
    return make_server(home, users=users, scheduler=scheduler, monitor=monitor, notifier=notifier,
                       api_token=SERVICE)


def login(server, username, password=PW):
    status, body = call_json(server, "POST", "/api/auth/login", token=None,
                             body={"username": username, "password": password})
    assert status == 200, body
    return body["token"]


@pytest.fixture
def tokens(server):
    return {name: login(server, name) for name in ("admin", "op", "viewer")}


# --------------------------------------------------------------------------- auth endpoints


class TestAuthEndpoints:
    def test_status_is_public(self, server):
        status, body = call_json(server, "GET", "/api/auth/status", token=None)
        assert status == 200 and body["auth_required"] is True and body["users_enabled"] is True
        assert body["version"]

    def test_status_without_auth(self, make_server, home):
        srv = make_server(home)
        body = call_json(srv, "GET", "/api/auth/status", token=None)[1]
        assert body["auth_required"] is False and body["users_enabled"] is False

    def test_login_and_me(self, server):
        status, body = call_json(server, "POST", "/api/auth/login", token=None,
                                 body={"username": "op", "password": PW})
        assert status == 200 and body["token"].startswith("fio_") and body["expires_at"]
        assert body["user"] == {"username": "op", "role": "operator", "token_kind": "session"}
        me = call_json(server, "GET", "/api/auth/me", token=body["token"])
        assert me == (200, {"username": "op", "role": "operator", "token_kind": "session"})

    @pytest.mark.parametrize("body", [{"username": "op", "password": "nope nope"},
                                      {"username": "ghost", "password": PW}, {}])
    def test_bad_credentials_401(self, server, body):
        status, payload = call_json(server, "POST", "/api/auth/login", token=None, body=body)
        assert status == 401 and "Invalid username or password" in payload["error"]

    def test_login_throttled_429(self, server):
        for _ in range(5):
            assert call(server, "POST", "/api/auth/login", token=None,
                        body={"username": "op", "password": "wrong guess"})[0] == 401
        status, body = call_json(server, "POST", "/api/auth/login", token=None,
                                 body={"username": "op", "password": PW})
        assert status == 429 and body["retry_after"] > 0 and "Too many failed logins" in body["error"]

    def test_login_without_user_store_404(self, make_server, home):
        srv = make_server(home, api_token=SERVICE)
        assert call(srv, "POST", "/api/auth/login", token=None, body={"username": "a", "password": "b"})[0] == 404

    def test_me_requires_token(self, server):
        assert call(server, "GET", "/api/auth/me", token=None)[0] == 401
        assert call_json(server, "GET", "/api/auth/me", token=SERVICE)[1]["token_kind"] == "service"

    def test_logout_revokes_session(self, server):
        token = login(server, "viewer")
        assert call_json(server, "POST", "/api/auth/logout", token=token) == (200, {"ok": True})
        assert call(server, "GET", "/api/auth/me", token=token)[0] == 401

    def test_logout_keeps_api_token(self, server, users):
        api, _ = users.issue_token("viewer", kind="api")
        assert call(server, "POST", "/api/auth/logout", token=api)[0] == 200
        assert call(server, "GET", "/api/auth/me", token=api)[0] == 200

    def test_change_own_password(self, server):
        token = login(server, "op")
        assert call(server, "POST", "/api/me/password", token=token,
                    body={"current": "bad bad bad", "new": "brand new pw"})[0] == 400
        status, body = call_json(server, "POST", "/api/me/password", token=token,
                                 body={"current": PW, "new": "short"})
        assert status == 400 and "at least 8" in body["error"]
        assert call(server, "POST", "/api/me/password", token=token,
                    body={"current": PW, "new": "brand new pw"})[0] == 200
        assert call(server, "GET", "/api/auth/me", token=token)[0] == 401  # session revoked
        login(server, "op", "brand new pw")

    def test_service_token_cannot_change_password(self, server):
        assert call(server, "POST", "/api/me/password", token=SERVICE, body={"current": "x", "new": "y" * 9})[0] == 400


# --------------------------------------------------------------------------- tokens


class TestTokenEndpoints:
    def test_create_list_use_delete(self, server, tokens):
        status, body = call_json(server, "POST", "/api/tokens", token=tokens["op"],
                                 body={"name": "phone", "ttl_days": 1})
        assert status == 200 and body["name"] == "phone" and body["expires_at"]
        api = body["token"]
        assert call_json(server, "GET", "/api/auth/me", token=api)[1]["token_kind"] == "api"
        listed = call_json(server, "GET", "/api/tokens", token=tokens["op"])[1]["tokens"]
        assert [t["name"] for t in listed] == ["phone"] and "token" not in listed[0]
        # Other users do not see or delete it.
        assert call_json(server, "GET", "/api/tokens", token=tokens["admin"])[1]["tokens"] == []
        assert call(server, "DELETE", f"/api/tokens/{listed[0]['id']}", token=tokens["admin"])[0] == 404
        assert call(server, "DELETE", f"/api/tokens/{listed[0]['id']}", token=tokens["op"])[0] == 200
        assert call(server, "GET", "/api/auth/me", token=api)[0] == 401
        assert call(server, "DELETE", f"/api/tokens/{listed[0]['id']}", token=tokens["op"])[0] == 404

    def test_default_name_and_no_expiry(self, server, tokens):
        body = call_json(server, "POST", "/api/tokens", token=tokens["viewer"], body={})[1]
        assert body["name"] == "api" and body["expires_at"] is None

    def test_service_token_has_no_tokens(self, server):
        status, body = call_json(server, "GET", "/api/tokens", token=SERVICE)
        assert status == 400 and "user accounts" in body["error"]

    def test_tokens_without_users_404(self, make_server, home):
        srv = make_server(home, api_token=SERVICE)
        assert call(srv, "GET", "/api/tokens", token=SERVICE)[0] == 404


# --------------------------------------------------------------------------- roles


class TestRoles:
    def test_viewer_can_read_but_not_command(self, server, tokens):
        assert call(server, "GET", "/api/devices", token=tokens["viewer"])[0] == 200
        status, body = call_json(server, "POST", "/api/devices/lamp/commands/on", token=tokens["viewer"])
        assert status == 403 and "operator" in body["error"]

    def test_operator_can_command(self, server, tokens, home):
        assert call(server, "POST", "/api/devices/lamp/commands/on", token=tokens["op"])[0] == 200
        assert home["lamp"].is_on

    @pytest.mark.parametrize("path", ["/api/users", "/api/notifications"])
    @pytest.mark.parametrize("who", ["viewer", "op"])
    def test_admin_only_reads(self, server, tokens, path, who):
        assert call(server, "GET", path, token=tokens[who])[0] == 403

    def test_admin_and_service_can_read_admin_endpoints(self, server, tokens):
        assert call(server, "GET", "/api/users", token=tokens["admin"])[0] == 200
        assert call(server, "GET", "/api/users", token=SERVICE)[0] == 200


# --------------------------------------------------------------------------- users


class TestUserEndpoints:
    def test_list_add_update_delete(self, server, tokens, users):
        admin = tokens["admin"]
        names = [u["username"] for u in call_json(server, "GET", "/api/users", token=admin)[1]["users"]]
        assert names == ["admin", "op", "viewer"]
        assert call(server, "POST", "/api/users", token=admin,
                    body={"username": "guest", "password": "guest pass 1"})[0] == 200
        assert {u["username"]: u["role"] for u in users.list_users()}["guest"] == "viewer"
        assert call(server, "POST", "/api/users/guest", token=admin, body={"role": "operator"})[0] == 200
        assert call(server, "POST", "/api/users/guest", token=admin, body={"password": "other pass 2"})[0] == 200
        assert users.authenticate("guest", "other pass 2").role == "operator"
        assert call(server, "POST", "/api/users/guest", token=admin, body={"disabled": True})[0] == 200
        assert call(server, "POST", "/api/auth/login", token=None,
                    body={"username": "guest", "password": "other pass 2"})[0] == 401
        assert call(server, "DELETE", "/api/users/guest", token=admin)[0] == 200
        assert "guest" not in [u["username"] for u in users.list_users()]

    def test_disabling_user_kills_their_sessions(self, server, tokens):
        assert call(server, "POST", "/api/users/viewer", token=tokens["admin"], body={"disabled": True})[0] == 200
        assert call(server, "GET", "/api/auth/me", token=tokens["viewer"])[0] == 401

    @pytest.mark.parametrize("method,path,body,fragment", [
        ("POST", "/api/users", {"username": "bad name", "password": "long enough"}, "Username"),
        ("POST", "/api/users", {"username": "x", "password": "short"}, "at least 8"),
        ("POST", "/api/users", {"username": "op", "password": "long enough"}, "already exists"),
        ("POST", "/api/users", {"username": "y", "password": "long enough", "role": "root"}, "Role must be"),
        ("POST", "/api/users/admin", {"role": "viewer"}, "last admin"),
        ("POST", "/api/users/admin", {"disabled": True}, "last admin"),
        ("DELETE", "/api/users/admin", None, "last admin"),
        ("POST", "/api/users/ghost", {"role": "viewer"}, "No such user"),
    ])
    def test_errors_are_400(self, server, tokens, method, path, body, fragment):
        status, payload = call_json(server, method, path, token=tokens["admin"], body=body)
        assert status == 400 and fragment in payload["error"]

    def test_users_without_store_404(self, make_server, home):
        srv = make_server(home, api_token=SERVICE)
        assert call(srv, "GET", "/api/users", token=SERVICE)[0] == 404

    def test_unknown_user_route_404(self, server, tokens):
        assert call(server, "DELETE", "/api/users", token=tokens["admin"])[0] == 404


# --------------------------------------------------------------------------- schedules


class TestScheduleEndpoints:
    def test_list(self, server, tokens):
        status, body = call_json(server, "GET", "/api/schedules", token=tokens["viewer"])
        assert status == 200
        assert [s["name"] for s in body["schedules"]] == ["evening", "night"]
        assert all(s["next_run"] for s in body["schedules"])
        assert body["location"] == {"timezone": "Europe/London", "latitude": 51.5, "longitude": -0.12}
        assert body["sun"]["sunrise"] and body["sun"]["sunset"] and body["sun"]["date"]

    def test_list_without_scheduler(self, make_server, home):
        srv = make_server(home)
        assert call_json(srv, "GET", "/api/schedules", token=None) == (
            200, {"schedules": [], "location": None, "sun": None})

    def test_list_without_coordinates_has_no_sun(self, make_server, home):
        sched = Scheduler.from_config(home, [{"name": "n", "at": "10:00",
                                              "then": {"device": "lamp", "command": "on"}}], {"timezone": "UTC"})
        srv = make_server(home, scheduler=sched)
        body = call_json(srv, "GET", "/api/schedules", token=None)[1]
        assert body["sun"] is None and body["location"]["latitude"] is None

    def test_run_requires_operator(self, server, tokens, home):
        assert call(server, "POST", "/api/schedules/evening/run", token=tokens["viewer"])[0] == 403
        status, body = call_json(server, "POST", "/api/schedules/evening/run", token=tokens["op"])
        assert status == 200 and body == {"name": "evening", "ok": True}
        assert home["lamp"].is_on

    def test_enable_disable_requires_admin(self, server, tokens, scheduler):
        assert call(server, "POST", "/api/schedules/night/disable", token=tokens["op"])[0] == 403
        status, body = call_json(server, "POST", "/api/schedules/night/disable", token=tokens["admin"])
        assert status == 200 and body == {"name": "night", "enabled": False}
        assert scheduler.get("night").enabled is False
        listed = call_json(server, "GET", "/api/schedules", token=tokens["admin"])[1]["schedules"]
        assert listed[1]["next_run"] is None
        assert call(server, "POST", "/api/schedules/night/enable", token=tokens["admin"])[0] == 200
        assert scheduler.get("night").enabled is True

    def test_unknown_schedule_and_action(self, server, tokens):
        assert call(server, "POST", "/api/schedules/ghost/run", token=tokens["admin"])[0] == 404
        assert call(server, "POST", "/api/schedules/night/explode", token=tokens["admin"])[0] == 404


# --------------------------------------------------------------------------- notifications


class TestNotificationEndpoints:
    def test_describe(self, server, tokens):
        status, body = call_json(server, "GET", "/api/notifications", token=tokens["admin"])
        assert status == 200 and body["channels"] == {"log": "log", "broken": "log"}
        assert body["alerts"] == {"device_offline": ["log"]} and body["cooldown"] == 0

    def test_test_endpoint(self, server, tokens, channel):
        assert call_json(server, "POST", "/api/notifications/test", token=tokens["admin"],
                         body={"channel": "log"}) == (200, {"ok": True})
        assert channel.sent[-1]["title"] == "FuseIoT test"
        status, body = call_json(server, "POST", "/api/notifications/test", token=tokens["admin"],
                                 body={"channel": "sms"})
        assert status == 400 and "Unknown notification channel" in body["error"]
        assert call(server, "POST", "/api/notifications/test", token=tokens["admin"],
                    body={"channel": "broken"})[0] == 502
        recent = call_json(server, "GET", "/api/notifications", token=tokens["admin"])[1]["recent"]
        assert [r["ok"] for r in recent] == [True, False]

    def test_test_endpoint_requires_admin(self, server, tokens):
        assert call(server, "POST", "/api/notifications/test", token=tokens["op"],
                    body={"channel": "log"})[0] == 403

    def test_without_notifier(self, make_server, home):
        srv = make_server(home)
        assert call_json(srv, "GET", "/api/notifications", token=None)[1] == {
            "channels": {}, "alerts": {}, "cooldown": None, "recent": []}
        assert call(srv, "POST", "/api/notifications/test", token=None, body={"channel": "x"})[0] == 400

    def test_notifier_defaults_to_hub_notifier(self, make_server, home, notifier):
        home.notifier = notifier
        srv = make_server(home)
        assert srv.notifier is notifier

    def test_failed_command_alerts(self, make_server, home, add_virtual):
        add_virtual("switchable", Switchable, "broken", failure_rate=1.0)
        log = LogChannel("log")
        mgr = NotificationManager({"log": log}, alerts={"command_failed": ["log"]})
        srv = make_server(home, notifier=mgr)
        assert call(srv, "POST", "/api/devices/broken/commands/on", token=None)[0] == 502
        assert log.sent[0]["title"] == "broken: on failed"


# --------------------------------------------------------------------------- static / PWA


class TestStaticAssets:
    def test_manifest(self, server):
        status, headers, body = call(server, "GET", "/static/manifest.webmanifest", token=None)
        assert status == 200 and headers["Content-Type"] == "application/manifest+json"
        assert json.loads(body)["icons"]

    def test_service_worker(self, server):
        status, headers, body = call(server, "GET", "/sw.js", token=None)
        assert status == 200 and headers["Service-Worker-Allowed"] == "/"
        assert headers["Content-Type"].startswith("text/javascript") and body

    def test_icon(self, server):
        status, headers, body = call(server, "GET", "/static/icons/icon-192.png", token=None)
        assert status == 200 and headers["Content-Type"] == "image/png" and body[:4] == b"\x89PNG"

    def test_head_request(self, server):
        conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=10)
        try:
            conn.request("HEAD", "/static/manifest.webmanifest")
            resp = conn.getresponse()
            assert resp.status == 200 and resp.read() == b"" and int(resp.getheader("Content-Length")) > 0
        finally:
            conn.close()

    @pytest.mark.parametrize("path", [
        "/static/../server/app.py", "/static/%2e%2e/templates/index.html", "/static/..%2F..%2Fauth.py",
        "/static/icons/missing.png", "/static/templates", "/static/sw", "/static/foo.exe",
        "/static/..\\..\\x.js", "/static/%5c..%5cx.js",
    ])
    def test_unsafe_or_missing_paths_404(self, server, path):
        assert call(server, "GET", path, token=None)[0] == 404

    def test_static_disabled_with_dashboard(self, make_server, home):
        srv = make_server(home, enable_dashboard=False)
        assert call(srv, "GET", "/static/manifest.webmanifest", token=None)[0] == 404
        assert call(srv, "GET", "/sw.js", token=None)[0] == 404

    def test_asset_helper_rejects_traversal(self):
        assert app_module._asset("../server/app.py") is None
        assert app_module._asset("static/./sw.js") is None
        assert app_module._asset("") is None
        assert app_module._asset("static/sw.js")


# --------------------------------------------------------------------------- monitor integration


class TestMonitorIntegration:
    def test_health_reports_monitor_offline_devices(self, server, monitor, home):
        monitor.poll_all()
        body = call_json(server, "GET", "/health", token=None)[1]
        assert body["status"] == "healthy" and body["offline_devices"] == []
        home["fan"].protocol.sim.failure_rate = 1.0
        monitor.poll_all()
        status, body = call_json(server, "GET", "/health", token=None)
        assert status == 200 and body["status"] == "degraded" and body["offline_devices"] == ["fan"]
        home["lamp"].protocol.sim.failure_rate = 1.0
        monitor.poll_all()
        status, body = call_json(server, "GET", "/health", token=None)
        assert status == 503 and body["status"] == "unhealthy"

    def test_device_info_includes_status(self, server, monitor, tokens):
        monitor.poll_all()
        info = call_json(server, "GET", "/api/devices/lamp", token=tokens["viewer"])[1]
        assert info["status"]["online"] is True

    def test_metrics_device_up(self, server, monitor, home):
        home["fan"].protocol.sim.failure_rate = 1.0
        monitor.poll_all()
        text = call(server, "GET", "/metrics", token=None)[2].decode()
        assert 'fuseiot_device_up{device="fan"} 0' in text
        assert 'fuseiot_device_up{device="lamp"} 1' in text

    def test_sse_status_events(self, server, monitor, home, tokens):
        conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=10)
        try:
            conn.request("GET", f"/api/events?token={tokens['viewer']}")
            resp = conn.getresponse()
            assert resp.status == 200
            assert resp.fp.readline() == b": connected\n"
            assert resp.fp.readline() == b"\n"
            home["fan"].protocol.sim.failure_rate = 1.0
            monitor.poll_all()
            events = []
            while len(events) < 2:  # lamp + fan go online/offline (order not guaranteed)
                line = resp.fp.readline()
                if line == b"event: status\n":
                    data = resp.fp.readline().decode()
                    events.append(json.loads(data[len("data: "):]))
            by_id = {e["device_id"]: e["status"]["online"] for e in events}
            assert by_id == {"lamp": True, "fan": False}
        finally:
            conn.close()

    def test_stop_detaches_monitor_listener(self, make_server, home, monitor):
        srv = make_server(home, monitor=monitor)
        assert srv._broadcast_status in monitor.listeners
        srv.stop()
        assert srv._broadcast_status not in monitor.listeners


# --------------------------------------------------------------------------- insecure bind


class _FakeHTTPD:
    def __init__(self, address, handler):
        self.server_address = address
        self.daemon_threads = False

    def server_close(self):
        pass

    def shutdown(self):
        pass


class TestInsecureBind:
    def test_refused_without_users(self, home, tmp_path):
        empty = UserStore(str(tmp_path / "empty.db"))
        try:
            with pytest.raises(ConfigurationError, match="Refusing"):
                FuseIoTServer(home, host="0.0.0.0", port=0, users=empty)
        finally:
            empty.close()

    def test_allowed_once_users_exist(self, home, users, monkeypatch):
        monkeypatch.setattr(app_module, "ThreadingHTTPServer", _FakeHTTPD)
        srv = FuseIoTServer(home, host="0.0.0.0", port=8123, users=users)
        assert srv.auth.enabled and srv.host == "0.0.0.0"
        srv.stop()
