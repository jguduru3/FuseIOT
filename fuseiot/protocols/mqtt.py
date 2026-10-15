"""
MQTT transport.

Topic convention (``topic_prefix`` is the device's base topic, e.g. ``home/plug1``):

* State reads (``_method: GET``, ``_path: /state``):
  the device publishes its state as JSON to ``<prefix>/state`` (ideally
  *retained*). FuseIoT subscribes to ``<prefix>/#`` and returns the newest
  message on that topic. If none has arrived yet, it publishes an empty
  message to ``<prefix>/state/get`` and waits for a reply.
* Commands (``_method: POST``, ``_path: /on``):
  the JSON body is published to ``<prefix>/set/on``.

Explicit ``_topic`` / ``_response_topic`` keys override the convention.
"""
import json
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional, Tuple

from ..exceptions import ProtocolError
from ..logging_config import get_logger
from .base import ConnectionState, Protocol, ProtocolConfig

logger = get_logger("protocols.mqtt")

try:
    import paho.mqtt.client as mqtt

    PAHO_AVAILABLE = True
except ImportError:  # pragma: no cover - depends on environment
    mqtt = None  # type: ignore[assignment]
    PAHO_AVAILABLE = False


@dataclass
class MQTTConfig(ProtocolConfig):
    """MQTT-specific configuration."""
    broker: str = "localhost"
    port: int = 1883
    client_id: Optional[str] = None
    username: Optional[str] = None
    password: Optional[str] = None
    topic_prefix: str = "device"
    qos: int = 1


Handler = Callable[[str, Any], None]


class MQTT(Protocol):
    """
    MQTT device transport (paho-mqtt 1.x and 2.x).

    Args:
        broker: Broker hostname.
        port: Broker port (1883 plain, 8883 TLS).
        topic_prefix: Base topic of the device, e.g. ``home/plug1``.
        qos: Quality of service for publishes/subscriptions.
        timeout: Seconds to wait for connection and replies.
        keepalive: MQTT keepalive in seconds.
    """

    def __init__(
        self,
        broker: str,
        port: int = 1883,
        client_id: Optional[str] = None,
        username: Optional[str] = None,
        password: Optional[str] = None,
        topic_prefix: str = "device",
        qos: int = 1,
        timeout: float = 5.0,
        keepalive: int = 60,
        config: Optional[ProtocolConfig] = None,
    ):
        if not PAHO_AVAILABLE:
            raise ImportError("paho-mqtt is required for MQTT: pip install 'fuseiot[mqtt]'")
        super().__init__(config or MQTTConfig())
        self.broker = broker
        self.port = int(port)
        self.client_id = client_id or f"fuseiot-{uuid.uuid4().hex[:12]}"
        self.username = username
        self.password = password
        self.topic_prefix = topic_prefix.strip("/")
        self.qos = int(qos)
        self.config.timeout = float(timeout)
        self.keepalive = int(keepalive)

        self._client: Any = None
        self._connected = threading.Event()
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._latest: Dict[str, Tuple[float, Any]] = {}
        self._subscriptions: Dict[str, Handler] = {}

    # ---------------------------------------------------------------- basics

    @property
    def timeout(self) -> float:
        return self.config.timeout

    @property
    def scheme(self) -> str:
        return "mqtt"

    @property
    def endpoint(self) -> str:
        return f"{self.scheme}://{self.broker}:{self.port}/{self.topic_prefix}"

    def topic(self, suffix: str) -> str:
        suffix = suffix.strip("/")
        return f"{self.topic_prefix}/{suffix}" if suffix else self.topic_prefix

    def _make_client(self) -> Any:
        kwargs: Dict[str, Any] = {"client_id": self.client_id}
        if hasattr(mqtt, "CallbackAPIVersion"):  # paho-mqtt >= 2.0
            kwargs["callback_api_version"] = mqtt.CallbackAPIVersion.VERSION2
        client = mqtt.Client(**kwargs)
        if self.username:
            client.username_pw_set(self.username, self.password)
        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.on_message = self._on_message
        client.reconnect_delay_set(min_delay=1, max_delay=int(self.config.max_reconnect_delay))
        self._configure_client(client)
        return client

    def _configure_client(self, client: Any) -> None:
        """Hook for subclasses (TLS) to configure the client before connecting."""

    # ------------------------------------------------------------- callbacks

    @staticmethod
    def _rc_value(rc: Any) -> int:
        value = getattr(rc, "value", rc)
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0 if not getattr(rc, "is_failure", False) else 1

    def _on_connect(self, client: Any, userdata: Any, flags: Any, rc: Any, properties: Any = None) -> None:
        if self._rc_value(rc) == 0:
            client.subscribe(f"{self.topic_prefix}/#", qos=self.qos)
            for topic in self._subscriptions:
                if not topic.startswith(self.topic_prefix + "/"):
                    client.subscribe(topic, qos=self.qos)
            self._connected.set()
            self._update_state(ConnectionState.CONNECTED)
        else:
            self._connected.clear()
            self._update_state(ConnectionState.FAILED, f"MQTT connect refused: {rc}")

    def _on_disconnect(self, client: Any, userdata: Any, *args: Any) -> None:
        # paho 1.x: (client, userdata, rc); 2.x: (client, userdata, flags, rc, properties)
        rc = args[1] if len(args) >= 2 else (args[0] if args else 0)
        self._connected.clear()
        unexpected = self._rc_value(rc) != 0
        self._update_state(
            ConnectionState.RECONNECTING if unexpected else ConnectionState.DISCONNECTED,
            f"Unexpected disconnect: {rc}" if unexpected else None,
        )

    def _on_message(self, client: Any, userdata: Any, msg: Any) -> None:
        raw = msg.payload.decode("utf-8", errors="replace") if msg.payload else ""
        try:
            payload: Any = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            payload = {"raw": raw}
        with self._cond:
            self._latest[msg.topic] = (time.monotonic(), payload)
            self._cond.notify_all()
        for pattern, handler in list(self._subscriptions.items()):
            if mqtt.topic_matches_sub(pattern, msg.topic):
                try:
                    handler(msg.topic, payload)
                except Exception as e:
                    logger.error("mqtt_handler_error", topic=msg.topic, error=str(e))

    # ------------------------------------------------------------ lifecycle

    def connect(self) -> bool:
        if self._client is not None and self._connected.is_set():
            return True
        try:
            if self._client is None:
                self._client = self._make_client()
            self._update_state(ConnectionState.CONNECTING)
            self._client.connect(self.broker, self.port, keepalive=self.keepalive)
            self._client.loop_start()
        except Exception as e:
            self._update_state(ConnectionState.FAILED, f"{type(e).__name__}: {e}")
            return False
        if not self._connected.wait(self.config.timeout):
            self._update_state(ConnectionState.FAILED, "Timed out waiting for CONNACK")
            return False
        return True

    def disconnect(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            try:
                client.disconnect()
            finally:
                client.loop_stop()
        self._connected.clear()
        self._update_state(ConnectionState.DISCONNECTED)

    def subscribe(self, topic: str, handler: Handler) -> None:
        """Call ``handler(topic, payload)`` for messages on ``<prefix>/<topic>`` (wildcards OK)."""
        full = self.topic(topic)
        self._subscriptions[full] = handler
        if self._client is not None and self._connected.is_set():
            self._client.subscribe(full, qos=self.qos)

    # ------------------------------------------------------------- messaging

    def _require_connection(self) -> None:
        if (self._client is None or not self._connected.is_set()) and not self.connect():
            raise ProtocolError(f"Not connected: {self.last_error}", self.scheme, self.endpoint)

    def publish(self, topic: str, body: Any, retain: bool = False) -> None:
        """Publish JSON ``body`` to a full topic and wait for the broker ack (QoS>0)."""
        self._require_connection()
        data = body if isinstance(body, (str, bytes)) else json.dumps(body)
        info = self._client.publish(topic, data, qos=self.qos, retain=retain)
        if self.qos > 0:
            info.wait_for_publish(timeout=self.config.timeout)
            if not info.is_published():
                raise ProtocolError(f"Publish to {topic} not acknowledged", self.scheme, topic)

    def latest(self, topic: str) -> Optional[Any]:
        """Most recent payload received on a full topic, if any."""
        with self._lock:
            entry = self._latest.get(topic)
        return entry[1] if entry else None

    def _wait_for(self, topic: str, after: float, timeout: float) -> Any:
        deadline = time.monotonic() + timeout
        with self._cond:
            while True:
                entry = self._latest.get(topic)
                if entry and entry[0] >= after:
                    return entry[1]
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ProtocolError(f"No message on {topic} within {timeout}s",
                                        self.scheme, topic)
                self._cond.wait(remaining)

    def send(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        self._require_connection()
        start = time.monotonic()
        body = {k: v for k, v in payload.items() if not str(k).startswith("_")}
        method = str(payload.get("_method", "POST")).upper()
        path = str(payload.get("_path", "")).strip("/")
        try:
            if "_topic" in payload:
                topic = self.topic(str(payload["_topic"]))
                response_topic = payload.get("_response_topic")
                sent_at = time.monotonic()
                self.publish(topic, body)
                result: Any = {"published": True, "topic": topic}
                if response_topic:
                    result = self._wait_for(self.topic(str(response_topic)), sent_at, self.config.timeout)
            elif method == "GET":
                state_topic = self.topic(path or "state")
                cached = self.latest(state_topic)
                if cached is not None:
                    result = cached
                else:
                    sent_at = time.monotonic()
                    self.publish(f"{state_topic}/get", "")
                    result = self._wait_for(state_topic, sent_at, self.config.timeout)
            else:
                topic = self.topic(f"set/{path}" if path else "set")
                self.publish(topic, body)
                result = {"published": True, "topic": topic}
        except ProtocolError as e:
            self._record_error(str(e))
            raise
        except Exception as e:
            self._record_error(str(e))
            raise ProtocolError(f"MQTT operation failed: {e}", self.scheme, self.endpoint, e) from e

        self._record_success((time.monotonic() - start) * 1000)
        return result if isinstance(result, dict) else {"value": result}
