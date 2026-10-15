"""
HTTP/REST transport.

Payload convention used by all capabilities::

    {"_method": "GET" | "POST" | "PUT" | "PATCH" | "DELETE",
     "_path": "/state",
     ...body fields...}

Keys starting with ``_`` are routing metadata; everything else is sent as the
JSON body (or query string for GET/DELETE).
"""
import asyncio
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple, Union

from ..exceptions import AuthenticationError, ProtocolError
from ..logging_config import get_logger
from .base import AsyncProtocol, ConnectionState, ProtocolConfig

try:  # optional
    import aiohttp

    AIOHTTP_AVAILABLE = True
except ImportError:  # pragma: no cover - depends on environment
    aiohttp = None  # type: ignore[assignment]
    AIOHTTP_AVAILABLE = False

logger = get_logger("protocols.http")

_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")


@dataclass
class HTTPConfig(ProtocolConfig):
    """HTTP-specific configuration."""
    base_url: str = ""
    auth: Optional[Tuple[str, str]] = None
    bearer_token: Optional[str] = None
    headers: Optional[Dict[str, str]] = None
    verify_ssl: bool = True
    client_cert: Optional[Tuple[str, str]] = None
    connection_pool_size: int = 100


class HTTP(AsyncProtocol):
    """
    HTTP/REST device transport (sync via ``requests``, async via ``aiohttp``).

    Args:
        base_url: Device API root, e.g. ``http://192.168.1.45/api``.
        auth: ``(username, password)`` for HTTP Basic auth.
        bearer_token: Sent as ``Authorization: Bearer <token>``.
        headers: Extra headers.
        timeout: Per-request timeout in seconds.
        verify_ssl: Verify TLS certificates (or path to a CA bundle).
        client_cert: ``(cert_file, key_file)`` for mutual TLS.
        health_path: Path probed by :meth:`connect` (default: ``base_url``).
    """

    def __init__(
        self,
        base_url: str,
        auth: Optional[Tuple[str, str]] = None,
        bearer_token: Optional[str] = None,
        headers: Optional[Dict[str, str]] = None,
        timeout: Optional[float] = None,
        verify_ssl: Union[bool, str] = True,
        client_cert: Optional[Tuple[str, str]] = None,
        health_path: str = "",
        config: Optional[HTTPConfig] = None,
    ):
        super().__init__(config or HTTPConfig())
        if not base_url.startswith(("http://", "https://")):
            base_url = "http://" + base_url
        self.base_url = base_url.rstrip("/")
        self.auth = auth
        self.bearer_token = bearer_token
        self.headers = dict(headers or {})
        self.verify_ssl = verify_ssl
        self.client_cert = client_cert
        self.health_path = health_path
        if timeout is not None:
            self.config.timeout = float(timeout)

        self._sync_session: Any = None
        self._session_lock = threading.Lock()
        self._async_session: Any = None
        self._async_loop: Optional[asyncio.AbstractEventLoop] = None

    # ----------------------------------------------------------------- basics

    @property
    def timeout(self) -> float:
        return self.config.timeout

    @property
    def endpoint(self) -> str:
        return self.base_url

    def _get_headers(self) -> Dict[str, str]:
        h = {"Content-Type": "application/json", "Accept": "application/json", **self.headers}
        if self.bearer_token:
            h["Authorization"] = f"Bearer {self.bearer_token}"
        return h

    @staticmethod
    def _split(payload: Dict[str, Any]) -> Tuple[str, str, Dict[str, Any]]:
        method = str(payload.get("_method", "POST")).upper()
        if method not in _METHODS:
            raise ValueError(f"Unsupported HTTP method: {method}")
        path = str(payload.get("_path", "/"))
        if not path.startswith("/"):
            path = "/" + path
        body = {k: v for k, v in payload.items() if not str(k).startswith("_")}
        return method, path, body

    # ------------------------------------------------------------------- sync

    def _session(self) -> Any:
        try:
            import requests
        except ImportError as e:  # pragma: no cover
            raise ImportError("requests is required for HTTP: pip install 'fuseiot[http]'") from e
        with self._session_lock:
            if self._sync_session is None:
                session = requests.Session()
                session.headers.update(self._get_headers())
                if self.auth:
                    session.auth = tuple(self.auth)
                session.verify = self.verify_ssl
                if self.client_cert:
                    session.cert = tuple(self.client_cert)
                self._sync_session = session
            return self._sync_session

    def connect(self) -> bool:
        """
        Check the device is reachable.

        Any HTTP response (even 404/405) proves the host is up; only network
        errors or 5xx responses fail the check.
        """
        self._update_state(ConnectionState.CONNECTING)
        url = f"{self.base_url}{self.health_path}"
        try:
            response = self._session().get(url, timeout=self.config.timeout)
        except Exception as e:
            self._update_state(ConnectionState.FAILED, f"{type(e).__name__}: {e}")
            return False
        if response.status_code >= 500:
            self._update_state(ConnectionState.FAILED, f"HTTP {response.status_code} from {url}")
            return False
        if response.status_code == 401:
            self._update_state(ConnectionState.FAILED, "Authentication failed (HTTP 401)")
            return False
        self._update_state(ConnectionState.CONNECTED)
        return True

    def disconnect(self) -> None:
        with self._session_lock:
            if self._sync_session is not None:
                self._sync_session.close()
                self._sync_session = None
        self._update_state(ConnectionState.DISCONNECTED)

    def _decode(self, response: Any, url: str) -> Dict[str, Any]:
        if response.status_code in (401, 403):
            raise AuthenticationError(
                f"HTTP {response.status_code} from {url}",
                details={"url": url, "status": response.status_code},
            )
        if response.status_code >= 400:
            raise ProtocolError(
                f"HTTP {response.status_code}: {response.text[:200]}", "http", url,
                status=response.status_code,
            )
        if not response.content:
            return {}
        try:
            data = response.json()
        except ValueError as e:
            raise ProtocolError(f"Invalid JSON response from {url}: {e}", "http", url, e) from e
        return data if isinstance(data, dict) else {"value": data}

    def send(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Send one request and return the decoded JSON response."""
        method, path, body = self._split(payload)
        url = f"{self.base_url}{path}"
        start = time.monotonic()
        try:
            kwargs: Dict[str, Any] = {"timeout": self.config.timeout}
            if method in ("GET", "DELETE"):
                kwargs["params"] = body or None
            else:
                kwargs["json"] = body
            response = self._session().request(method, url, **kwargs)
            data = self._decode(response, url)
        except (AuthenticationError, ProtocolError) as e:
            self._record_error(str(e))
            raise
        except Exception as e:
            self._update_state(ConnectionState.FAILED, str(e))  # records the error once
            raise ProtocolError(f"HTTP request failed: {e}", "http", url, e) from e

        if not self.is_connected:
            self._update_state(ConnectionState.CONNECTED)
        self._record_success((time.monotonic() - start) * 1000)
        return data

    # ------------------------------------------------------------------ async

    def _has_live_async_session(self) -> bool:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return False
        return (self._async_session is not None and not self._async_session.closed
                and self._async_loop is loop)

    def _new_async_session(self) -> Any:
        auth = aiohttp.BasicAuth(*self.auth) if self.auth else None
        ssl: Any = None
        if self.verify_ssl is False:
            ssl = False
        elif isinstance(self.verify_ssl, str) or self.client_cert:
            import ssl as ssl_mod

            ssl = ssl_mod.create_default_context(
                cafile=self.verify_ssl if isinstance(self.verify_ssl, str) else None
            )
            if self.client_cert:
                ssl.load_cert_chain(*self.client_cert)
        return aiohttp.ClientSession(
            headers=self._get_headers(),
            auth=auth,
            timeout=aiohttp.ClientTimeout(total=self.config.timeout),
            connector=aiohttp.TCPConnector(limit=getattr(self.config, "connection_pool_size", 100), ssl=ssl),
        )

    async def _get_async_session(self) -> Any:
        """Persistent session for this event loop (closed by :meth:`disconnect_async`)."""
        if not AIOHTTP_AVAILABLE:
            return None
        loop = asyncio.get_running_loop()
        if not self._has_live_async_session():
            self._async_session = self._new_async_session()
            self._async_loop = loop
        return self._async_session

    async def connect_async(self) -> bool:
        if not AIOHTTP_AVAILABLE:
            return await super().connect_async()
        url = f"{self.base_url}{self.health_path}"
        try:
            session = await self._get_async_session()
            async with session.get(url) as response:
                status = response.status
        except Exception as e:
            self._update_state(ConnectionState.FAILED, f"{type(e).__name__}: {e}")
            return False
        if status >= 500 or status == 401:
            self._update_state(ConnectionState.FAILED, f"HTTP {status} from {url}")
            return False
        self._update_state(ConnectionState.CONNECTED)
        return True

    async def disconnect_async(self) -> None:
        if self._async_session is not None and not self._async_session.closed:
            await self._async_session.close()
        self._async_session = None
        self.disconnect()

    async def send_async(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Native async request (falls back to a worker thread without aiohttp)."""
        if not AIOHTTP_AVAILABLE:
            return await super().send_async(payload)
        method, path, body = self._split(payload)
        url = f"{self.base_url}{path}"
        start = time.monotonic()
        kwargs: Dict[str, Any] = {}
        if method in ("GET", "DELETE"):
            if body:
                kwargs["params"] = {k: str(v) for k, v in body.items()}
        else:
            kwargs["json"] = body
        try:
            if self._has_live_async_session():
                async with self._async_session.request(method, url, **kwargs) as response:
                    status = response.status
                    text = await response.text()
            else:
                # No persistent session (connect_async not called on this loop):
                # use a short-lived one so nothing leaks when the loop ends.
                session = self._new_async_session()
                async with session, session.request(method, url, **kwargs) as response:
                    status = response.status
                    text = await response.text()
        except Exception as e:
            self._record_error(str(e))
            raise ProtocolError(f"HTTP request failed: {e}", "http", url, e) from e

        if status in (401, 403):
            self._record_error(f"HTTP {status}")
            raise AuthenticationError(f"HTTP {status} from {url}", details={"url": url, "status": status})
        if status >= 400:
            self._record_error(f"HTTP {status}")
            raise ProtocolError(f"HTTP {status}: {text[:200]}", "http", url, status=status)
        self._record_success((time.monotonic() - start) * 1000)
        if not text:
            return {}
        import json

        try:
            data = json.loads(text)
        except ValueError as e:
            raise ProtocolError(f"Invalid JSON response from {url}: {e}", "http", url, e) from e
        return data if isinstance(data, dict) else {"value": data}
