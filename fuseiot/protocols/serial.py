"""
Serial (RS232 / RS485 / USB-CDC) transport.

Line-delimited JSON: each request is one JSON object followed by ``\\n``; the
device answers with one JSON line. Payload metadata (``_method``, ``_path``)
is sent as ``"method"`` and ``"path"`` so firmware can route commands.
"""
import json
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

from ..exceptions import ProtocolError
from ..logging_config import get_logger
from .base import ConnectionState, Protocol, ProtocolConfig

try:
    import serial as pyserial

    SERIAL_AVAILABLE = True
except ImportError:  # pragma: no cover
    pyserial = None  # type: ignore[assignment]
    SERIAL_AVAILABLE = False

logger = get_logger("protocols.serial")


@dataclass
class SerialConfig(ProtocolConfig):
    """Serial-specific configuration."""
    port: str = "/dev/ttyUSB0"
    baudrate: int = 9600
    bytesize: int = 8
    parity: str = "N"
    stopbits: int = 1


class Serial(Protocol):
    """
    JSON-lines over a serial port.

    Args:
        port: Device path (``/dev/ttyUSB0``, ``COM3``) or a pyserial URL
            (``socket://host:port``, ``loop://`` for tests).
        baudrate, bytesize, parity, stopbits: Line settings.
        timeout: Read timeout per response in seconds.
    """

    def __init__(
        self,
        port: str = "/dev/ttyUSB0",
        baudrate: int = 9600,
        timeout: float = 5.0,
        bytesize: int = 8,
        parity: str = "N",
        stopbits: int = 1,
        config: Optional[SerialConfig] = None,
    ):
        if not SERIAL_AVAILABLE:
            raise ImportError("pyserial is required: pip install 'fuseiot[serial]'")
        super().__init__(config or SerialConfig())
        self.port = port
        self.baudrate = int(baudrate)
        self.bytesize = bytesize
        self.parity = parity
        self.stopbits = stopbits
        self.config.timeout = float(timeout)
        self._serial: Any = None
        self._io_lock = threading.Lock()

    @property
    def endpoint(self) -> str:
        return f"serial:{self.port}"

    def connect(self) -> bool:
        if self._serial is not None and self._serial.is_open:
            return True
        try:
            self._serial = pyserial.serial_for_url(
                self.port,
                baudrate=self.baudrate,
                bytesize=self.bytesize,
                parity=self.parity,
                stopbits=self.stopbits,
                timeout=self.config.timeout,
                write_timeout=self.config.timeout,
            )
        except Exception as e:
            self._update_state(ConnectionState.FAILED, f"{type(e).__name__}: {e}")
            return False
        self._update_state(ConnectionState.CONNECTED)
        return True

    def disconnect(self) -> None:
        if self._serial is not None:
            try:
                self._serial.close()
            finally:
                self._serial = None
        self._update_state(ConnectionState.DISCONNECTED)

    def send(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        if (self._serial is None or not self._serial.is_open) and not self.connect():
            raise ProtocolError(f"Not connected: {self.last_error}", "serial", self.port)

        message = {k: v for k, v in payload.items() if not str(k).startswith("_")}
        if "_method" in payload:
            message["method"] = payload["_method"]
        if "_path" in payload:
            message["path"] = payload["_path"]

        start = time.monotonic()
        with self._io_lock:
            try:
                self._serial.reset_input_buffer()
                self._serial.write((json.dumps(message) + "\n").encode("utf-8"))
                self._serial.flush()
                line = self._serial.readline().decode("utf-8", errors="replace").strip()
            except Exception as e:
                self._update_state(ConnectionState.FAILED, str(e))  # records the error once
                raise ProtocolError(f"Serial I/O error: {e}", "serial", self.port, e) from e

        if not line:
            self._record_error("No response")
            raise ProtocolError(f"No response within {self.config.timeout}s", "serial", self.port)
        try:
            data = json.loads(line)
        except json.JSONDecodeError as e:
            self._record_error(str(e))
            raise ProtocolError(f"Invalid JSON response: {line[:100]!r}", "serial", self.port, e) from e
        self._record_success((time.monotonic() - start) * 1000)
        return data if isinstance(data, dict) else {"value": data}
