"""
Actions shared by rules and schedules.

Config forms::

    {device: porch_light, command: "on", params: {}, delay: 0}
    {notify: phone, message: "Hallway is {hallway_temp.value} C", title: "Home", priority: high}

Message templates may reference ``{rule}``, ``{schedule}``, ``{time}``,
``{date}`` and any device state value as ``{<device_id>.<key>}``.
"""
import asyncio
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Union

from ..exceptions import ConfigurationError
from ..logging_config import get_logger

logger = get_logger("automation.actions")

_PLACEHOLDER = re.compile(r"\{([A-Za-z0-9_.\-]+)\}")


def render_template(template: str, hub: Any, context: Optional[Dict[str, Any]] = None) -> str:
    """Fill ``{name}`` placeholders from ``context`` and device states; unknown ones stay as-is."""
    ctx = dict(context or {})
    now = datetime.now()
    ctx.setdefault("time", now.strftime("%H:%M"))
    ctx.setdefault("date", now.strftime("%Y-%m-%d"))

    def lookup(match: "re.Match[str]") -> str:
        key = match.group(1)
        if key in ctx:
            return str(ctx[key])
        device_id, _, prop = key.rpartition(".")
        if device_id and device_id in hub:
            state = hub[device_id].last_known_state() or {}
            if prop in state:
                value = state[prop]
                return f"{value:.1f}" if isinstance(value, float) else str(value)
        return match.group(0)

    return _PLACEHOLDER.sub(lookup, template)


@dataclass
class Action:
    """Invoke a device command (must be one of the device's ``commands``)."""
    device_id: str
    method: str
    params: Dict[str, Any] = field(default_factory=dict)
    delay: float = 0.0

    @property
    def devices(self) -> List[str]:
        return [self.device_id]

    async def execute(self, hub: Any, context: Optional[Dict[str, Any]] = None) -> bool:
        try:
            if self.delay > 0:
                await asyncio.sleep(self.delay)
            device = hub[self.device_id]
            if self.method not in device.commands:
                logger.error("action_not_allowed", device=self.device_id, method=self.method,
                             allowed=list(device.commands))
                return False
            runner = getattr(device, f"{self.method}_async")
            result = await runner(**self.params)
            ok = bool(getattr(result, "success", True))
            log = logger.info if ok else logger.warning
            log("action_executed", device=self.device_id, method=self.method, success=ok,
                error=getattr(result, "error", None))
            if not ok and getattr(hub, "notifier", None) is not None:
                hub.notifier.alert("command_failed", title=f"{self.device_id}: {self.method} failed",
                                   message=str(getattr(result, "error", "unknown error")),
                                   context=context)
            return ok
        except Exception as e:
            logger.error("action_failed", device=self.device_id, method=self.method, error=str(e))
            return False


@dataclass
class NotifyAction:
    """Send a notification through a configured channel (see ``fuseiot.notify``)."""
    channel: str
    message: str
    title: Optional[str] = None
    priority: str = "default"
    delay: float = 0.0

    @property
    def devices(self) -> List[str]:
        return []

    async def execute(self, hub: Any, context: Optional[Dict[str, Any]] = None) -> bool:
        if self.delay > 0:
            await asyncio.sleep(self.delay)
        notifier = getattr(hub, "notifier", None)
        if notifier is None:
            logger.error("notify_without_channels", channel=self.channel)
            return False
        message = render_template(self.message, hub, context)
        title = render_template(self.title, hub, context) if self.title else None
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, lambda: notifier.send(self.channel, message, title=title, priority=self.priority)
        )


AnyAction = Union[Action, NotifyAction]


def parse_action(spec: Dict[str, Any]) -> AnyAction:
    """Build an action from its config mapping."""
    if not isinstance(spec, dict):
        raise ConfigurationError(f"An action must be a mapping, got {spec!r}")
    delay = float(spec.get("delay", 0))
    if "notify" in spec:
        if "message" not in spec:
            raise ConfigurationError(f"Notify action needs a 'message': {spec}")
        return NotifyAction(channel=str(spec["notify"]), message=str(spec["message"]),
                            title=spec.get("title"), priority=str(spec.get("priority", "default")),
                            delay=delay)
    try:
        return Action(device_id=spec["device"], method=spec["command"],
                      params=dict(spec.get("params", {})), delay=delay)
    except KeyError as e:
        raise ConfigurationError(f"Rule action needs 'device' and 'command' (or 'notify'): {spec}") from e


def as_list(value: Any, owner: str, key: str) -> List[Dict[str, Any]]:
    """Accept one mapping or a list of mappings."""
    if isinstance(value, dict):
        return [value]
    if isinstance(value, list) and all(isinstance(v, dict) for v in value):
        return value
    raise ConfigurationError(f"{owner}: '{key}' must be a mapping or a list of mappings")
