from dataclasses import dataclass
from datetime import datetime
from enum import Enum, auto
from typing import (
    Any,
    Callable,
    Dict,
    List,
    NewType,
    Optional,
    Protocol,
    Tuple,
    TypeVar,
    Union,
    runtime_checkable,
)

# Primitive type aliases
DeviceID = NewType("DeviceID", str)
Payload = Dict[str, Any]
Handler = Callable[..., Any]
T = TypeVar("T")


class CommandStatus(Enum):
    """Command lifecycle states."""
    PENDING = auto()
    SENT = auto()
    ACKNOWLEDGED = auto()
    CONFIRMED = auto()
    FAILED = auto()
    TIMEOUT = auto()
    CANCELLED = auto()
    RATE_LIMITED = auto()
    CIRCUIT_OPEN = auto()


@dataclass(frozen=True)
class StateVector:
    """Immutable snapshot of device state."""

    values: Dict[str, Any]
    timestamp: float
    source: str  # "command", "poll", "event", "cache", "inferred", "persistence"
    confidence: float  # 0.0 to 1.0
    sequence_number: Optional[int] = None  # For ordering events

    def get(self, key: str, default: Any = None) -> Any:
        """Safe value access."""
        return self.values.get(key, default)

    def __contains__(self, key: str) -> bool:
        """Key membership."""
        return key in self.values

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "values": self.values,
            "timestamp": self.timestamp,
            "source": self.source,
            "confidence": self.confidence,
            "sequence_number": self.sequence_number,
        }


# NEW: Color types for RGB lights

@dataclass(frozen=True)
class Color:
    """RGB color representation."""
    r: int  # 0-255
    g: int  # 0-255
    b: int  # 0-255

    def __post_init__(self) -> None:
        for name in ("r", "g", "b"):
            value = getattr(self, name)
            if not isinstance(value, int) or not 0 <= value <= 255:
                raise ValueError(f"Color.{name} must be an int in 0-255, got {value!r}")

    @classmethod
    def from_hex(cls, hex_color: str) -> "Color":
        """Create from hex string (#RRGGBB)."""
        hex_color = hex_color.strip().lstrip("#")
        if len(hex_color) != 6:
            raise ValueError(f"Expected #RRGGBB, got {hex_color!r}")
        return cls(
            r=int(hex_color[0:2], 16),
            g=int(hex_color[2:4], 16),
            b=int(hex_color[4:6], 16)
        )

    @classmethod
    def from_hsv(cls, h: float, s: float, v: float) -> "Color":
        """Create from hue (0-360), saturation (0-1) and value (0-1)."""
        import colorsys

        if not (0.0 <= s <= 1.0 and 0.0 <= v <= 1.0):
            raise ValueError("Saturation and value must be within 0.0-1.0")
        r, g, b = colorsys.hsv_to_rgb((h % 360) / 360.0, s, v)
        return cls(round(r * 255), round(g * 255), round(b * 255))

    def to_hex(self) -> str:
        """Convert to hex string."""
        return f"#{self.r:02x}{self.g:02x}{self.b:02x}"

    def to_hsv(self) -> Tuple[float, float, float]:
        """Convert to HSV tuple."""
        r, g, b = self.r / 255.0, self.g / 255.0, self.b / 255.0
        mx = max(r, g, b)
        mn = min(r, g, b)
        df = mx - mn
        if mx == mn:
            h = 0
        elif mx == r:
            h = (60 * ((g - b) / df) + 360) % 360
        elif mx == g:
            h = (60 * ((b - r) / df) + 120) % 360
        else:
            h = (60 * ((r - g) / df) + 240) % 360
        s = 0 if mx == 0 else df / mx
        v = mx
        return (h, s, v)


# Type aliases
Brightness = Union[int, float]  # 0-100 or 0.0-1.0
Position = Union[int, float]


# Runtime-checkable protocols

@runtime_checkable
class Readable(Protocol):
    """Protocol for readable capabilities."""

    def read(self, fresh: bool = False) -> StateVector:
        """Read current state."""
        ...

    async def read_async(self, fresh: bool = False) -> StateVector:
        """Async read current state."""
        ...


@runtime_checkable
class SwitchableProtocol(Protocol):
    """Protocol for on/off control."""

    def on(self, confirm: bool = True, timeout: float = 2.0) -> Any:
        """Turn device on."""
        ...

    def off(self, confirm: bool = True, timeout: float = 2.0) -> Any:
        """Turn device off."""
        ...

    def toggle(self, confirm: bool = True, timeout: float = 2.0) -> Any:
        """Toggle current state."""
        ...

    @property
    def is_on(self) -> bool:
        """Current power state."""
        ...


@runtime_checkable
class DimmableProtocol(SwitchableProtocol, Protocol):
    """Protocol for brightness control."""

    def set_brightness(self, brightness: Brightness, confirm: bool = True) -> Any:
        """Set brightness level."""
        ...

    @property
    def brightness(self) -> Brightness:
        """Current brightness."""
        ...


@runtime_checkable
class SensorProtocol(Protocol):
    """Protocol for sensor readings."""

    def read(self, fresh: bool = False) -> Dict[str, Any]:
        """Read measurement."""
        ...

    @property
    def value(self) -> float:
        """Current value."""
        ...


@runtime_checkable
class MotorProtocol(Protocol):
    """Protocol for position/speed control."""

    def move_to(self, position: Position, **kwargs) -> Any:
        """Move to absolute position."""
        ...

    def set_speed(self, speed: float) -> Any:
        """Set movement speed."""
        ...

    def stop(self) -> Any:
        """Emergency stop."""
        ...

    @property
    def position(self) -> Position:
        """Current position."""
        ...

    @property
    def is_moving(self) -> bool:
        """Movement status."""
        ...


@runtime_checkable
class LockProtocol(Protocol):
    """Protocol for lock control."""

    def lock(self, confirm: bool = True) -> Any:
        """Lock the device."""
        ...

    def unlock(self, confirm: bool = True) -> Any:
        """Unlock the device."""
        ...

    @property
    def is_locked(self) -> bool:
        """Lock status."""
        ...


@runtime_checkable
class RGBLightProtocol(DimmableProtocol, Protocol):
    """Protocol for RGB color control."""

    def set_color(self, color: Color, confirm: bool = True) -> Any:
        """Set RGB color."""
        ...

    def set_hsv(self, h: float, s: float, v: float, confirm: bool = True) -> Any:
        """Set HSV color."""
        ...

    @property
    def color(self) -> Color:
        """Current color."""
        ...


# Utility types

class HealthStatus(Enum):
    """Device health enumeration."""
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"
    UNKNOWN = "unknown"
    OFFLINE = "offline"


@dataclass
class DeviceInfo:
    """Static device information."""
    device_id: DeviceID
    category: str
    protocol: str
    endpoint: str
    capabilities: List[str]
    health: HealthStatus = HealthStatus.UNKNOWN
    metadata: Dict[str, Any] = None
    last_seen: Optional[datetime] = None
    firmware_version: Optional[str] = None

    def __post_init__(self):
        if self.metadata is None:
            self.metadata = {}


# Type variables for generics
CapabilityType = TypeVar("CapabilityType", bound="fuseiot.capabilities.base.Capability")  # noqa: F821
ProtocolType = TypeVar("ProtocolType", bound="fuseiot.protocols.base.Protocol")  # noqa: F821
