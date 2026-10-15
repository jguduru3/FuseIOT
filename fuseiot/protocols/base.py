"""Transport protocol base classes."""
import asyncio
import functools
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Optional

from fuseiot.logging_config import get_logger

logger = get_logger("protocols.base")


class ConnectionState(Enum):
    """Protocol connection states."""
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    RECONNECTING = "reconnecting"
    FAILED = "failed"


@dataclass
class ProtocolConfig:
    """Common protocol configuration."""

    timeout: float = 5.0
    retry_attempts: int = 3
    connection_check_interval: Optional[float] = None
    keepalive: bool = True
    keepalive_interval: float = 30.0
    auto_reconnect: bool = True
    reconnect_delay: float = 1.0
    max_reconnect_delay: float = 60.0
    compression: bool = False
    metrics_enabled: bool = True


class Protocol(ABC):
    """
    Abstract base for transports.

    Subclasses implement :meth:`connect`, :meth:`disconnect` and :meth:`send`.
    ``send`` takes a payload dict (see the HTTP module for the convention),
    blocks, returns the device's response dict, and raises
    :class:`~fuseiot.exceptions.ProtocolError` on failure.
    """

    def __init__(self, config: Optional[ProtocolConfig] = None):
        self.config = config or ProtocolConfig()
        self._conn_state = ConnectionState.DISCONNECTED
        self._last_error: Optional[str] = None
        self._connection_time: Optional[float] = None
        self._metrics_lock = threading.Lock()
        self._metrics: Dict[str, Any] = {
            "messages_sent": 0,
            "messages_received": 0,
            "errors": 0,
            "latency_ms_total": 0.0,
        }

    # ``_state`` kept as an alias for backwards compatibility.
    @property
    def _state(self) -> ConnectionState:
        return self._conn_state

    @_state.setter
    def _state(self, value: ConnectionState) -> None:
        self._conn_state = value

    @property
    def name(self) -> str:
        return self.__class__.__name__.lower()

    @property
    def endpoint(self) -> str:
        return self.name

    @property
    def is_connected(self) -> bool:
        return self._conn_state == ConnectionState.CONNECTED

    @property
    def connection_state(self) -> ConnectionState:
        return self._conn_state

    @property
    def last_error(self) -> Optional[str]:
        return self._last_error

    @property
    def uptime_seconds(self) -> Optional[float]:
        if self._connection_time and self.is_connected:
            return time.time() - self._connection_time
        return None

    @abstractmethod
    def connect(self) -> bool:
        """Establish the connection. Return True on success; never raise for network errors."""

    @abstractmethod
    def disconnect(self) -> None:
        """Close the connection."""

    @abstractmethod
    def send(self, payload: Dict[str, Any]) -> Any:
        """Send a payload and return the response (blocking)."""

    async def send_async(self, payload: Dict[str, Any]) -> Any:
        """Async send; default runs :meth:`send` in a worker thread."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, functools.partial(self.send, payload))

    async def connect_async(self) -> bool:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self.connect)

    async def disconnect_async(self) -> None:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self.disconnect)

    def health_check(self) -> Dict[str, Any]:
        with self._metrics_lock:
            metrics = dict(self._metrics)
        sent = metrics["messages_sent"]
        metrics["avg_latency_ms"] = round(metrics["latency_ms_total"] / sent, 2) if sent else 0.0
        return {
            "connected": self.is_connected,
            "state": self._conn_state.value,
            "last_error": self._last_error,
            "protocol": self.name,
            "endpoint": self.endpoint,
            "uptime_seconds": self.uptime_seconds,
            "metrics": metrics,
        }

    def _record_success(self, latency_ms: float) -> None:
        with self._metrics_lock:
            self._metrics["messages_sent"] += 1
            self._metrics["messages_received"] += 1
            self._metrics["latency_ms_total"] += latency_ms

    def _record_error(self, error: str) -> None:
        self._last_error = error
        with self._metrics_lock:
            self._metrics["errors"] += 1

    def _update_state(self, new_state: ConnectionState, error: Optional[str] = None) -> None:
        old_state = self._conn_state
        self._conn_state = new_state
        if error:
            self._record_error(error)

        if new_state == ConnectionState.CONNECTED and old_state != ConnectionState.CONNECTED:
            self._connection_time = time.time()
            logger.info("protocol_connected", protocol=self.name, endpoint=self.endpoint)
        elif old_state == ConnectionState.CONNECTED and new_state != ConnectionState.CONNECTED:
            log = logger.warning if error else logger.info
            log("protocol_disconnected", protocol=self.name, endpoint=self.endpoint,
                new_state=new_state.value, error=error)
        elif error:
            logger.warning("protocol_error", protocol=self.name, endpoint=self.endpoint, error=error)

    def __enter__(self) -> "Protocol":
        self.connect()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.disconnect()

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self.endpoint} {self._conn_state.value}>"


class AsyncProtocol(Protocol, ABC):
    """Protocol with a native async implementation of :meth:`send_async`."""
