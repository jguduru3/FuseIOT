"""
Time-based automation: fixed times, sunrise/sunset with offsets, and cron.

Config::

    location: {latitude: 12.97, longitude: 77.59, timezone: Asia/Kolkata}
    schedules:
      - name: porch_light_on
        at: sunset+10m            # HH:MM | sunrise | sunset, optional +/-<N>h<M>m offset
        days: daily               # daily | weekdays | weekends | [mon, wed, fri]
        then: [{device: porch_light, command: "on"}]
      - name: porch_light_off
        at: "23:30"
        then: {device: porch_light, command: "off"}
      - name: morning_heating
        cron: "30 6 * * 1-5"      # 06:30 Monday-Friday
        then:
          - {device: heating, command: set_mode, params: {mode: heat}}
          - {notify: phone, message: "Heating on, hallway is {hallway_temp.value} C"}

Missed runs (host asleep or down for more than ``grace_seconds``) are skipped
rather than replayed late, so a light does not switch on at 3am because the
gateway rebooted.
"""
import asyncio
import re
import threading
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, tzinfo
from typing import Any, Dict, List, Optional

from ..exceptions import ConfigurationError
from ..logging_config import get_logger
from .actions import AnyAction, as_list, parse_action
from .cron import CronExpression
from .sun import sun_event

logger = get_logger("automation.schedule")

_WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
_DAY_SETS = {
    "daily": set(range(7)),
    "everyday": set(range(7)),
    "weekdays": {0, 1, 2, 3, 4},
    "weekends": {5, 6},
}
_AT_PATTERN = re.compile(
    r"^\s*(?:(?P<sun>sunrise|sunset)|(?P<hh>\d{1,2}):(?P<mm>\d{2}))"
    r"\s*(?:(?P<sign>[+-])\s*(?:(?P<oh>\d+)h)?\s*(?:(?P<om>\d+)m)?)?\s*$",
    re.IGNORECASE,
)


@dataclass
class Location:
    """Where the home is: needed for sunrise/sunset and the local time zone."""
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    timezone: str = "local"

    @property
    def tz(self) -> tzinfo:
        if self.timezone in ("", "local", None):
            local = datetime.now().astimezone().tzinfo
            assert local is not None
            return local
        try:
            from zoneinfo import ZoneInfo
        except ImportError:  # pragma: no cover - Python < 3.9
            raise ConfigurationError("zoneinfo requires Python 3.9+") from None
        try:
            return ZoneInfo(self.timezone)
        except Exception as e:
            raise ConfigurationError(
                f"Unknown timezone {self.timezone!r} (use an IANA name like 'Europe/Berlin'): {e}"
            ) from None

    @property
    def has_coordinates(self) -> bool:
        return self.latitude is not None and self.longitude is not None

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "Location":
        data = data or {}
        unknown = set(data) - {"latitude", "longitude", "timezone"}
        if unknown:
            raise ConfigurationError(f"Unknown location keys: {sorted(unknown)}")
        coords = {}
        for key in ("latitude", "longitude"):
            value = data.get(key)
            if value is not None:
                try:
                    value = float(value)
                except (TypeError, ValueError):
                    raise ConfigurationError(f"location.{key} must be a number, got {value!r}") from None
            coords[key] = value
        loc = cls(latitude=coords["latitude"], longitude=coords["longitude"],
                  timezone=data.get("timezone", "local"))
        if (loc.latitude is None) != (loc.longitude is None):
            raise ConfigurationError("location needs both latitude and longitude")
        if loc.latitude is not None and not -90 <= float(loc.latitude) <= 90:
            raise ConfigurationError("latitude must be between -90 and 90")
        if loc.longitude is not None and not -180 <= float(loc.longitude) <= 180:
            raise ConfigurationError("longitude must be between -180 and 180")
        _ = loc.tz  # validate the time zone early
        return loc

    def now(self) -> datetime:
        return datetime.now(self.tz)


def _parse_days(value: Any, name: str) -> set:
    if value is None:
        return set(range(7))
    if isinstance(value, str):
        key = value.strip().lower()
        if key in _DAY_SETS:
            return set(_DAY_SETS[key])
        value = [v.strip() for v in key.split(",")]
    if not isinstance(value, (list, tuple, set)):
        raise ConfigurationError(
            f"Schedule {name!r}: 'days' must be daily, weekdays, weekends or a list of days, got {value!r}")
    days = set()
    for item in value:
        key = str(item).strip().lower()[:3]
        if key not in _WEEKDAYS:
            raise ConfigurationError(f"Schedule {name!r}: unknown day {item!r}")
        days.add(_WEEKDAYS.index(key))
    if not days:
        raise ConfigurationError(f"Schedule {name!r}: 'days' is empty")
    return days


@dataclass
class Schedule:
    """One scheduled job."""
    name: str
    actions: List[AnyAction]
    at: Optional[str] = None
    cron: Optional[str] = None
    days: set = field(default_factory=lambda: set(range(7)))
    enabled: bool = True
    last_run: Optional[datetime] = None
    last_ok: Optional[bool] = None
    run_count: int = 0
    _sun: Optional[str] = field(default=None, repr=False)
    _clock: Optional[time] = field(default=None, repr=False)
    _offset: timedelta = field(default=timedelta(0), repr=False)
    _cron: Optional[CronExpression] = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if bool(self.at) == bool(self.cron):
            raise ConfigurationError(f"Schedule {self.name!r}: set exactly one of 'at' or 'cron'")
        if self.cron:
            self._cron = CronExpression(self.cron)
            return
        match = _AT_PATTERN.match(str(self.at))
        if not match:
            raise ConfigurationError(
                f"Schedule {self.name!r}: invalid 'at' {self.at!r} "
                "(use HH:MM, sunrise, sunset, or e.g. sunset-30m / sunrise+1h15m)"
            )
        if match.group("sun"):
            self._sun = match.group("sun").lower()
        else:
            hh, mm = int(match.group("hh")), int(match.group("mm"))
            if hh > 23 or mm > 59:
                raise ConfigurationError(f"Schedule {self.name!r}: invalid time {self.at!r}")
            self._clock = time(hh, mm)
        if match.group("sign"):
            minutes = int(match.group("oh") or 0) * 60 + int(match.group("om") or 0)
            if minutes == 0:
                raise ConfigurationError(f"Schedule {self.name!r}: offset needs hours or minutes")
            self._offset = timedelta(minutes=minutes if match.group("sign") == "+" else -minutes)

    @property
    def uses_sun(self) -> bool:
        return self._sun is not None

    def _time_on(self, day: date, location: Location) -> Optional[datetime]:
        tz = location.tz
        if self._sun:
            if not location.has_coordinates:
                raise ConfigurationError(
                    f"Schedule {self.name!r} uses {self._sun}; set location.latitude/longitude"
                )
            event = sun_event(day, float(location.latitude), float(location.longitude), tz,  # type: ignore[arg-type]
                              rising=self._sun == "sunrise")
            return None if event is None else event + self._offset
        assert self._clock is not None
        return datetime.combine(day, self._clock, tzinfo=tz) + self._offset

    def next_run(self, after: datetime, location: Location) -> Optional[datetime]:
        """Next run strictly after ``after`` (an aware datetime), or None if none within a year."""
        tz = location.tz
        local_after = after.astimezone(tz)
        if self._cron is not None:
            for candidate in self._cron.iter_after(local_after.replace(tzinfo=None)):
                if candidate.weekday() in self.days:
                    return candidate.replace(tzinfo=tz)
            return None
        day = local_after.date() - timedelta(days=1)  # offsets can move a run across midnight
        for _ in range(400):
            if day.weekday() in self.days:
                when = self._time_on(day, location)
                if when is not None and when > local_after:
                    return when
            day += timedelta(days=1)
        return None

    def describe(self, location: Location, now: Optional[datetime] = None) -> Dict[str, Any]:
        now = now or location.now()
        try:
            upcoming = self.next_run(now, location)
        except ConfigurationError:
            upcoming = None
        return {
            "name": self.name,
            "enabled": self.enabled,
            "at": self.at,
            "cron": self.cron,
            "days": [_WEEKDAYS[d] for d in sorted(self.days)],
            "next_run": upcoming.isoformat() if upcoming and self.enabled else None,
            "last_run": self.last_run.isoformat() if self.last_run else None,
            "last_ok": self.last_ok,
            "run_count": self.run_count,
        }


def schedule_from_dict(spec: Dict[str, Any]) -> Schedule:
    if not isinstance(spec, dict):
        raise ConfigurationError(f"A schedule must be a mapping, got {spec!r}")
    unknown = set(spec) - {"name", "at", "cron", "days", "then", "enabled"}
    if unknown:
        raise ConfigurationError(f"Schedule {spec.get('name', '?')!r}: unknown keys {sorted(unknown)}")
    try:
        name = str(spec["name"])
        then = spec["then"]
    except KeyError as e:
        raise ConfigurationError(f"Schedule is missing {e.args[0]!r}: {spec}") from e
    actions = [parse_action(a) for a in as_list(then, f"Schedule {name!r}", "then")]
    if not actions:
        raise ConfigurationError(f"Schedule {name!r}: 'then' is empty")
    return Schedule(name=name, actions=actions, at=spec.get("at"), cron=spec.get("cron"),
                    days=_parse_days(spec.get("days"), name), enabled=bool(spec.get("enabled", True)))


class Scheduler:
    """Runs :class:`Schedule` jobs against a hub in a background thread."""

    def __init__(self, hub: Any, location: Optional[Location] = None, grace_seconds: float = 120.0):
        self.hub = hub
        self.location = location or Location()
        self.grace_seconds = grace_seconds
        self.schedules: List[Schedule] = []
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._due: Dict[str, Optional[datetime]] = {}

    @classmethod
    def from_config(cls, hub: Any, specs: List[Dict[str, Any]],
                    location: Optional[Dict[str, Any]] = None) -> "Scheduler":
        scheduler = cls(hub, Location.from_dict(location))
        for spec in specs:
            schedule = schedule_from_dict(spec)
            for action in schedule.actions:
                for ref in action.devices:
                    if ref not in hub:
                        raise ConfigurationError(
                            f"Schedule {schedule.name!r} references unknown device {ref!r}")
            if schedule.uses_sun and not scheduler.location.has_coordinates:
                raise ConfigurationError(
                    f"Schedule {schedule.name!r} uses sunrise/sunset; set location.latitude/longitude")
            scheduler.add(schedule)
        return scheduler

    # ----------------------------------------------------------- management

    def add(self, schedule: Schedule) -> None:
        with self._lock:
            if any(s.name == schedule.name for s in self.schedules):
                raise ConfigurationError(f"Duplicate schedule name: {schedule.name}")
            self.schedules.append(schedule)
            self._due[schedule.name] = self._compute_next(schedule, self.location.now())
        self._wake.set()
        logger.info("schedule_added", name=schedule.name, next_run=str(self._due[schedule.name]))

    def remove(self, name: str) -> bool:
        with self._lock:
            before = len(self.schedules)
            self.schedules = [s for s in self.schedules if s.name != name]
            self._due.pop(name, None)
            removed = len(self.schedules) != before
        self._wake.set()
        return removed

    def get(self, name: str) -> Optional[Schedule]:
        with self._lock:
            return next((s for s in self.schedules if s.name == name), None)

    def set_enabled(self, name: str, enabled: bool) -> bool:
        schedule = self.get(name)
        if schedule is None:
            return False
        schedule.enabled = enabled
        with self._lock:
            self._due[name] = self._compute_next(schedule, self.location.now())
        self._wake.set()
        return True

    def list_schedules(self) -> List[Dict[str, Any]]:
        now = self.location.now()
        with self._lock:
            return [s.describe(self.location, now) for s in self.schedules]

    def _compute_next(self, schedule: Schedule, after: datetime) -> Optional[datetime]:
        if not schedule.enabled:
            return None
        return schedule.next_run(after, self.location)

    # -------------------------------------------------------------- running

    def run_now(self, name: str) -> bool:
        """Run a schedule's actions immediately (for testing from the API/CLI)."""
        schedule = self.get(name)
        if schedule is None:
            return False
        return self._execute(schedule, self.location.now())

    def _execute(self, schedule: Schedule, when: datetime) -> bool:
        logger.info("schedule_fired", name=schedule.name, at=when.isoformat())

        async def run_all() -> bool:
            ok = True
            for action in schedule.actions:
                ok = await action.execute(self.hub, {"schedule": schedule.name}) and ok
            return ok

        try:
            ok = asyncio.run(run_all())
        except Exception as e:
            logger.error("schedule_failed", name=schedule.name, error=str(e))
            ok = False
        schedule.last_run = when
        schedule.last_ok = ok
        schedule.run_count += 1
        return ok

    def tick(self, now: Optional[datetime] = None) -> List[str]:
        """
        Fire every schedule that is due at ``now``. Returns the names fired.

        Called by the background loop; public so it can be driven in tests.
        """
        now = now or self.location.now()
        due: List[Schedule] = []
        with self._lock:
            for schedule in self.schedules:
                when = self._due.get(schedule.name)
                if when is None or when > now:
                    continue
                if (now - when).total_seconds() > self.grace_seconds:
                    logger.warning("schedule_missed", name=schedule.name, due=when.isoformat())
                else:
                    due.append(schedule)
                self._due[schedule.name] = self._compute_next(schedule, max(now, when))
        for schedule in due:
            threading.Thread(target=self._execute, args=(schedule, now), daemon=True,
                             name=f"schedule-{schedule.name}").start()
        return [s.name for s in due]

    def _loop(self) -> None:
        logger.info("scheduler_started", schedules=len(self.schedules), timezone=str(self.location.tz))
        while not self._stop.is_set():
            self.tick()
            with self._lock:
                upcoming = [d for d in self._due.values() if d is not None]
            now = self.location.now()
            wait = min([(d - now).total_seconds() for d in upcoming] + [30.0])
            self._wake.clear()
            # Wake at least every 30s so clock changes (NTP, DST) are picked up.
            self._wake.wait(max(0.05, min(wait, 30.0)))
        logger.info("scheduler_stopped")

    def start_background(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="fuseiot-scheduler")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
