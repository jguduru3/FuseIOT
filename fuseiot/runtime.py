"""
Assemble a complete home-automation runtime from a :class:`~fuseiot.config.Config`.

Used by ``fuseiot serve`` and ``fuseiot validate``; also handy when embedding::

    config = auto_config("fuseiot.yaml")
    with Runtime.from_config(config) as rt:
        rt.serve(host="0.0.0.0")
"""
from dataclasses import dataclass, field
from typing import Any, List, Optional

from .auth import UserStore
from .automation.actions import NotifyAction
from .automation.schedule import Location, Scheduler, schedule_from_dict
from .config import Config
from .edge.rules import RuleEngine, rule_from_dict
from .exceptions import ConfigurationError
from .hub import Hub
from .logging_config import get_logger
from .monitoring.device_monitor import DeviceMonitor
from .notify import NotificationManager
from .registry import load_devices, parse_device

logger = get_logger("runtime")


def validate_config(config: Config) -> List[str]:
    """Check devices, rules, schedules, location and notifications without contacting anything."""
    errors: List[str] = []
    ids = set()
    for entry in config.devices:
        try:
            ids.add(parse_device(entry).device_id)
        except ConfigurationError as e:
            errors.append(str(e))
    try:
        notifier = NotificationManager.from_config(config.notifications)
    except ConfigurationError as e:
        errors.append(str(e))
        notifier = None
    try:
        location = Location.from_dict(config.location)
    except ConfigurationError as e:
        errors.append(str(e))
        location = None

    def check_actions(owner: str, actions: List[Any]) -> None:
        for action in actions:
            for ref in action.devices:
                if ref not in ids:
                    errors.append(f"{owner} references unknown device {ref!r}")
            if isinstance(action, NotifyAction) and (notifier is None or not notifier.has_channel(action.channel)):
                errors.append(f"{owner} uses unknown notification channel {action.channel!r}")

    for spec in config.rules:
        try:
            rule = rule_from_dict(spec)
        except ConfigurationError as e:
            errors.append(str(e))
            continue
        for ref in sorted({c.device_id for c in rule.conditions} - ids):
            errors.append(f"Rule {rule.name!r} references unknown device {ref!r}")
        check_actions(f"Rule {rule.name!r}", rule.actions + (rule.else_actions or []))

    for spec in config.schedules:
        try:
            schedule = schedule_from_dict(spec)
        except ConfigurationError as e:
            errors.append(str(e))
            continue
        if schedule.uses_sun and location is not None and not location.has_coordinates:
            errors.append(f"Schedule {schedule.name!r} uses sunrise/sunset; set location.latitude/longitude")
        check_actions(f"Schedule {schedule.name!r}", schedule.actions)
    return errors


@dataclass
class Runtime:
    hub: Hub
    config: Config
    notifier: Optional[NotificationManager] = None
    rules: Optional[RuleEngine] = None
    scheduler: Optional[Scheduler] = None
    monitor: Optional[DeviceMonitor] = None
    users: Optional[UserStore] = None
    cloud: Any = None
    server: Any = None
    _started: bool = field(default=False, repr=False)

    @classmethod
    def from_config(cls, config: Config, hub: Optional[Hub] = None, with_rules: bool = True) -> "Runtime":
        errors = validate_config(config)
        if errors:
            raise ConfigurationError("Invalid configuration:\n  - " + "\n  - ".join(errors))
        hub = hub or Hub(config)
        load_devices(hub, config.devices, connect=False)
        rt = cls(hub=hub, config=config)
        if config.notifications.get("channels"):
            rt.notifier = NotificationManager.from_config(config.notifications)
            hub.notifier = rt.notifier
        if config.rules and with_rules:
            rt.rules = RuleEngine.from_config(hub, config.rules)
        if config.schedules or config.location:
            rt.scheduler = Scheduler.from_config(hub, config.schedules, config.location)
        if config.monitor_interval > 0 and len(hub):
            rt.monitor = DeviceMonitor(hub, interval=config.monitor_interval,
                                       offline_after=config.offline_after, notifier=rt.notifier)
        if config.users_db:
            rt.users = UserStore(config.users_db, session_ttl=config.session_days * 86400)
        if config.cloud_enabled:
            from .cloud import connector_from_config

            rt.cloud = connector_from_config(hub, config.cloud_provider or "", config.cloud_options)
        return rt

    def start(self) -> "Runtime":
        """Start background services (monitor, rules, schedules, cloud)."""
        if self._started:
            return self
        if self.monitor is not None:
            self.monitor.start_background()
        if self.rules is not None:
            self.rules.start_background(self.config.rules_interval)
        if self.scheduler is not None:
            self.scheduler.start_background()
        if self.cloud is not None:
            if self.cloud.connect():
                self.cloud.attach()
            else:
                logger.warning("cloud_connect_failed")
        self._started = True
        return self

    def make_server(self, host: Optional[str] = None, port: Optional[int] = None,
                    api_token: Optional[str] = None, allow_insecure: bool = False) -> Any:
        from .server.app import FuseIoTServer

        self.server = FuseIoTServer(
            self.hub, host=host or self.config.server_host, port=port if port is not None else self.config.server_port,
            api_token=api_token or self.config.api_token, rule_engine=self.rules, allow_insecure=allow_insecure,
            users=self.users, scheduler=self.scheduler, monitor=self.monitor, notifier=self.notifier,
        )
        return self.server

    def stop(self) -> None:
        for part in (self.scheduler, self.rules, self.monitor):
            if part is not None:
                try:
                    part.stop()
                except Exception as e:
                    logger.warning("runtime_stop_error", part=type(part).__name__, error=str(e))
        if self.cloud is not None:
            self.cloud.detach()
            self.cloud.disconnect()
        if self.server is not None:
            self.server.stop()
            self.server = None
        if self.users is not None:
            self.users.close()
        self.hub.close()
        self._started = False

    def __enter__(self) -> "Runtime":
        return self.start()

    def __exit__(self, *exc: Any) -> None:
        self.stop()
