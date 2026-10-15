"""Binary on/off capability (relays, smart plugs, simple lights)."""
from typing import Any, Dict, Optional

from ..result import CommandResult
from .base import Capability


def _as_bool(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "on", "yes")
    return bool(value)


class Switchable(Capability):
    """
    Binary power control.

    Device contract (HTTP paths, relative to the protocol base URL):
        GET  /state -> {"power": bool}   (``"state": "on"`` is also accepted)
        POST /on    -> turn on
        POST /off   -> turn off
    """

    commands = ("on", "off", "toggle")

    @property
    def category(self) -> str:
        return "switchable"

    def _fetch_state(self) -> Dict[str, Any]:
        response = self.protocol.send({"_method": "GET", "_path": "/state"})
        if not isinstance(response, dict):
            response = {}
        power = response.get("power", response.get("state", False))
        return {"power": _as_bool(power)}

    def on(self, confirm: Optional[bool] = None, timeout: Optional[float] = None) -> CommandResult:
        """Turn the device on."""
        return self._send_and_confirm(
            command_payload={"_method": "POST", "_path": "/on", "power": True},
            expected_state={"power": True},
            timeout=timeout,
            command_name="on",
            confirm=confirm,
        )

    def off(self, confirm: Optional[bool] = None, timeout: Optional[float] = None) -> CommandResult:
        """Turn the device off."""
        return self._send_and_confirm(
            command_payload={"_method": "POST", "_path": "/off", "power": False},
            expected_state={"power": False},
            timeout=timeout,
            command_name="off",
            confirm=confirm,
        )

    def toggle(self, confirm: Optional[bool] = None, timeout: Optional[float] = None) -> CommandResult:
        """Invert the current power state (reads the device first)."""
        current = self.read_state(fresh=True).get("power", False)
        if current:
            return self.off(confirm=confirm, timeout=timeout)
        return self.on(confirm=confirm, timeout=timeout)

    @property
    def is_on(self) -> bool:
        """Current power state (cached for the hub TTL)."""
        return bool(self._state_value("power", False))
