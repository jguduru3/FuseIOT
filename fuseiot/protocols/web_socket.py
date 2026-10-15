"""
WebSocket transport.

Request/response over one socket: each request is sent as JSON with an ``id``
field; the device should echo the same ``id`` in its reply. Replies without
an ``id`` are matched to the oldest outstanding request. Messages that arrive
without a pending request are kept as the latest *push* state.

The connection runs on a private background event loop, so the protocol can
be used from synchronous code and from any asyncio loop.
"""
import asyncio
import itertools
import json
import threading
import time
from collections import OrderedDict
from concurrent.futures import Future
from dataclasses import dataclass
from typing import Any, Dict, Optional

from ..exceptions import ProtocolError
from ..logging_config import get_logger
from .base import AsyncProtocol, ConnectionState, ProtocolConfig

try:
    import websockets

    WEBSOCKETS_AVAILABLE = True
except ImportError:  # pragma: no cover
    websockets = None  # type: ignore[assignment]
    WEBSOCKETS_AVAILABLE = False

logger = get_logger("protocols.websocket")


@dataclass
class WebSocketConfig(ProtocolConfig):
    """WebSocket-specific configuration."""
    uri: str = ""
    ping_interval: float = 20.0
    ping_timeout: float = 10.0


class WebSocket(AsyncProtocol):
    """JSON request/response over WebSocket (``ws://`` or ``wss://``)."""

    def __init__(
        self,
        uri: str,
        ping_interval: float = 20.0,
        ping_timeout: float = 10.0,
        timeout: Optional[float] = None,
        headers: Optional[Dict[str, str]] = None,
        config: Optional[WebSocketConfig] = None,
    ):
        if not WEBSOCKETS_AVAILABLE:
            raise ImportError("websockets is required: pip install 'fuseiot[websocket]'")
        super().__init__(config or WebSocketConfig())
        self.uri = uri
        self.ping_interval = ping_interval
        self.ping_timeout = ping_timeout
        self.headers = headers or {}
        if timeout is not None:
            self.config.timeout = float(timeout)

        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._ws: Any = None
        self._reader: Optional[asyncio.Task[None]] = None
        self._pending: OrderedDict[str, asyncio.Future[Any]] = OrderedDict()
        self._ids = itertools.count(1)
        self._send_lock: Optional[asyncio.Lock] = None
        self.latest_push: Optional[Dict[str, Any]] = None

    @property
    def endpoint(self) -> str:
        return self.uri

    # ------------------------------------------------------ background loop

    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        if self._loop is None or self._loop.is_closed():
            loop = asyncio.new_event_loop()
            thread = threading.Thread(target=loop.run_forever, name="fuseiot-ws", daemon=True)
            thread.start()
            self._loop, self._thread = loop, thread
        return self._loop

    def _run(self, coro: Any, timeout: Optional[float] = None) -> Any:
        future: Future = asyncio.run_coroutine_threadsafe(coro, self._ensure_loop())
        return future.result(timeout=timeout)

    async def _bridge(self, coro: Any) -> Any:
        future = asyncio.run_coroutine_threadsafe(coro, self._ensure_loop())
        return await asyncio.wrap_future(future)

    # ------------------------------------------------------- loop-side impl

    async def _connect_impl(self) -> bool:
        if self._ws is not None:
            return True
        self._update_state(ConnectionState.CONNECTING)
        try:
            kwargs: Dict[str, Any] = {"ping_interval": self.ping_interval,
                                      "ping_timeout": self.ping_timeout,
                                      "open_timeout": self.config.timeout}
            if self.headers:
                kwargs["additional_headers"] = self.headers
            try:
                self._ws = await websockets.connect(self.uri, **kwargs)
            except TypeError:  # websockets < 13 uses extra_headers
                if "additional_headers" in kwargs:
                    kwargs["extra_headers"] = kwargs.pop("additional_headers")
                self._ws = await websockets.connect(self.uri, **kwargs)
        except Exception as e:
            self._update_state(ConnectionState.FAILED, f"{type(e).__name__}: {e}")
            return False
        self._send_lock = asyncio.Lock()
        self._reader = asyncio.get_running_loop().create_task(self._read_loop())
        self._update_state(ConnectionState.CONNECTED)
        return True

    async def _read_loop(self) -> None:
        ws = self._ws
        try:
            async for message in ws:
                try:
                    data = json.loads(message)
                except (TypeError, json.JSONDecodeError):
                    logger.warning("websocket_invalid_json", message=str(message)[:100])
                    continue
                msg_id = str(data.get("id")) if isinstance(data, dict) and "id" in data else None
                if msg_id and msg_id in self._pending:
                    fut = self._pending.pop(msg_id)
                elif self._pending and msg_id is None:
                    _, fut = self._pending.popitem(last=False)
                else:
                    self.latest_push = data if isinstance(data, dict) else {"value": data}
                    continue
                if not fut.done():
                    fut.set_result(data)
        except Exception as e:
            self._record_error(str(e))
        finally:
            self._ws = None
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(ProtocolError("Connection closed", "websocket", self.uri))
            self._pending.clear()
            if self._conn_state == ConnectionState.CONNECTED:
                self._update_state(ConnectionState.DISCONNECTED, "Connection closed by peer")

    async def _send_impl(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        if self._ws is None and not await self._connect_impl():
            raise ProtocolError(f"Not connected: {self.last_error}", "websocket", self.uri)
        msg_id = str(next(self._ids))
        message = {k: v for k, v in payload.items()}
        message["id"] = msg_id
        fut: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[msg_id] = fut
        start = time.monotonic()
        try:
            assert self._send_lock is not None
            async with self._send_lock:
                await self._ws.send(json.dumps(message))
            data = await asyncio.wait_for(fut, timeout=self.config.timeout)
        except asyncio.TimeoutError:
            self._pending.pop(msg_id, None)
            self._record_error("Response timeout")
            raise ProtocolError("Response timeout", "websocket", self.uri) from None
        except ProtocolError:
            raise
        except Exception as e:
            self._pending.pop(msg_id, None)
            self._record_error(str(e))
            raise ProtocolError(f"WebSocket send failed: {e}", "websocket", self.uri, e) from e
        self._record_success((time.monotonic() - start) * 1000)
        if isinstance(data, dict):
            data = {k: v for k, v in data.items() if k != "id"}
            return data
        return {"value": data}

    async def _disconnect_impl(self) -> None:
        ws, self._ws = self._ws, None
        if ws is not None:
            await ws.close()
        if self._reader is not None:
            self._reader.cancel()
            self._reader = None
        self._update_state(ConnectionState.DISCONNECTED)

    # ----------------------------------------------------------- public API

    def connect(self) -> bool:
        return bool(self._run(self._connect_impl(), timeout=self.config.timeout + 5))

    def send(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self._run(self._send_impl(payload), timeout=self.config.timeout + 5)

    def disconnect(self) -> None:
        if self._loop is None or self._loop.is_closed():
            self._update_state(ConnectionState.DISCONNECTED)
            return
        try:
            self._run(self._disconnect_impl(), timeout=5)
        finally:
            loop, self._loop = self._loop, None
            loop.call_soon_threadsafe(loop.stop)
            if self._thread is not None:
                self._thread.join(timeout=5)
            loop.close()

    async def connect_async(self) -> bool:
        return bool(await self._bridge(self._connect_impl()))

    async def send_async(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return await self._bridge(self._send_impl(payload))

    async def disconnect_async(self) -> None:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self.disconnect)
