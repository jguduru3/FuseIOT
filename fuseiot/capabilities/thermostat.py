"""Heating / cooling control."""
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Optional, Union

from ..result import CommandResult
from .base import Capability, CapabilityConfig


class ThermostatMode(Enum):
    OFF = "off"
    HEAT = "heat"
    COOL = "cool"
    AUTO = "auto"
    FAN_ONLY = "fan_only"


@dataclass
class ThermostatConfig(CapabilityConfig):
    min_temp: float = 10.0
    max_temp: float = 30.0
    hysteresis: float = 0.5


class Thermostat(Capability):
    """
    Target temperature, mode and fan control.

    Device contract:
        GET  /status -> {"temperature": float, "target_temperature": float,
                         "mode": "off|heat|cool|auto|fan_only",
                         "fan_running": bool, "heating"?: bool, "cooling"?: bool}
        POST /target -> {"temperature": float}
        POST /mode   -> {"mode": str}
        POST /fan    -> {"on": bool}
    """

    commands = ("set_temperature", "set_mode", "fan_on", "fan_off")

    def __init__(
        self,
        protocol: Any,
        cache: Any,
        config: Any = None,
        min_temp: Optional[float] = None,
        max_temp: Optional[float] = None,
        hysteresis: Optional[float] = None,
        **kwargs: Any,
    ):
        super().__init__(protocol, cache, config, **kwargs)
        cfg = self.config
        self.min_temp = min_temp if min_temp is not None else getattr(cfg, "min_temp", 10.0)
        self.max_temp = max_temp if max_temp is not None else getattr(cfg, "max_temp", 30.0)
        self.hysteresis = hysteresis if hysteresis is not None else getattr(cfg, "hysteresis", 0.5)
        self.config.tolerance = max(self.config.tolerance, 0.05)

    @property
    def category(self) -> str:
        return "thermostat"

    def _fetch_state(self) -> Dict[str, Any]:
        response = self.protocol.send({"_method": "GET", "_path": "/status"})
        if not isinstance(response, dict):
            response = {}
        return {
            "temperature": float(response.get("temperature", 0.0)),
            "target_temperature": float(response.get("target_temperature", 0.0)),
            "mode": str(response.get("mode", "off")),
            "fan_running": bool(response.get("fan_running", False)),
            "heating": bool(response.get("heating", False)),
            "cooling": bool(response.get("cooling", False)),
        }

    def set_temperature(
        self, temperature: float, confirm: Optional[bool] = None, timeout: Optional[float] = None
    ) -> CommandResult:
        clamped = float(max(self.min_temp, min(self.max_temp, temperature)))
        return self._send_and_confirm(
            {"_method": "POST", "_path": "/target", "temperature": clamped},
            {"target_temperature": clamped},
            timeout, "set_temperature", confirm,
        )

    def set_mode(
        self,
        mode: Union[ThermostatMode, str],
        confirm: Optional[bool] = None,
        timeout: Optional[float] = None,
    ) -> CommandResult:
        value = ThermostatMode(mode).value
        return self._send_and_confirm(
            {"_method": "POST", "_path": "/mode", "mode": value},
            {"mode": value},
            timeout, "set_mode", confirm,
        )

    def fan_on(self, confirm: Optional[bool] = None, timeout: Optional[float] = None) -> CommandResult:
        return self._send_and_confirm(
            {"_method": "POST", "_path": "/fan", "on": True},
            {"fan_running": True}, timeout, "fan_on", confirm,
        )

    def fan_off(self, confirm: Optional[bool] = None, timeout: Optional[float] = None) -> CommandResult:
        return self._send_and_confirm(
            {"_method": "POST", "_path": "/fan", "on": False},
            {"fan_running": False}, timeout, "fan_off", confirm,
        )

    @property
    def temperature(self) -> float:
        return float(self._state_value("temperature", 0.0))

    @property
    def target_temperature(self) -> float:
        return float(self._state_value("target_temperature", 0.0))

    @property
    def mode(self) -> str:
        return str(self._state_value("mode", "off"))

    @property
    def is_heating(self) -> bool:
        return bool(self._state_value("heating", False))

    @property
    def is_cooling(self) -> bool:
        return bool(self._state_value("cooling", False))
