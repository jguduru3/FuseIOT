"""Brightness-controllable lights."""
from typing import Any, Dict, Optional

from ..result import CommandResult
from ..types import Brightness
from .base import Capability
from .switchable import _as_bool


class Dimmable(Capability):
    """
    On/off plus brightness (0-100).

    Device contract:
        GET  /state      -> {"power": bool, "brightness": 0-100}
        POST /on         -> {"power": true, "brightness"?: int}
        POST /off
        POST /brightness -> {"brightness": int}
        POST /fade       -> {"brightness": int, "duration_ms": int}
    """

    commands = ("on", "off", "toggle", "set_brightness", "fade_to")

    def __init__(
        self,
        protocol: Any,
        cache: Any,
        config: Any = None,
        min_brightness: int = 0,
        max_brightness: int = 100,
        **kwargs: Any,
    ):
        super().__init__(protocol, cache, config, **kwargs)
        self.min_brightness = min_brightness
        self.max_brightness = max_brightness

    @property
    def category(self) -> str:
        return "dimmable"

    def _clamp_brightness(self, value: Brightness) -> int:
        """Clamp to range. Floats in [0.0, 1.0] are treated as fractions."""
        if isinstance(value, float) and 0.0 <= value <= 1.0:
            value = value * 100
        return int(max(self.min_brightness, min(self.max_brightness, round(value))))

    def _fetch_state(self) -> Dict[str, Any]:
        response = self.protocol.send({"_method": "GET", "_path": "/state"})
        if not isinstance(response, dict):
            response = {}
        return {
            "power": _as_bool(response.get("power", response.get("state", False))),
            "brightness": int(response.get("brightness", 0) or 0),
        }

    def on(
        self,
        brightness: Optional[Brightness] = None,
        confirm: Optional[bool] = None,
        timeout: Optional[float] = None,
    ) -> CommandResult:
        """Turn on, optionally at a given brightness."""
        payload: Dict[str, Any] = {"_method": "POST", "_path": "/on", "power": True}
        expected: Dict[str, Any] = {"power": True}
        if brightness is not None:
            clamped = self._clamp_brightness(brightness)
            payload["brightness"] = clamped
            expected["brightness"] = clamped
        return self._send_and_confirm(payload, expected, timeout, "on", confirm)

    def off(self, confirm: Optional[bool] = None, timeout: Optional[float] = None) -> CommandResult:
        return self._send_and_confirm(
            {"_method": "POST", "_path": "/off", "power": False},
            {"power": False},
            timeout, "off", confirm,
        )

    def toggle(self, confirm: Optional[bool] = None, timeout: Optional[float] = None) -> CommandResult:
        if self.read_state(fresh=True).get("power"):
            return self.off(confirm=confirm, timeout=timeout)
        return self.on(confirm=confirm, timeout=timeout)

    def set_brightness(
        self,
        brightness: Brightness,
        confirm: Optional[bool] = None,
        timeout: Optional[float] = None,
    ) -> CommandResult:
        """Set brightness without changing power state."""
        clamped = self._clamp_brightness(brightness)
        return self._send_and_confirm(
            {"_method": "POST", "_path": "/brightness", "brightness": clamped},
            {"brightness": clamped},
            timeout, "set_brightness", confirm,
        )

    def fade_to(
        self,
        brightness: Brightness,
        duration_ms: int = 1000,
        confirm: Optional[bool] = None,
        timeout: Optional[float] = None,
    ) -> CommandResult:
        """Fade to a brightness over ``duration_ms``; the timeout covers the fade."""
        clamped = self._clamp_brightness(brightness)
        if timeout is None:
            timeout = self.config.timeout + duration_ms / 1000.0
        return self._send_and_confirm(
            {"_method": "POST", "_path": "/fade", "brightness": clamped, "duration_ms": duration_ms},
            {"brightness": clamped},
            timeout, "fade_to", confirm,
        )

    @property
    def brightness(self) -> int:
        return int(self._state_value("brightness", 0))

    @property
    def is_on(self) -> bool:
        return bool(self._state_value("power", False))
