"""Position and speed control (steppers, servos, actuators, blinds)."""
from typing import Any, Dict, Optional, Tuple

from ..result import CommandResult
from .base import Capability


class Motor(Capability):
    """
    Absolute positioning with speed control.

    Device contract:
        GET  /status -> {"position": float, "speed": float, "moving": bool, "homed": bool}
        POST /move   -> {"position": float, "speed"?: float}
        POST /speed  -> {"speed": float}
        POST /stop
        POST /home
    """

    commands = ("move_to", "set_speed", "stop", "home")

    def __init__(
        self,
        protocol: Any,
        cache: Any,
        config: Any = None,
        position_range: Tuple[float, float] = (0, 360),
        speed_range: Tuple[float, float] = (0, 100),
        position_tolerance: float = 0.5,
        **kwargs: Any,
    ):
        super().__init__(protocol, cache, config, **kwargs)
        self.position_min, self.position_max = position_range
        self.speed_min, self.speed_max = speed_range
        # Physical positions rarely match exactly.
        self.config.tolerance = max(self.config.tolerance, position_tolerance)

    @property
    def category(self) -> str:
        return "motor"

    def _clamp_position(self, position: float) -> float:
        return float(max(self.position_min, min(self.position_max, position)))

    def _clamp_speed(self, speed: float) -> float:
        return float(max(self.speed_min, min(self.speed_max, speed)))

    def _fetch_state(self) -> Dict[str, Any]:
        response = self.protocol.send({"_method": "GET", "_path": "/status"})
        if not isinstance(response, dict):
            response = {}
        return {
            "position": float(response.get("position", 0.0)),
            "speed": float(response.get("speed", 0.0)),
            "moving": bool(response.get("moving", False)),
            "homed": bool(response.get("homed", False)),
        }

    def move_to(
        self,
        position: float,
        speed: Optional[float] = None,
        confirm: Optional[bool] = None,
        timeout: Optional[float] = None,
    ) -> CommandResult:
        """Move to an absolute position (clamped to ``position_range``)."""
        target = self._clamp_position(position)
        payload: Dict[str, Any] = {"_method": "POST", "_path": "/move", "position": target}
        if speed is not None:
            payload["speed"] = self._clamp_speed(speed)
        return self._send_and_confirm(
            payload, {"position": target, "moving": False}, timeout, "move_to", confirm
        )

    def set_speed(
        self, speed: float, confirm: Optional[bool] = None, timeout: Optional[float] = None
    ) -> CommandResult:
        clamped = self._clamp_speed(speed)
        return self._send_and_confirm(
            {"_method": "POST", "_path": "/speed", "speed": clamped},
            {"speed": clamped},
            timeout, "set_speed", confirm,
        )

    def stop(self, confirm: Optional[bool] = None, timeout: Optional[float] = None) -> CommandResult:
        """Stop immediately. Sent without retries delay and with a short timeout."""
        return self._send_and_confirm(
            {"_method": "POST", "_path": "/stop", "stop": True},
            {"moving": False},
            timeout if timeout is not None else 1.0, "stop", confirm,
        )

    def home(self, confirm: Optional[bool] = None, timeout: Optional[float] = None) -> CommandResult:
        """Return to the home position (allowed up to 30s by default)."""
        return self._send_and_confirm(
            {"_method": "POST", "_path": "/home"},
            {"position": float(self.position_min), "homed": True},
            timeout if timeout is not None else 30.0, "home", confirm,
        )

    @property
    def position(self) -> float:
        return float(self._state_value("position", 0.0))

    @property
    def is_moving(self) -> bool:
        return bool(self._state_value("moving", False))
