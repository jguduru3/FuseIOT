"""Power and energy metering."""
from dataclasses import dataclass
from typing import Any, Dict, Optional

from ..result import CommandResult
from .base import Capability


@dataclass
class EnergyReading:
    power_watts: float
    voltage: float
    current_amps: float
    energy_kwh: float
    frequency_hz: Optional[float] = None
    power_factor: Optional[float] = None


class EnergyMonitor(Capability):
    """
    Power / energy meter.

    Device contract:
        GET  /energy -> {"power": W, "voltage": V, "current": A, "energy": kWh,
                         "frequency"?: Hz, "power_factor"?: float}
        POST /reset  -> {"counter": "energy"}
    """

    commands = ("reset_counter",)

    def __init__(self, protocol: Any, cache: Any, config: Any = None,
                 cost_per_kwh: float = 0.15, currency: str = "USD", **kwargs: Any):
        super().__init__(protocol, cache, config, **kwargs)
        self.cost_per_kwh = cost_per_kwh
        self.currency = currency
        self.config.tolerance = max(self.config.tolerance, 1e-3)

    @property
    def category(self) -> str:
        return "energy_monitor"

    def _fetch_state(self) -> Dict[str, Any]:
        response = self.protocol.send({"_method": "GET", "_path": "/energy"})
        if not isinstance(response, dict):
            response = {}
        energy = float(response.get("energy", 0.0))
        return {
            "power_watts": float(response.get("power", 0.0)),
            "voltage": float(response.get("voltage", 0.0)),
            "current_amps": float(response.get("current", 0.0)),
            "energy_kwh": energy,
            "frequency_hz": response.get("frequency"),
            "power_factor": response.get("power_factor"),
            "cost": round(energy * self.cost_per_kwh, 4),
            "currency": self.currency,
        }

    def read(self, fresh: bool = False) -> EnergyReading:
        state = self.read_state(fresh=fresh)
        return EnergyReading(
            power_watts=state["power_watts"],
            voltage=state["voltage"],
            current_amps=state["current_amps"],
            energy_kwh=state["energy_kwh"],
            frequency_hz=state.get("frequency_hz"),
            power_factor=state.get("power_factor"),
        )

    def reset_counter(self, confirm: Optional[bool] = None, timeout: Optional[float] = None) -> CommandResult:
        return self._send_and_confirm(
            {"_method": "POST", "_path": "/reset", "counter": "energy"},
            {"energy_kwh": 0.0}, timeout, "reset_counter", confirm,
        )

    @property
    def power(self) -> float:
        return float(self._state_value("power_watts", 0.0))

    @property
    def energy_kwh(self) -> float:
        return float(self._state_value("energy_kwh", 0.0))

    @property
    def total_cost(self) -> float:
        """Cost of the energy counted so far at ``cost_per_kwh``."""
        return float(self._state_value("cost", 0.0))

    # Backwards-compatible alias
    today_cost = total_cost

    def calculate_cost(self, kwh: float) -> float:
        return kwh * self.cost_per_kwh
