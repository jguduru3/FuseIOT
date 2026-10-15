"""
Cloud connector base: mirror device state upward and accept remote commands.

Remote commands are untrusted input. They are validated, restricted to the
target device's declared ``commands`` and executed through the normal command
pipeline (rate limits, circuit breaker, confirmation).
"""
import json
from abc import ABC, abstractmethod
from typing import Any, Callable, Dict, Optional

from pydantic import ValidationError as PydanticValidationError

from ..exceptions import CloudError
from ..logging_config import get_logger
from ..utils.validation import CommandModel

logger = get_logger("cloud")


class CloudConnector(ABC):
    """
    Abstract cloud connector.

    Subclasses implement :meth:`connect`, :meth:`disconnect`,
    :meth:`publish_state` and :meth:`subscribe_commands`.
    """

    def __init__(self, hub: Any):
        self.hub = hub
        self._connected = False
        self._unsubscribe: Optional[Callable[[], None]] = None

    @abstractmethod
    def connect(self) -> bool:
        """Connect to the cloud service."""

    @abstractmethod
    def disconnect(self) -> None:
        """Disconnect."""

    @abstractmethod
    def publish_state(self, device_id: str, state: Dict[str, Any]) -> bool:
        """Publish one device's state."""

    @abstractmethod
    def subscribe_commands(self, callback: Callable[[Dict[str, Any]], None]) -> None:
        """Call ``callback(message_dict)`` for each command received from the cloud."""

    @property
    def is_connected(self) -> bool:
        return self._connected

    def attach(self) -> None:
        """Publish every state change and execute incoming commands."""
        if self.hub.event_bus is None:
            raise CloudError("Hub event bus is disabled; cannot mirror state")
        self._unsubscribe = self.hub.event_bus.subscribe_all(
            lambda event: self.publish_state(event.device_id, event.new_state)
        )
        self.subscribe_commands(self.handle_command)

    def detach(self) -> None:
        if self._unsubscribe:
            self._unsubscribe()
            self._unsubscribe = None

    def handle_command(self, message: Any) -> Dict[str, Any]:
        """
        Execute a command message ``{"device_id", "command", "params"}``.

        Returns a result dict (also suitable for publishing back).
        """
        if isinstance(message, (bytes, str)):
            try:
                message = json.loads(message)
            except json.JSONDecodeError:
                return {"success": False, "error": "invalid JSON"}
        try:
            cmd = CommandModel.model_validate(message)
        except PydanticValidationError as e:
            logger.warning("cloud_command_invalid", error=str(e))
            return {"success": False, "error": "invalid command message"}

        if cmd.device_id not in self.hub:
            return {"success": False, "device_id": cmd.device_id, "error": "unknown device"}
        device = self.hub[cmd.device_id]
        if cmd.command not in device.commands:
            logger.warning("cloud_command_rejected", device=cmd.device_id, command=cmd.command)
            return {"success": False, "device_id": cmd.device_id,
                    "error": f"command {cmd.command!r} not allowed"}
        try:
            result = getattr(device, cmd.command)(**cmd.params)
        except (TypeError, ValueError) as e:
            return {"success": False, "device_id": cmd.device_id, "error": f"bad params: {e}"}
        logger.info("cloud_command_executed", device=cmd.device_id, command=cmd.command,
                    success=result.success)
        return result.to_dict()

    def __enter__(self) -> "CloudConnector":
        if not self.connect():
            raise CloudError(f"{type(self).__name__} failed to connect")
        return self

    def __exit__(self, *exc: Any) -> None:
        self.detach()
        self.disconnect()
