"""Shared test doubles: stub protocols, a fake paho-mqtt client/broker, a fake serial port
and a local WebSocket device server."""
import asyncio
import json
import threading
from typing import Any, Callable, Dict, List, Optional

import paho.mqtt.client as paho

from fuseiot.capabilities.base import CapabilityConfig
from fuseiot.exceptions import ProtocolError
from fuseiot.protocols.base import ConnectionState, Protocol
from fuseiot.simulation.virtual_device import VirtualDevice


def fast_config(**overrides: Any) -> CapabilityConfig:
    """A CapabilityConfig tuned for quick tests (no retry back-off, fast polling)."""
    values: Dict[str, Any] = dict(timeout=1.0, retry_attempts=1, retry_delay=0.0, poll_interval=0.01)
    values.update(overrides)
    return CapabilityConfig(**values)


def start_fast(owner: Any, httpd: Any) -> None:
    """``owner.start()`` with a short shutdown poll interval (faster teardown)."""
    owner.start(poll_interval=0.02)


# --------------------------------------------------------------------------- protocols


class StubProtocol(Protocol):
    """Protocol whose responses are produced by a callable; records every payload."""

    def __init__(self, responder: Optional[Callable[[Dict[str, Any]], Any]] = None,
                 connect_ok: bool = True):
        super().__init__()
        self.responder = responder or (lambda payload: {})
        self.connect_ok = connect_ok
        self.sent: List[Dict[str, Any]] = []
        self.connect_calls = 0
        self.disconnect_calls = 0

    def connect(self) -> bool:
        self.connect_calls += 1
        if self.connect_ok:
            self._update_state(ConnectionState.CONNECTED)
            return True
        self._update_state(ConnectionState.FAILED, "stub unreachable")
        return False

    def disconnect(self) -> None:
        self.disconnect_calls += 1
        self._update_state(ConnectionState.DISCONNECTED)

    def send(self, payload: Dict[str, Any]) -> Any:
        self.sent.append(dict(payload))
        return self.responder(payload)


class FlakyVirtualDevice(VirtualDevice):
    """VirtualDevice whose next ``fail_sends`` sends (or GETs) raise ProtocolError."""

    def __init__(self, *args: Any, fail_sends: int = 0, fail_gets: int = 0, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self.fail_sends = fail_sends
        self.fail_gets = fail_gets

    def send(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        is_get = str(payload.get("_method", "POST")).upper() == "GET"
        if is_get and self.fail_gets > 0:
            self.fail_gets -= 1
            raise ProtocolError("flaky read", "virtual", self.endpoint)
        if not is_get and self.fail_sends > 0:
            self.fail_sends -= 1
            raise ProtocolError("flaky send", "virtual", self.endpoint)
        return super().send(payload)


# --------------------------------------------------------------------------- MQTT fakes


class FakeMessage:
    def __init__(self, topic: str, payload: bytes, retain: bool = False):
        self.topic = topic
        self.payload = payload
        self.retain = retain
        self.qos = 0


class FakePublishInfo:
    def __init__(self, published: bool = True):
        self._published = published
        self.waited = False

    def wait_for_publish(self, timeout: Optional[float] = None) -> None:
        self.waited = True

    def is_published(self) -> bool:
        return self._published


def _to_bytes(payload: Any) -> bytes:
    if payload is None:
        return b""
    if isinstance(payload, bytes):
        return payload
    return str(payload).encode("utf-8")


class FakeBroker:
    """In-memory MQTT broker with retained messages and device hooks."""

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.clients: List[FakeClient] = []
        self.retained: Dict[str, bytes] = {}
        self.log: List[Any] = []  # (topic, payload bytes, retain)
        self.devices: List[Callable[[FakeBroker, str, bytes], None]] = []
        self.connack_rc = 0
        self.refuse_connect = False
        self.ack_publishes = True
        self.client_cls: Any = None

    def publish(self, topic: str, payload: Any, retain: bool = False) -> None:
        data = _to_bytes(payload)
        with self.lock:
            self.log.append((topic, data, retain))
            if retain:
                self.retained[topic] = data
            targets = [c for c in self.clients if c.matches(topic)]
        for client in targets:
            client.deliver(FakeMessage(topic, data, retain))
        for device in list(self.devices):
            device(self, topic, data)

    def messages(self, topic: str) -> List[Any]:
        out = []
        for t, data, _ in self.log:
            if t == topic:
                try:
                    out.append(json.loads(data) if data else None)
                except ValueError:
                    out.append(data.decode())
        return out

    @property
    def instances(self) -> List["FakeClient"]:
        return self.client_cls.instances


class FakeClient:
    """Stand-in for ``paho.mqtt.client.Client`` (paho 2.x callback signatures)."""

    broker: FakeBroker = None  # type: ignore[assignment]
    instances: List["FakeClient"] = []

    def __init__(self, client_id: str = "", callback_api_version: Any = None, **kwargs: Any):
        self.client_id = client_id
        self.callback_api_version = callback_api_version
        self.on_connect: Any = None
        self.on_disconnect: Any = None
        self.on_message: Any = None
        self.subscriptions: Dict[str, int] = {}
        self.credentials: Any = None
        self.tls_args: Any = None
        self.tls_insecure: Optional[bool] = None
        self.reconnect_delay: Any = None
        self.connect_args: Any = None
        self.connected = False
        self.loop_running = False
        type(self).instances.append(self)

    def username_pw_set(self, username: str, password: Optional[str] = None) -> None:
        self.credentials = (username, password)

    def reconnect_delay_set(self, min_delay: int = 1, max_delay: int = 120) -> None:
        self.reconnect_delay = (min_delay, max_delay)

    def tls_set(self, **kwargs: Any) -> None:
        self.tls_args = kwargs

    def tls_insecure_set(self, value: bool) -> None:
        self.tls_insecure = value

    def connect(self, host: str, port: int = 1883, keepalive: int = 60) -> int:
        if self.broker.refuse_connect:
            raise ConnectionRefusedError("broker refused TCP connection")
        self.connect_args = (host, port, keepalive)
        return 0

    def loop_start(self) -> None:
        self.loop_running = True
        rc = self.broker.connack_rc
        if rc == 0:
            self.connected = True
            with self.broker.lock:
                self.broker.clients.append(self)
        if self.on_connect:
            self.on_connect(self, None, {}, rc, None)

    def loop_stop(self) -> None:
        self.loop_running = False

    def _detach(self) -> None:
        self.connected = False
        with self.broker.lock:
            if self in self.broker.clients:
                self.broker.clients.remove(self)

    def disconnect(self) -> None:
        self._detach()
        if self.on_disconnect:
            self.on_disconnect(self, None, {}, 0, None)

    def drop(self, rc: int = 7) -> None:
        """Simulate an unexpected network drop."""
        self._detach()
        if self.on_disconnect:
            self.on_disconnect(self, None, {}, rc, None)

    def subscribe(self, topic: str, qos: int = 0) -> Any:
        self.subscriptions[topic] = qos
        with self.broker.lock:
            retained = [(t, p) for t, p in self.broker.retained.items() if paho.topic_matches_sub(topic, t)]
        for t, p in retained:
            self.deliver(FakeMessage(t, p, True))
        return (0, 1)

    def unsubscribe(self, topic: str) -> Any:
        self.subscriptions.pop(topic, None)
        return (0, 1)

    def matches(self, topic: str) -> bool:
        return self.connected and any(paho.topic_matches_sub(s, topic) for s in self.subscriptions)

    def deliver(self, msg: FakeMessage) -> None:
        if self.on_message:
            self.on_message(self, None, msg)

    def publish(self, topic: str, payload: Any = None, qos: int = 0, retain: bool = False) -> FakePublishInfo:
        self.broker.publish(topic, payload, retain)
        return FakePublishInfo(self.broker.ack_publishes)


class MQTTDevice:
    """A device on the fake broker following the FuseIoT MQTT topic convention."""

    def __init__(self, prefix: str, device_type: str = "switchable", retain: bool = True,
                 **sim: Any):
        self.prefix = prefix
        self.retain = retain
        self.virtual = VirtualDevice(device_type, **sim)

    def __call__(self, broker: FakeBroker, topic: str, payload: bytes) -> None:
        set_prefix = self.prefix + "/set/"
        if topic.startswith(set_prefix):
            body = json.loads(payload) if payload else {}
            self.virtual.send({"_method": "POST", "_path": "/" + topic[len(set_prefix):], **body})
            self.publish_state(broker)
        elif topic.startswith(self.prefix + "/") and topic.endswith("/get"):
            path = topic[len(self.prefix):-len("/get")]
            state = self.virtual.send({"_method": "GET", "_path": path})
            broker.publish(self.prefix + path, json.dumps(state))

    def publish_state(self, broker: FakeBroker) -> None:
        broker.publish(self.prefix + "/state", json.dumps(self.virtual.state), retain=self.retain)


# --------------------------------------------------------------------------- serial fake


class FakeSerialPort:
    """pyserial-like port; ``responder(request_dict) -> bytes`` produces the reply line."""

    def __init__(self, responder: Callable[[Dict[str, Any]], bytes]):
        self.is_open = True
        self.written: List[bytes] = []
        self.responder = responder
        self._pending = b""
        self.flushed = 0
        self.resets = 0
        self.fail_write = False

    def reset_input_buffer(self) -> None:
        self.resets += 1

    def write(self, data: bytes) -> int:
        if self.fail_write:
            raise OSError("device unplugged")
        self.written.append(data)
        self._pending = self.responder(json.loads(data.decode("utf-8")))
        return len(data)

    def flush(self) -> None:
        self.flushed += 1

    def readline(self) -> bytes:
        out, self._pending = self._pending, b""
        return out

    def close(self) -> None:
        self.is_open = False

    def requests(self) -> List[Dict[str, Any]]:
        return [json.loads(w.decode("utf-8")) for w in self.written]


def virtual_serial_responder(device: VirtualDevice) -> Callable[[Dict[str, Any]], bytes]:
    """Answer serial JSON-line requests using a VirtualDevice."""

    def respond(request: Dict[str, Any]) -> bytes:
        payload = {k: v for k, v in request.items() if k not in ("method", "path")}
        payload["_method"] = request.get("method", "POST")
        payload["_path"] = request.get("path", "/")
        return (json.dumps(device.send(payload)) + "\n").encode("utf-8")

    return respond


# --------------------------------------------------------------------------- websocket server


class WebSocketDeviceServer:
    """A websockets server on 127.0.0.1 answering JSON requests via a VirtualDevice.

    Special ``_path`` values: ``/silent`` (never answer), ``/close`` (close the
    socket), ``/noid`` (answer without ``id``), ``/push`` (send an unsolicited
    message before the answer).
    """

    def __init__(self, device_type: str = "switchable"):
        self.device = VirtualDevice(device_type)
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True, name="test-ws-server")
        self.server: Any = None
        self.port = 0
        self.received: List[Dict[str, Any]] = []

    async def _handler(self, ws: Any) -> None:
        async for raw in ws:
            msg = json.loads(raw)
            self.received.append(msg)
            mid = msg.get("id")
            path = msg.get("_path")
            if path == "/silent":
                continue
            if path == "/close":
                await ws.close()
                return
            if path == "/noid":
                await ws.send(json.dumps({"ok": True, "noid": True}))
                continue
            if path == "/push":
                await ws.send(json.dumps({"id": "unsolicited", "pushed": True}))
                await ws.send(json.dumps({"id": mid, "ok": True}))
                continue
            payload = {k: v for k, v in msg.items() if k != "id"}
            result = self.device.send(payload)
            await ws.send(json.dumps({**result, "id": mid}))

    async def _start(self) -> None:
        import websockets

        self.server = await websockets.serve(self._handler, "127.0.0.1", 0)
        self.port = list(self.server.sockets)[0].getsockname()[1]

    async def _stop(self) -> None:
        self.server.close()
        await self.server.wait_closed()

    @property
    def uri(self) -> str:
        return f"ws://127.0.0.1:{self.port}"

    def start(self) -> "WebSocketDeviceServer":
        self.thread.start()
        asyncio.run_coroutine_threadsafe(self._start(), self.loop).result(timeout=10)
        return self

    def stop(self) -> None:
        try:
            asyncio.run_coroutine_threadsafe(self._stop(), self.loop).result(timeout=10)
        finally:
            self.loop.call_soon_threadsafe(self.loop.stop)
            self.thread.join(timeout=10)
            self.loop.close()


# --------------------------------------------------------------------------- polling


def wait_for(predicate: Callable[[], Any], timeout: float = 10.0, interval: float = 0.01) -> Any:
    """Poll ``predicate`` until it returns a truthy value (returned) or fail after ``timeout``."""
    import time

    deadline = time.monotonic() + timeout
    while True:
        value = predicate()
        if value:
            return value
        if time.monotonic() > deadline:
            raise AssertionError(f"condition not met within {timeout}s")
        time.sleep(interval)
