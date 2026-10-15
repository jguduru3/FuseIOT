"""Time-based automation: Location, Schedule, schedule_from_dict and the Scheduler."""
import threading
import time
from datetime import date, datetime, timedelta, timezone

import pytest

from fuseiot import Sensor, Switchable
from fuseiot.automation.actions import Action, NotifyAction
from fuseiot.automation.schedule import Location, Schedule, Scheduler, schedule_from_dict
from fuseiot.automation.sun import sunrise, sunset
from fuseiot.exceptions import ConfigurationError
from fuseiot.notify import LogChannel, NotificationManager
from tests.helpers import wait_for

LONDON = {"latitude": 51.5074, "longitude": -0.1278, "timezone": "Europe/London"}
UTC = timezone.utc


def sched(at=None, cron=None, days=None, name="s", actions=None):
    return Schedule(name=name, actions=actions or [Action("lamp", "on")], at=at, cron=cron,
                    days=days if days is not None else set(range(7)))


@pytest.fixture
def lamp(add_virtual):
    return add_virtual("switchable", Switchable, "lamp")


# --------------------------------------------------------------------------- Location


class TestLocation:
    def test_defaults_to_local_timezone(self):
        loc = Location.from_dict(None)
        assert loc.timezone == "local" and not loc.has_coordinates
        assert loc.tz.utcoffset(datetime.now()) == datetime.now().astimezone().utcoffset()
        assert loc.now().tzinfo is not None

    def test_iana_timezone(self):
        loc = Location.from_dict(LONDON)
        assert loc.has_coordinates and str(loc.tz) == "Europe/London"
        assert loc.now().tzinfo is loc.tz

    @pytest.mark.parametrize("data,fragment", [
        ({"latitude": 1.0}, "both latitude and longitude"),
        ({"longitude": 1.0}, "both latitude and longitude"),
        ({"latitude": 91, "longitude": 0}, "latitude must be between"),
        ({"latitude": -90.5, "longitude": 0}, "latitude must be between"),
        ({"latitude": 0, "longitude": 180.1}, "longitude must be between"),
        ({"timezone": "Mars/Olympus_Mons"}, "Unknown timezone"),
        ({"lat": 1, "lon": 2}, "Unknown location keys"),
    ])
    def test_validation(self, data, fragment):
        with pytest.raises(ConfigurationError, match=fragment):
            Location.from_dict(data)

    def test_boundary_coordinates_accepted(self):
        loc = Location.from_dict({"latitude": -90, "longitude": 180, "timezone": "UTC"})
        assert loc.has_coordinates

    def test_non_numeric_coordinates_are_configuration_errors(self):
        with pytest.raises(ConfigurationError):
            Location.from_dict({"latitude": "north", "longitude": 0})


# --------------------------------------------------------------------------- Schedule


class TestScheduleParsing:
    @pytest.mark.parametrize("at", ["sunset-30m", "sunrise+1h15m", "SUNSET", "sunrise + 2h", "07:05",
                                    "23:59", "0:00", "12:00+10m"])
    def test_valid_at(self, at):
        sched(at=at)

    @pytest.mark.parametrize("at,fragment", [
        ("24:00", "invalid time"),
        ("12:60", "invalid time"),
        ("7:5", "invalid 'at'"),
        ("noon", "invalid 'at'"),
        ("sunset+", "offset needs hours or minutes"),
        ("sunset+30", "invalid 'at'"),
        ("sunset*2h", "invalid 'at'"),
    ])
    def test_invalid_at(self, at, fragment):
        with pytest.raises(ConfigurationError, match=fragment):
            sched(at=at)

    def test_exactly_one_of_at_or_cron(self):
        with pytest.raises(ConfigurationError, match="exactly one"):
            sched()
        with pytest.raises(ConfigurationError, match="exactly one"):
            sched(at="10:00", cron="* * * * *")

    def test_bad_cron_rejected(self):
        with pytest.raises(ConfigurationError, match="5 fields"):
            sched(cron="* *")

    def test_uses_sun(self):
        assert sched(at="sunset").uses_sun
        assert not sched(at="10:00").uses_sun
        assert not sched(cron="0 * * * *").uses_sun


class TestNextRun:
    def test_fixed_time_today_then_tomorrow(self):
        loc = Location(timezone="Europe/London")
        tz = loc.tz
        s = sched(at="07:30")
        assert s.next_run(datetime(2026, 10, 15, 6, 0, tzinfo=tz), loc) == datetime(2026, 10, 15, 7, 30, tzinfo=tz)
        assert s.next_run(datetime(2026, 10, 15, 7, 30, tzinfo=tz), loc) == datetime(2026, 10, 16, 7, 30, tzinfo=tz)

    def test_after_in_other_timezone_is_converted(self):
        loc = Location(timezone="Asia/Kolkata")
        s = sched(at="09:00")
        # 03:00 UTC is 08:30 IST, so the run is 30 minutes later.
        nxt = s.next_run(datetime(2026, 10, 15, 3, 0, tzinfo=UTC), loc)
        assert nxt.astimezone(UTC) == datetime(2026, 10, 15, 3, 30, tzinfo=UTC)

    def test_positive_and_negative_offsets(self):
        loc = Location(timezone="UTC")
        tz = loc.tz
        after = datetime(2026, 10, 15, 0, 0, tzinfo=tz)
        assert sched(at="10:00+1h15m").next_run(after, loc) == datetime(2026, 10, 15, 11, 15, tzinfo=tz)
        assert sched(at="10:00-45m").next_run(after, loc) == datetime(2026, 10, 15, 9, 15, tzinfo=tz)

    def test_negative_offset_crossing_midnight(self):
        loc = Location(timezone="UTC")
        tz = loc.tz
        s = sched(at="00:10-30m", days={0})  # Monday 00:10 minus 30m = Sunday 23:40
        nxt = s.next_run(datetime(2026, 10, 18, 12, 0, tzinfo=tz), loc)  # Sunday noon
        assert nxt == datetime(2026, 10, 18, 23, 40, tzinfo=tz)

    def test_days_filter(self):
        loc = Location(timezone="UTC")
        tz = loc.tz
        s = sched(at="08:00", days={5, 6})  # weekends
        nxt = s.next_run(datetime(2026, 10, 15, 9, 0, tzinfo=tz), loc)  # Thursday
        assert nxt == datetime(2026, 10, 17, 8, 0, tzinfo=tz) and nxt.weekday() == 5

    def test_sunset_with_offset(self):
        loc = Location.from_dict(LONDON)
        s = sched(at="sunset-30m")
        nxt = s.next_run(datetime(2026, 6, 21, 12, 0, tzinfo=loc.tz), loc)
        expected = sunset(date(2026, 6, 21), LONDON["latitude"], LONDON["longitude"], loc.tz) - timedelta(minutes=30)
        assert nxt == expected
        assert abs((nxt - nxt.replace(hour=20, minute=51, second=0)).total_seconds()) <= 180

    def test_sunrise_with_offset_moves_to_next_day_once_passed(self):
        loc = Location.from_dict(LONDON)
        s = sched(at="sunrise+1h15m")
        nxt = s.next_run(datetime(2026, 6, 21, 12, 0, tzinfo=loc.tz), loc)
        rise = sunrise(date(2026, 6, 22), LONDON["latitude"], LONDON["longitude"], loc.tz)
        assert nxt == rise + timedelta(hours=1, minutes=15)

    def test_sun_requires_coordinates(self):
        with pytest.raises(ConfigurationError, match="latitude/longitude"):
            sched(at="sunset").next_run(datetime.now(UTC), Location(timezone="UTC"))

    def test_polar_night_skips_days_without_sunrise(self):
        loc = Location(latitude=69.65, longitude=18.96, timezone="Europe/Oslo")
        nxt = sched(at="sunrise").next_run(datetime(2026, 12, 1, tzinfo=loc.tz), loc)
        assert nxt is not None and nxt.date() >= date(2027, 1, 10)

    def test_cron_next_run_and_days_filter(self):
        loc = Location(timezone="Europe/Berlin")
        tz = loc.tz
        s = sched(cron="30 6 * * *", days={0})  # only Mondays
        nxt = s.next_run(datetime(2026, 10, 15, 12, 0, tzinfo=tz), loc)
        assert nxt == datetime(2026, 10, 19, 6, 30, tzinfo=tz)

    def test_describe(self):
        loc = Location(timezone="UTC")
        s = sched(at="10:00", days={0, 2})
        info = s.describe(loc, now=datetime(2026, 10, 15, tzinfo=loc.tz))
        assert info["name"] == "s" and info["days"] == ["mon", "wed"] and info["at"] == "10:00"
        assert info["next_run"] == "2026-10-19T10:00:00+00:00"
        assert info["last_run"] is None and info["run_count"] == 0
        s.enabled = False
        assert s.describe(loc)["next_run"] is None

    def test_describe_tolerates_missing_coordinates(self):
        info = sched(at="sunset").describe(Location(timezone="UTC"))
        assert info["next_run"] is None


class TestDst:
    @pytest.mark.parametrize("at", ["01:30", "02:30"])
    @pytest.mark.parametrize("start", [date(2027, 3, 26), date(2027, 10, 29)])
    def test_london_transitions_run_at_most_once_per_day(self, at, start):
        loc = Location(timezone="Europe/London")
        s = sched(at=at)
        t = datetime(start.year, start.month, start.day, tzinfo=UTC)
        runs = []
        for _ in range(5):
            t = s.next_run(t, loc)
            runs.append(t)
        local_days = [r.astimezone(loc.tz).date() for r in runs]
        assert len(set(local_days)) == len(local_days) == 5
        utc = [r.astimezone(UTC) for r in runs]
        assert utc == sorted(utc)
        for a, b in zip(utc, utc[1:]):
            assert timedelta(hours=22) <= b - a <= timedelta(hours=26)

    @pytest.mark.parametrize("at", ["01:30", "02:30"])
    @pytest.mark.parametrize("start", [datetime(2027, 3, 27, 12, tzinfo=UTC), datetime(2027, 10, 30, 12, tzinfo=UTC)])
    def test_scheduler_tick_across_transition(self, hub, lamp, at, start):
        scheduler = Scheduler(hub, Location(timezone="Europe/London"), grace_seconds=3600)
        scheduler.add(schedule_from_dict({"name": "night", "at": at, "then": {"device": "lamp", "command": "on"}}))
        scheduler._due["night"] = scheduler.schedules[0].next_run(start, scheduler.location)
        fired_days = []
        now = start
        while now < start + timedelta(days=2):
            now += timedelta(minutes=15)
            if scheduler.tick(now=now):
                fired_days.append(now.astimezone(scheduler.location.tz).date())
        assert len(fired_days) == len(set(fired_days)) == 2
        wait_for(lambda: scheduler.schedules[0].run_count == 2)


# --------------------------------------------------------------------------- schedule_from_dict


class TestScheduleFromDict:
    def test_full(self):
        s = schedule_from_dict({"name": "eve", "at": "sunset+10m", "days": "weekdays",
                                "then": [{"device": "lamp", "command": "on"},
                                         {"notify": "phone", "message": "Lights on"}],
                                "enabled": False})
        assert s.name == "eve" and s.days == {0, 1, 2, 3, 4} and not s.enabled
        assert isinstance(s.actions[0], Action) and isinstance(s.actions[1], NotifyAction)

    @pytest.mark.parametrize("days,expected", [
        ("daily", set(range(7))), ("everyday", set(range(7))), ("weekends", {5, 6}),
        ("Weekdays", {0, 1, 2, 3, 4}), (["mon", "Wednesday", "FRI"], {0, 2, 4}), ("sat, sun", {5, 6}),
        (None, set(range(7))),
    ])
    def test_days(self, days, expected):
        spec = {"name": "d", "at": "10:00", "then": {"device": "lamp", "command": "on"}}
        if days is not None:
            spec["days"] = days
        assert schedule_from_dict(spec).days == expected

    def test_single_action_mapping_and_cron(self):
        s = schedule_from_dict({"name": "c", "cron": "0 7 * * 1-5", "then": {"device": "lamp", "command": "on"}})
        assert s.cron == "0 7 * * 1-5" and len(s.actions) == 1

    @pytest.mark.parametrize("spec,fragment", [
        ("not a mapping", "must be a mapping"),
        ({"at": "10:00", "then": {"device": "x", "command": "on"}}, "missing 'name'"),
        ({"name": "n", "at": "10:00"}, "missing 'then'"),
        ({"name": "n", "at": "10:00", "then": []}, "'then' is empty"),
        ({"name": "n", "at": "10:00", "then": "lamp on"}, "must be a mapping or a list"),
        ({"name": "n", "at": "10:00", "then": {"device": "x"}}, "needs 'device' and 'command'"),
        ({"name": "n", "at": "10:00", "then": {"device": "x", "command": "on"}, "when": 1}, "unknown keys"),
        ({"name": "n", "at": "10:00", "days": ["funday"], "then": {"device": "x", "command": "on"}}, "unknown day"),
        ({"name": "n", "at": "10:00", "days": [], "then": {"device": "x", "command": "on"}}, "'days' is empty"),
        ({"name": "n", "then": {"device": "x", "command": "on"}}, "exactly one"),
        ({"name": "n", "at": "10:00", "then": {"notify": "p"}}, "needs a 'message'"),
    ])
    def test_errors(self, spec, fragment):
        with pytest.raises(ConfigurationError, match=fragment):
            schedule_from_dict(spec)

    def test_scalar_days_is_configuration_error(self):
        with pytest.raises(ConfigurationError):
            schedule_from_dict({"name": "n", "at": "10:00", "days": 1, "then": {"device": "x", "command": "on"}})


# --------------------------------------------------------------------------- Scheduler


@pytest.fixture
def scheduler(hub, lamp):
    s = Scheduler(hub, Location(timezone="UTC"))
    yield s
    s.stop()


def lamp_on(name="s", at="10:00", **extra):
    return schedule_from_dict({"name": name, "at": at, "then": {"device": "lamp", "command": "on"}, **extra})


class TestSchedulerConfig:
    def test_from_config(self, hub, lamp):
        s = Scheduler.from_config(hub, [{"name": "a", "at": "sunset", "then": {"device": "lamp", "command": "on"}},
                                        {"name": "b", "cron": "0 * * * *",
                                         "then": {"notify": "phone", "message": "hi"}}], LONDON)
        assert [x.name for x in s.schedules] == ["a", "b"]
        assert str(s.location.tz) == "Europe/London"

    def test_unknown_device(self, hub, lamp):
        with pytest.raises(ConfigurationError, match="unknown device 'ghost'"):
            Scheduler.from_config(hub, [{"name": "a", "at": "10:00", "then": {"device": "ghost", "command": "on"}}])

    def test_sun_without_coordinates(self, hub, lamp):
        with pytest.raises(ConfigurationError, match="uses sunrise/sunset"):
            Scheduler.from_config(hub, [{"name": "a", "at": "sunrise", "then": {"device": "lamp", "command": "on"}}],
                                  {"timezone": "UTC"})

    def test_bad_location(self, hub):
        with pytest.raises(ConfigurationError, match="Unknown timezone"):
            Scheduler.from_config(hub, [], {"timezone": "Nowhere/Land"})

    def test_duplicate_names(self, scheduler):
        scheduler.add(lamp_on("x"))
        with pytest.raises(ConfigurationError, match="Duplicate schedule name"):
            scheduler.add(lamp_on("x", at="11:00"))


class TestSchedulerManagement:
    def test_list_get_remove(self, scheduler):
        scheduler.add(lamp_on("a"))
        scheduler.add(lamp_on("b", cron="*/5 * * * *", at=None))
        names = [s["name"] for s in scheduler.list_schedules()]
        assert names == ["a", "b"]
        assert all(s["next_run"] for s in scheduler.list_schedules())
        assert scheduler.get("a").name == "a" and scheduler.get("zzz") is None
        assert scheduler.remove("a") is True and scheduler.remove("a") is False
        assert [s["name"] for s in scheduler.list_schedules()] == ["b"]

    def test_set_enabled(self, scheduler):
        scheduler.add(lamp_on("a"))
        assert scheduler._due["a"] is not None
        assert scheduler.set_enabled("a", False) is True
        assert scheduler._due["a"] is None
        assert scheduler.list_schedules()[0]["enabled"] is False
        assert scheduler.list_schedules()[0]["next_run"] is None
        assert scheduler.set_enabled("a", True) and scheduler._due["a"] is not None
        assert scheduler.set_enabled("nope", True) is False

    def test_disabled_schedule_never_fires(self, scheduler):
        scheduler.add(lamp_on("a", enabled=False))
        assert scheduler._due["a"] is None
        assert scheduler.tick(now=datetime.now(UTC) + timedelta(days=3)) == []

    def test_run_now(self, scheduler, lamp):
        scheduler.add(lamp_on("a"))
        assert scheduler.run_now("a") is True
        assert lamp.is_on
        s = scheduler.get("a")
        assert s.run_count == 1 and s.last_ok is True and s.last_run is not None
        assert scheduler.run_now("missing") is False

    def test_run_now_reports_failure(self, hub, add_virtual, scheduler):
        add_virtual("switchable", Switchable, "broken", failure_rate=1.0)
        scheduler.add(schedule_from_dict({"name": "f", "at": "10:00", "then": {"device": "broken", "command": "on"}}))
        assert scheduler.run_now("f") is False
        assert scheduler.get("f").last_ok is False and scheduler.get("f").run_count == 1

    def test_disallowed_command_fails(self, hub, add_virtual, scheduler):
        add_virtual("sensor", Sensor, "temp")
        scheduler.add(schedule_from_dict({"name": "f", "at": "10:00", "then": {"device": "temp", "command": "on"}}))
        assert scheduler.run_now("f") is False

    def test_runs_notify_actions_with_schedule_context(self, hub, scheduler):
        channel = LogChannel("log")
        hub.notifier = NotificationManager({"log": channel})
        scheduler.add(schedule_from_dict({"name": "greet", "at": "10:00",
                                          "then": {"notify": "log", "message": "from {schedule}", "title": "T"}}))
        assert scheduler.run_now("greet") is True
        assert channel.sent[0]["message"] == "from greet" and channel.sent[0]["title"] == "T"


class TestSchedulerTick:
    def test_fires_when_due_and_reschedules(self, scheduler, lamp):
        scheduler.add(lamp_on("a"))
        due = scheduler._due["a"]
        assert scheduler.tick(now=due - timedelta(seconds=1)) == []
        assert scheduler.tick(now=due) == ["a"]
        wait_for(lambda: lamp.is_on)
        wait_for(lambda: scheduler.get("a").run_count == 1)
        assert scheduler.get("a").last_run == due
        assert scheduler._due["a"] == due + timedelta(days=1)
        assert scheduler.tick(now=due + timedelta(seconds=5)) == []

    def test_fires_within_grace(self, scheduler):
        scheduler.add(lamp_on("a"))
        due = scheduler._due["a"]
        assert scheduler.tick(now=due + timedelta(seconds=scheduler.grace_seconds - 1)) == ["a"]
        wait_for(lambda: scheduler.get("a").run_count == 1)

    def test_missed_run_beyond_grace_is_skipped(self, scheduler, lamp):
        scheduler.add(lamp_on("a"))
        due = scheduler._due["a"]
        late = due + timedelta(seconds=scheduler.grace_seconds + 60)
        assert scheduler.tick(now=late) == []
        assert scheduler._due["a"] == due + timedelta(days=1)
        time.sleep(0.05)
        assert scheduler.get("a").run_count == 0 and not lamp.is_on

    def test_long_outage_skips_to_future_run(self, scheduler):
        scheduler.add(lamp_on("a"))
        due = scheduler._due["a"]
        late = due + timedelta(days=3, hours=1)
        assert scheduler.tick(now=late) == []
        assert scheduler._due["a"] > late

    def test_multiple_due_schedules(self, scheduler):
        scheduler.add(lamp_on("a", at="10:00"))
        scheduler.add(lamp_on("b", at="10:00"))
        scheduler.add(lamp_on("c", at="18:00"))
        due = scheduler._due["a"]
        assert sorted(scheduler.tick(now=due)) == ["a", "b"]
        wait_for(lambda: scheduler.get("a").run_count == scheduler.get("b").run_count == 1)

    def test_cron_schedule_ticks(self, scheduler):
        scheduler.add(schedule_from_dict({"name": "q", "cron": "*/15 * * * *",
                                          "then": {"device": "lamp", "command": "toggle"}}))
        due = scheduler._due["q"]
        assert due.minute % 15 == 0
        assert scheduler.tick(now=due) == ["q"]
        assert scheduler._due["q"] == due + timedelta(minutes=15)
        wait_for(lambda: scheduler.get("q").run_count == 1)


class TestSchedulerBackground:
    def test_background_fires_and_stops_promptly(self, scheduler, lamp):
        s = lamp_on("soon")
        scheduler.add(s)
        scheduler._due["soon"] = datetime.now(UTC) + timedelta(milliseconds=100)
        scheduler._wake.set()
        scheduler.start_background()
        wait_for(lambda: s.run_count == 1)
        assert lamp.is_on
        started = time.monotonic()
        scheduler.stop()
        assert time.monotonic() - started < 3
        assert scheduler._thread is None

    def test_stop_wakes_long_wait(self, scheduler):
        scheduler.add(lamp_on("later", at="10:00"))
        scheduler.start_background()
        time.sleep(0.05)
        t0 = time.monotonic()
        scheduler.stop()
        assert time.monotonic() - t0 < 3
        assert not any(t.name == "fuseiot-scheduler" for t in threading.enumerate())

    def test_stop_without_start(self, hub):
        Scheduler(hub).stop()
