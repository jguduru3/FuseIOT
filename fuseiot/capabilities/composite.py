"""Devices that combine several capabilities behind one ID."""
from typing import Any, Dict, Optional

from ..logging_config import get_logger
from .base import Capability
from .energy_monitor import EnergyMonitor
from .switchable import Switchable

logger = get_logger("capabilities.composite")


class CompositeCapability(Capability):
    """
    Base for multi-function devices.

    Sub-capabilities get IDs of the form ``<device_id>.<name>`` so their
    cache entries and events stay separate.
    """

    def __init__(self, protocol: Any, cache: Any, config: Any = None, **kwargs: Any):
        super().__init__(protocol, cache, config, **kwargs)
        self._capabilities: Dict[str, Capability] = {}

    def add_capability(self, name: str, capability: Capability) -> None:
        self._capabilities[name] = capability

    def get_capability(self, name: str) -> Optional[Capability]:
        return self._capabilities.get(name)

    def _sub_kwargs(self, name: str) -> Dict[str, Any]:
        return {"event_bus": self.event_bus, "device_id": f"{self.id}.{name}"}

    @property
    def category(self) -> str:
        return "composite"

    def _fetch_state(self) -> Dict[str, Any]:
        return {name: cap._fetch_state() for name, cap in self._capabilities.items()}

    def read_state(self, fresh: bool = False) -> Dict[str, Any]:
        return {name: cap.read_state(fresh=fresh) for name, cap in self._capabilities.items()}

    def last_known_state(self) -> Optional[Dict[str, Any]]:
        states = {name: cap.last_known_state() for name, cap in self._capabilities.items()}
        return states if any(v is not None for v in states.values()) else None

    def add_result_listener(self, listener):  # type: ignore[override]
        removers = [super().add_result_listener(listener)]
        removers += [c.add_result_listener(listener) for c in self._capabilities.values()]

        def remove() -> None:
            for r in removers:
                r()

        return remove


class SmartPlug(CompositeCapability):
    """Smart plug with power monitoring (``Switchable`` + ``EnergyMonitor``)."""

    commands = ("on", "off", "toggle", "reset_counter")

    def __init__(self, protocol: Any, cache: Any, config: Any = None,
                 cost_per_kwh: float = 0.15, currency: str = "USD", **kwargs: Any):
        super().__init__(protocol, cache, config, **kwargs)
        self.switch = Switchable(protocol, cache, self.config, **self._sub_kwargs("switch"))
        self.energy = EnergyMonitor(protocol, cache, self.config, cost_per_kwh=cost_per_kwh,
                                    currency=currency, **self._sub_kwargs("energy"))
        self.add_capability("switch", self.switch)
        self.add_capability("energy", self.energy)

    @property
    def category(self) -> str:
        return "smart_plug"

    def on(self, **kwargs: Any):
        return self.switch.on(**kwargs)

    def off(self, **kwargs: Any):
        return self.switch.off(**kwargs)

    def toggle(self, **kwargs: Any):
        return self.switch.toggle(**kwargs)

    def reset_counter(self, **kwargs: Any):
        return self.energy.reset_counter(**kwargs)

    @property
    def is_on(self) -> bool:
        return self.switch.is_on

    @property
    def power(self) -> float:
        return self.energy.power

    @property
    def today_cost(self) -> float:
        return self.energy.total_cost
