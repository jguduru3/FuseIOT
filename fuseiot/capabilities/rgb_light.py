"""RGB / HSV colour lights with effects."""
import colorsys
from typing import Any, Dict, Optional

from ..result import CommandResult
from ..types import Brightness, Color
from .base import Capability
from .switchable import _as_bool


class ColorMode:
    RGB = "rgb"
    HSV = "hsv"
    TEMPERATURE = "temperature"
    EFFECT = "effect"


class RGBLight(Capability):
    """
    Colour light.

    Device contract:
        GET  /state      -> {"power": bool, "brightness": 0-100, "r","g","b": 0-255,
                             "color_mode"?: str, "effect"?: str}
        POST /on         -> {"power": true, "r"?, "g"?, "b"?, "brightness"?}
        POST /off
        POST /color      -> {"r","g","b", "brightness"?}
        POST /brightness -> {"brightness": int}
        POST /effect     -> {"effect": str, "speed": int}

    ``set_hsv`` is converted to RGB client-side so devices only need RGB.
    """

    commands = ("on", "off", "toggle", "set_color", "set_hex", "set_hsv",
                "set_brightness", "set_effect")

    def __init__(self, protocol: Any, cache: Any, config: Any = None,
                 min_brightness: int = 0, max_brightness: int = 100, **kwargs: Any):
        super().__init__(protocol, cache, config, **kwargs)
        self.min_brightness = min_brightness
        self.max_brightness = max_brightness

    @property
    def category(self) -> str:
        return "rgb_light"

    def _clamp_brightness(self, value: Brightness) -> int:
        if isinstance(value, float) and 0.0 <= value <= 1.0:
            value = value * 100
        return int(max(self.min_brightness, min(self.max_brightness, round(value))))

    def _fetch_state(self) -> Dict[str, Any]:
        response = self.protocol.send({"_method": "GET", "_path": "/state"})
        if not isinstance(response, dict):
            response = {}
        r = int(response.get("r", 255))
        g = int(response.get("g", 255))
        b = int(response.get("b", 255))
        return {
            "power": _as_bool(response.get("power", False)),
            "brightness": int(response.get("brightness", 100)),
            "r": r,
            "g": g,
            "b": b,
            "hex": Color(r, g, b).to_hex(),
            "color_mode": response.get("color_mode", ColorMode.RGB),
            "effect": response.get("effect", "none"),
        }

    def set_color(
        self,
        color: Color,
        brightness: Optional[Brightness] = None,
        confirm: Optional[bool] = None,
        timeout: Optional[float] = None,
    ) -> CommandResult:
        """Set an RGB colour (``Color`` or ``(r, g, b)``), optionally with brightness."""
        if not isinstance(color, Color):
            color = Color(*color)
        payload: Dict[str, Any] = {
            "_method": "POST", "_path": "/color",
            "r": color.r, "g": color.g, "b": color.b, "color_mode": ColorMode.RGB,
        }
        expected: Dict[str, Any] = {"r": color.r, "g": color.g, "b": color.b}
        if brightness is not None:
            clamped = self._clamp_brightness(brightness)
            payload["brightness"] = clamped
            expected["brightness"] = clamped
        return self._send_and_confirm(payload, expected, timeout, "set_color", confirm)

    def set_hex(self, hex_color: str, brightness: Optional[Brightness] = None,
                confirm: Optional[bool] = None, timeout: Optional[float] = None) -> CommandResult:
        """Set colour from ``#RRGGBB``."""
        return self.set_color(Color.from_hex(hex_color), brightness, confirm, timeout)

    def set_hsv(
        self,
        h: float,
        s: float,
        v: float,
        confirm: Optional[bool] = None,
        timeout: Optional[float] = None,
    ) -> CommandResult:
        """Set colour from hue (0-360), saturation (0-1) and value (0-1)."""
        color = Color.from_hsv(h, s, v)
        result = self.set_color(color, confirm=confirm, timeout=timeout)
        result.command = "set_hsv"
        return result

    def set_brightness(self, brightness: Brightness, confirm: Optional[bool] = None,
                       timeout: Optional[float] = None) -> CommandResult:
        clamped = self._clamp_brightness(brightness)
        return self._send_and_confirm(
            {"_method": "POST", "_path": "/brightness", "brightness": clamped},
            {"brightness": clamped}, timeout, "set_brightness", confirm,
        )

    def on(
        self,
        color: Optional[Color] = None,
        brightness: Optional[Brightness] = None,
        confirm: Optional[bool] = None,
        timeout: Optional[float] = None,
    ) -> CommandResult:
        payload: Dict[str, Any] = {"_method": "POST", "_path": "/on", "power": True}
        expected: Dict[str, Any] = {"power": True}
        if color is not None:
            if not isinstance(color, Color):
                color = Color(*color)
            rgb = {"r": color.r, "g": color.g, "b": color.b}
            payload.update(rgb)
            expected.update(rgb)
        if brightness is not None:
            clamped = self._clamp_brightness(brightness)
            payload["brightness"] = clamped
            expected["brightness"] = clamped
        return self._send_and_confirm(payload, expected, timeout, "on", confirm)

    def off(self, confirm: Optional[bool] = None, timeout: Optional[float] = None) -> CommandResult:
        return self._send_and_confirm(
            {"_method": "POST", "_path": "/off", "power": False},
            {"power": False}, timeout, "off", confirm,
        )

    def toggle(self, confirm: Optional[bool] = None, timeout: Optional[float] = None) -> CommandResult:
        if self.read_state(fresh=True).get("power"):
            return self.off(confirm=confirm, timeout=timeout)
        return self.on(confirm=confirm, timeout=timeout)

    def set_effect(self, effect: str, speed: int = 50, confirm: Optional[bool] = None,
                   timeout: Optional[float] = None) -> CommandResult:
        """Start a named effect such as ``"rainbow"`` or ``"pulse"``."""
        return self._send_and_confirm(
            {"_method": "POST", "_path": "/effect", "effect": effect, "speed": int(speed)},
            {"effect": effect}, timeout, "set_effect", confirm,
        )

    @property
    def color(self) -> Color:
        state = self.read_state()
        return Color(state.get("r", 255), state.get("g", 255), state.get("b", 255))

    @property
    def hsv(self):
        c = self.color
        h, s, v = colorsys.rgb_to_hsv(c.r / 255, c.g / 255, c.b / 255)
        return (h * 360.0, s, v)

    @property
    def brightness(self) -> int:
        return int(self._state_value("brightness", 100))

    @property
    def is_on(self) -> bool:
        return bool(self._state_value("power", False))
