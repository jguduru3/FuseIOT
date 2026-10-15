"""
In-process simulated devices.

``VirtualDevice`` is a :class:`Protocol` that behaves like real hardware
following the FuseIoT device contract, so you can develop and test without
devices::

    hub.add_device(VirtualDevice("switchable"), Switchable, device_id="lamp")

Options let you simulate latency, random failures and slow actuators
(``settle_ms``: state changes become visible only after a delay, exercising
confirmation polling).
"""
import copy
import random
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..exceptions import ProtocolError
from ..logging_config import get_logger
from ..protocols.base import ConnectionState, Protocol, ProtocolConfig

logger = get_logger("simulation")

DEFAULT_STATES: Dict[str, Dict[str, Any]] = {
    "switchable": {"power": False},
    "dimmable": {"power": False, "brightness": 0},
    "rgb_light": {"power": False, "brightness": 100, "r": 255, "g": 255, "b": 255,
                  "color_mode": "rgb", "effect": "none"},
    "sensor": {"value": 20.0, "unit": "celsius"},
    "motor": {"position": 0.0, "speed": 50.0, "moving": False, "homed": True},
    "thermostat": {"temperature": 20.0, "target_temperature": 21.0, "mode": "off",
                   "fan_running": False, "heating": False, "cooling": False},
    "lock": {"lock_state": "locked", "battery_level": 100, "door_open": False},
    "energy_monitor": {"power": 0.0, "voltage": 230.0, "current": 0.0, "energy": 0.0},
    "smart_plug": {"power": False},
}

_ENERGY_DEFAULT = {"power": 0.0, "voltage": 230.0, "current": 0.0, "energy": 0.0}


@dataclass
class VirtualDeviceConfig:
    """Simulation parameters."""
    initial_state: Optional[Dict[str, Any]] = None
    latency_ms: float = 0.0
    jitter_ms: float = 0.0
    failure_rate: float = 0.0       # probability (0-1) that a request raises
    settle_ms: float = 0.0          # delay before commanded state becomes visible
    sensor_noise: float = 0.0       # +/- random noise added to sensor readings
    load_watts: float = 60.0        # smart_plug draw while on
    seed: Optional[int] = None


class VirtualDevice(Protocol):
    """Simulated device speaking the FuseIoT device contract."""

    def __init__(
        self,
        device_type: str = "switchable",
        config: Optional[VirtualDeviceConfig] = None,
        **overrides: Any,
    ):
        super().__init__(ProtocolConfig(timeout=5.0))
        if device_type not in DEFAULT_STATES:
            raise ValueError(f"Unknown virtual device type {device_type!r}; "
                             f"choose from {sorted(DEFAULT_STATES)}")
        self.device_type = device_type
        self.sim = copy.copy(config) if config else VirtualDeviceConfig()
        for key, value in overrides.items():
            if not hasattr(self.sim, key):
                raise TypeError(f"Unknown simulation option: {key}")
            setattr(self.sim, key, value)

        self._rng = random.Random(self.sim.seed)
        self._lock = threading.RLock()
        self._device_state: Dict[str, Any] = copy.deepcopy(DEFAULT_STATES[device_type])
        self._energy: Dict[str, Any] = dict(_ENERGY_DEFAULT)
        if self.sim.initial_state:
            self._device_state.update(self.sim.initial_state)
        self._pending: List[Tuple[float, Dict[str, Any]]] = []
        self._handlers: Dict[str, Callable[[Dict[str, Any]], Dict[str, Any]]] = {}
        self.requests: List[Dict[str, Any]] = []  # request log for assertions

    @property
    def sim_config(self) -> VirtualDeviceConfig:
        return self.sim

    @property
    def name(self) -> str:
        return "virtual"

    @property
    def endpoint(self) -> str:
        return f"virtual://{self.device_type}"

    # ------------------------------------------------------------ lifecycle

    def connect(self) -> bool:
        self._update_state(ConnectionState.CONNECTED)
        return True

    def disconnect(self) -> None:
        self._update_state(ConnectionState.DISCONNECTED)

    # --------------------------------------------------------------- state

    @property
    def state(self) -> Dict[str, Any]:
        """Current (settled) device state."""
        with self._lock:
            self._apply_pending()
            return copy.deepcopy(self._device_state)

    def set_state(self, state: Dict[str, Any]) -> None:
        """Change device state directly (simulates a physical change)."""
        with self._lock:
            self._device_state.update(state)

    def on_request(self, path: str, handler: Callable[[Dict[str, Any]], Dict[str, Any]]) -> None:
        """Override handling of one path (receives the full payload)."""
        self._handlers[path] = handler

    def _apply_pending(self) -> None:
        now = time.monotonic()
        ready = [chg for at, chg in self._pending if at <= now]
        self._pending = [(at, chg) for at, chg in self._pending if at > now]
        for change in ready:
            self._device_state.update(change)

    def _schedule(self, change: Dict[str, Any]) -> None:
        if self.sim.settle_ms > 0:
            self._pending.append((time.monotonic() + self.sim.settle_ms / 1000.0, change))
        else:
            self._device_state.update(change)

    # ------------------------------------------------------------- handling

    def send(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        if not self.is_connected:
            self.connect()
        delay = self.sim.latency_ms + self._rng.uniform(-self.sim.jitter_ms, self.sim.jitter_ms)
        if delay > 0:
            time.sleep(delay / 1000.0)
        self.requests.append(dict(payload))
        if self.sim.failure_rate and self._rng.random() < self.sim.failure_rate:
            self._record_error("Simulated device failure")
            raise ProtocolError("Simulated device failure", "virtual", self.endpoint)

        method = str(payload.get("_method", "POST")).upper()
        path = str(payload.get("_path", "/"))
        body = {k: v for k, v in payload.items() if not str(k).startswith("_")}
        with self._lock:
            self._apply_pending()
            if path in self._handlers:
                result = self._handlers[path](payload)
            elif method == "GET":
                result = self._read(path)
            else:
                result = self._command(path, body)
        self._record_success(delay if delay > 0 else 0.0)
        return result

    def handle(self, method: str, path: str, body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Entry point used by the HTTP device simulator."""
        payload = dict(body or {})
        payload.update({"_method": method, "_path": path})
        return self.send(payload)

    def _read(self, path: str) -> Dict[str, Any]:
        if path == "/energy" and self.device_type == "smart_plug":
            on = bool(self._device_state.get("power"))
            watts = self.sim.load_watts if on else 0.0
            return {**self._energy, "power": watts, "current": round(watts / self._energy["voltage"], 3)}
        state = copy.deepcopy(self._device_state)
        if self.device_type == "sensor":
            if self.sim.sensor_noise:
                state["value"] = float(state["value"]) + self._rng.uniform(
                    -self.sim.sensor_noise, self.sim.sensor_noise)
            state["timestamp"] = time.time()
        return state

    def _command(self, path: str, body: Dict[str, Any]) -> Dict[str, Any]:
        change: Dict[str, Any] = {}
        if path == "/on":
            change = {"power": True, **body}
        elif path == "/off":
            change = {"power": False}
        elif path in ("/brightness", "/fade"):
            change = {"brightness": body.get("brightness")}
        elif path == "/color":
            change = {k: body[k] for k in ("r", "g", "b", "brightness") if k in body}
            change["color_mode"] = "rgb"
        elif path == "/effect":
            change = {"effect": body.get("effect", "none"), "color_mode": "effect"}
        elif path == "/move":
            change = {"position": body.get("position", 0.0), "moving": False}
            if "speed" in body:
                change["speed"] = body["speed"]
        elif path == "/speed":
            change = {"speed": body.get("speed")}
        elif path == "/stop":
            change = {"moving": False}
        elif path == "/home":
            change = {"position": 0.0, "homed": True, "moving": False}
        elif path == "/target":
            target = body.get("temperature")
            change = {"target_temperature": target}
        elif path == "/mode":
            change = {"mode": body.get("mode", "off")}
        elif path == "/fan":
            change = {"fan_running": bool(body.get("on"))}
        elif path == "/lock":
            action = body.get("action", "lock")
            change = {"lock_state": "locked" if action == "lock" else "unlocked"}
        elif path == "/calibrate":
            change = {"calibrated": True}
        elif path == "/reset":
            if self.device_type == "smart_plug":
                self._energy["energy"] = 0.0
            else:
                change = {"energy": 0.0}
        else:
            change = dict(body)

        change = {k: v for k, v in change.items() if v is not None}
        self._schedule(change)
        if self.device_type == "thermostat":
            self._update_thermostat_flags()
        return {"status": "accepted", "path": path}

    def _update_thermostat_flags(self) -> None:
        s = self._device_state
        mode = s.get("mode")
        temp, target = float(s.get("temperature", 0)), float(s.get("target_temperature", 0))
        s["heating"] = mode in ("heat", "auto") and temp < target
        s["cooling"] = mode in ("cool", "auto") and temp > target

    def __repr__(self) -> str:
        return f"<VirtualDevice {self.device_type} {self._conn_state.value}>"
