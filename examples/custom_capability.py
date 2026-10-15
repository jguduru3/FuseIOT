"""
Extending FuseIoT: a custom capability and a custom protocol.

    python examples/custom_capability.py
"""
from typing import Any, Dict, Optional

from fuseiot import Capability, CommandResult, Hub, Protocol
from fuseiot.protocols.base import ConnectionState


class Valve(Capability):
    """A water valve that opens to a percentage."""

    commands = ("open_to", "close")

    @property
    def category(self) -> str:
        return "valve"

    def _fetch_state(self) -> Dict[str, Any]:
        reply = self.protocol.send({"_method": "GET", "_path": "/valve"})
        return {"opening": float(reply["opening"])}

    def open_to(self, percent: float, confirm: Optional[bool] = None,
                timeout: Optional[float] = None) -> CommandResult:
        percent = max(0.0, min(100.0, percent))
        return self._send_and_confirm(
            {"_method": "POST", "_path": "/valve", "opening": percent},
            expected_state={"opening": percent},
            timeout=timeout, command_name="open_to", confirm=confirm,
        )

    def close(self, **kwargs: Any) -> CommandResult:
        return self.open_to(0, **kwargs)


class InMemoryValveProtocol(Protocol):
    """Stand-in transport: replace send() with Modbus, CAN, BLE, ..."""

    def __init__(self) -> None:
        super().__init__()
        self.opening = 0.0

    def connect(self) -> bool:
        self._update_state(ConnectionState.CONNECTED)
        return True

    def disconnect(self) -> None:
        self._update_state(ConnectionState.DISCONNECTED)

    def send(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        if payload.get("_method") == "POST":
            self.opening = float(payload["opening"])
        return {"opening": self.opening}


if __name__ == "__main__":
    with Hub() as hub:
        valve = hub.add_device(InMemoryValveProtocol(), Valve, device_id="irrigation_valve")
        print(valve.open_to(35))
        print(valve.read_state())
        print(valve.close())
