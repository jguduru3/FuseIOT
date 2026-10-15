"""Measurement reading capability (temperature, humidity, level, ...)."""
import time
from typing import Any, Dict, Optional

from ..result import CommandResult
from .base import Capability


class Sensor(Capability):
    """
    Read-only measurement with optional calibration.

    Device contract:
        GET  /reading   -> {"value": float, "unit"?: str, "timestamp"?: float}
        POST /calibrate -> {"reference": float}; afterwards /reading reports
                           {"calibrated": true}
    """

    commands = ("calibrate",)

    def __init__(self, protocol: Any, cache: Any, config: Any = None, unit: str = "unknown", **kwargs: Any):
        super().__init__(protocol, cache, config, **kwargs)
        self.unit = unit

    @property
    def category(self) -> str:
        return "sensor"

    def _fetch_state(self) -> Dict[str, Any]:
        response = self.protocol.send({"_method": "GET", "_path": "/reading"})
        if not isinstance(response, dict):
            response = {"value": response}
        value = response.get("value", response.get("reading"))
        if value is None:
            raise ValueError(f"Sensor response has no 'value': {response}")
        state: Dict[str, Any] = {
            "value": float(value),
            "unit": response.get("unit", self.unit),
            "timestamp": float(response.get("timestamp") or time.time()),
        }
        if "calibrated" in response:
            state["calibrated"] = bool(response["calibrated"])
        return state

    def read(self, fresh: bool = False) -> Dict[str, Any]:
        """
        Get a reading.

        Returns:
            dict with ``value``, ``unit``, ``timestamp`` and ``age_seconds``.
        """
        state = self.read_state(fresh=fresh)
        return {**state, "age_seconds": max(0.0, time.time() - state["timestamp"]), "fresh": fresh}

    @property
    def value(self) -> float:
        return float(self._state_value("value", 0.0))

    def calibrate(
        self,
        reference_value: float,
        confirm: Optional[bool] = None,
        timeout: Optional[float] = None,
    ) -> CommandResult:
        """Calibrate the sensor against a known reference value."""
        return self._send_and_confirm(
            {"_method": "POST", "_path": "/calibrate", "reference": reference_value},
            {"calibrated": True},
            timeout, "calibrate", confirm,
        )
