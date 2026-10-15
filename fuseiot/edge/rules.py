"""
Local automation rules ("if this, then that") evaluated on the edge.

Rules can be built in Python or declared in the config file::

    rules:
      - name: frost_protection
        when:
          - {device: outdoor_temp, property: value, op: "<", value: 2}
        then:
          - {device: pipe_heater, command: "on"}
        else:
          - {device: pipe_heater, command: "off"}
        mode: edge          # edge (default): act only when the outcome changes
        cooldown: 60        # seconds between triggers

By default rules are *edge-triggered*: actions run when the conditions become
true (and ``else`` actions when they become false), not on every evaluation.
"""
import asyncio
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Union

from ..automation.actions import Action, AnyAction, NotifyAction, as_list, parse_action
from ..exceptions import ConfigurationError
from ..logging_config import get_logger

logger = get_logger("edge.rules")

_MISSING = object()


class Operator(Enum):
    EQ = "=="
    NE = "!="
    LT = "<"
    GT = ">"
    LE = "<="
    GE = ">="
    CONTAINS = "contains"
    CHANGED = "changed"


@dataclass
class Condition:
    """
    Compare one device property with a value.

    ``property`` is looked up in the device state first (e.g. ``value``,
    ``power``, ``position``), then as a non-callable attribute.
    """
    device_id: str
    property: str
    operator: Union[Operator, str] = Operator.EQ
    value: Any = None
    _previous: Any = field(default=_MISSING, repr=False, compare=False)

    def __post_init__(self) -> None:
        if isinstance(self.operator, str):
            aliases = {"=": "==", "eq": "==", "ne": "!=", "lt": "<", "gt": ">", "le": "<=", "ge": ">="}
            self.operator = Operator(aliases.get(self.operator, self.operator))

    def current_value(self, hub: Any) -> Any:
        device = hub[self.device_id]
        state = device.read_state()
        if isinstance(state, dict) and self.property in state:
            return state[self.property]
        attr = getattr(type(device), self.property, None)
        if isinstance(attr, property):
            return getattr(device, self.property)
        raise ConfigurationError(f"{self.device_id} has no property {self.property!r}")

    def evaluate(self, hub: Any) -> bool:
        try:
            current = self.current_value(hub)
        except Exception as e:
            logger.warning("condition_unavailable", device=self.device_id,
                           property=self.property, error=str(e))
            return False
        op = self.operator
        try:
            if op == Operator.CHANGED:
                changed = self._previous is not _MISSING and current != self._previous
                self._previous = current
                return changed
            if op == Operator.EQ:
                return current == self.value
            if op == Operator.NE:
                return current != self.value
            if op == Operator.LT:
                return current < self.value
            if op == Operator.GT:
                return current > self.value
            if op == Operator.LE:
                return current <= self.value
            if op == Operator.GE:
                return current >= self.value
            if op == Operator.CONTAINS:
                return self.value in current
        except TypeError as e:
            logger.warning("condition_type_error", device=self.device_id,
                           property=self.property, error=str(e))
        return False


@dataclass
class Trigger:
    """Reserved for scheduled triggers (``interval``) in future versions."""
    type: str
    params: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Rule:
    """A named rule: all conditions true -> actions, otherwise -> else_actions."""
    name: str
    conditions: List[Condition]
    actions: List[AnyAction]
    else_actions: Optional[List[AnyAction]] = None
    enabled: bool = True
    mode: str = "edge"           # "edge" or "level"
    cooldown: float = 0.0
    last_triggered: Optional[float] = None
    last_outcome: Optional[bool] = None
    trigger_count: int = 0

    def evaluate(self, hub: Any) -> bool:
        return all(c.evaluate(hub) for c in self.conditions)


def rule_from_dict(spec: Dict[str, Any]) -> Rule:
    """Build a :class:`Rule` from its config-file representation."""
    try:
        name = spec["name"]
        when = spec["when"]
        then = spec["then"]
    except KeyError as e:
        raise ConfigurationError(f"Rule is missing {e.args[0]!r}: {spec}") from e
    conditions = []
    for c in as_list(when, f"Rule {name!r}", "when"):
        try:
            conditions.append(Condition(device_id=c["device"], property=c["property"],
                                        operator=c.get("op", "=="), value=c.get("value")))
        except (KeyError, ValueError) as e:
            raise ConfigurationError(f"Invalid condition in rule {name!r}: {c} ({e})") from e
    mode = spec.get("mode", "edge")
    if mode not in ("edge", "level"):
        raise ConfigurationError(f"Rule {name!r}: mode must be 'edge' or 'level'")
    return Rule(
        name=name,
        conditions=conditions,
        actions=[parse_action(a) for a in as_list(then, f"Rule {name!r}", "then")],
        else_actions=[parse_action(a) for a in as_list(spec.get("else", []), f"Rule {name!r}", "else")]
        or None,
        enabled=bool(spec.get("enabled", True)),
        mode=mode,
        cooldown=float(spec.get("cooldown", 0)),
    )


def _describe(action: AnyAction) -> Dict[str, Any]:
    if isinstance(action, NotifyAction):
        return {"notify": action.channel, "message": action.message}
    return {"device": action.device_id, "command": action.method, "params": action.params}


__all__ = ["Action", "Condition", "NotifyAction", "Operator", "Rule", "RuleEngine", "Trigger", "rule_from_dict"]


class RuleEngine:
    """Evaluate rules against a hub, periodically or on demand."""

    def __init__(self, hub: Any):
        self.hub = hub
        self.rules: List[Rule] = []
        self.running = False
        self._task: Optional[asyncio.Task[None]] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    @classmethod
    def from_config(cls, hub: Any, specs: List[Dict[str, Any]]) -> "RuleEngine":
        engine = cls(hub)
        for spec in specs:
            rule = rule_from_dict(spec)
            actions = rule.actions + (rule.else_actions or [])
            for ref in [c.device_id for c in rule.conditions] + [d for a in actions for d in a.devices]:
                if ref not in hub:
                    raise ConfigurationError(f"Rule {rule.name!r} references unknown device {ref!r}")
            engine.add(rule)
        return engine

    def add(self, rule: Rule) -> None:
        if any(r.name == rule.name for r in self.rules):
            raise ConfigurationError(f"Duplicate rule name: {rule.name}")
        self.rules.append(rule)
        logger.info("rule_added", name=rule.name)

    def add_simple(self, name: str, if_condition: Condition, then_actions: List[Action],
                   else_actions: Optional[List[Action]] = None, **kwargs: Any) -> Rule:
        rule = Rule(name=name, conditions=[if_condition], actions=then_actions,
                    else_actions=else_actions, **kwargs)
        self.add(rule)
        return rule

    def remove(self, name: str) -> bool:
        for i, rule in enumerate(self.rules):
            if rule.name == name:
                self.rules.pop(i)
                logger.info("rule_removed", name=name)
                return True
        return False

    def set_enabled(self, name: str, enabled: bool) -> bool:
        for rule in self.rules:
            if rule.name == name:
                rule.enabled = enabled
                rule.last_outcome = None
                return True
        return False

    async def evaluate(self) -> None:
        """Evaluate every enabled rule once."""
        for rule in list(self.rules):
            if not rule.enabled:
                continue
            loop = asyncio.get_running_loop()
            outcome = await loop.run_in_executor(None, rule.evaluate, self.hub)
            changed = outcome != rule.last_outcome
            rule.last_outcome = outcome
            if rule.mode == "edge" and not changed:
                continue
            now = time.time()
            if rule.cooldown and rule.last_triggered and now - rule.last_triggered < rule.cooldown:
                continue
            actions = rule.actions if outcome else (rule.else_actions or [])
            if not actions:
                continue
            rule.last_triggered = now
            rule.trigger_count += 1
            logger.info("rule_triggered", name=rule.name, outcome=outcome)
            for action in actions:
                await action.execute(self.hub, {"rule": rule.name})

    async def run(self, interval: float = 5.0) -> None:
        self.running = True
        logger.info("rules_engine_started", interval=interval, rules=len(self.rules))
        try:
            while self.running and not self._stop.is_set():
                try:
                    await self.evaluate()
                except Exception as e:  # keep the engine alive
                    logger.error("rules_evaluation_error", error=str(e))
                # Sleep in short slices so stop() takes effect promptly.
                deadline = time.monotonic() + interval
                while self.running and not self._stop.is_set():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    await asyncio.sleep(min(0.05, remaining))
        finally:
            self.running = False
            logger.info("rules_engine_stopped")

    def start(self, interval: float = 5.0) -> None:
        """Start inside the running asyncio loop."""
        self._task = asyncio.get_running_loop().create_task(self.run(interval))

    def start_background(self, interval: float = 5.0) -> None:
        """Start in a dedicated thread (for synchronous applications and the server)."""
        self._stop.clear()
        self._thread = threading.Thread(target=lambda: asyncio.run(self.run(interval)),
                                        daemon=True, name="fuseiot-rules")
        self._thread.start()

    def stop(self) -> None:
        self.running = False
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
        if self._thread is not None:
            self._thread.join(timeout=10)

    def list_rules(self) -> List[Dict[str, Any]]:
        return [
            {
                "name": r.name,
                "enabled": r.enabled,
                "mode": r.mode,
                "last_triggered": r.last_triggered,
                "last_outcome": r.last_outcome,
                "trigger_count": r.trigger_count,
                "conditions": [
                    {"device": c.device_id, "property": c.property,
                     "op": c.operator.value if isinstance(c.operator, Operator) else c.operator,
                     "value": c.value} for c in r.conditions
                ],
                "actions": [_describe(a) for a in r.actions],
            }
            for r in self.rules
        ]
