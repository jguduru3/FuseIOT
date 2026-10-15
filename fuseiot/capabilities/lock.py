"""Smart lock control."""
from enum import Enum
from typing import Any, Dict, Optional

from ..result import CommandResult
from .base import Capability


class LockState(Enum):
    LOCKED = "locked"
    UNLOCKED = "unlocked"
    JAMMED = "jammed"
    UNKNOWN = "unknown"


class Lock(Capability):
    """
    Lock / unlock with state verification.

    Device contract:
        GET  /status -> {"lock_state": "locked|unlocked|jammed",
                         "battery_level"?: int, "door_open"?: bool}
        POST /lock   -> {"action": "lock" | "unlock", "user_code"?: str}
    """

    commands = ("lock", "unlock")

    def __init__(self, protocol: Any, cache: Any, config: Any = None,
                 auto_lock_delay: Optional[int] = None, **kwargs: Any):
        super().__init__(protocol, cache, config, **kwargs)
        self.auto_lock_delay = auto_lock_delay

    @property
    def category(self) -> str:
        return "lock"

    def _fetch_state(self) -> Dict[str, Any]:
        response = self.protocol.send({"_method": "GET", "_path": "/status"})
        if not isinstance(response, dict):
            response = {}
        raw = str(response.get("lock_state", "unknown")).lower()
        lock_state = raw if raw in {s.value for s in LockState} else LockState.UNKNOWN.value
        return {
            "lock_state": lock_state,
            "battery_level": response.get("battery_level"),
            "last_user": response.get("last_user"),
            "door_open": bool(response.get("door_open", False)),
        }

    def lock(self, confirm: Optional[bool] = None, timeout: Optional[float] = None) -> CommandResult:
        return self._send_and_confirm(
            {"_method": "POST", "_path": "/lock", "action": "lock"},
            {"lock_state": LockState.LOCKED.value},
            timeout, "lock", confirm,
        )

    def unlock(
        self,
        user_code: Optional[str] = None,
        confirm: Optional[bool] = None,
        timeout: Optional[float] = None,
    ) -> CommandResult:
        payload: Dict[str, Any] = {"_method": "POST", "_path": "/lock", "action": "unlock"}
        if user_code:
            payload["user_code"] = user_code
        return self._send_and_confirm(
            payload, {"lock_state": LockState.UNLOCKED.value}, timeout, "unlock", confirm
        )

    @property
    def lock_state(self) -> LockState:
        return LockState(self._state_value("lock_state", "unknown"))

    @property
    def is_locked(self) -> bool:
        return self._state_value("lock_state") == LockState.LOCKED.value

    @property
    def battery_level(self) -> Optional[int]:
        return self._state_value("battery_level")
