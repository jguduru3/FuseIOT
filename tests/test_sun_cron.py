"""Sunrise/sunset calculation and cron expressions."""
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from fuseiot.automation.cron import CronExpression
from fuseiot.automation.sun import sun_event, sunrise, sunset
from fuseiot.exceptions import ConfigurationError

LONDON = (51.5074, -0.1278, ZoneInfo("Europe/London"))
SYDNEY = (-33.8688, 151.2093, ZoneInfo("Australia/Sydney"))
TROMSO = (69.65, 18.96, ZoneInfo("Europe/Oslo"))


def close_to(actual, hh, mm, minutes=3):
    target = actual.replace(hour=hh, minute=mm, second=0)
    return abs((actual - target).total_seconds()) <= minutes * 60


class TestSun:
    def test_london_midsummer(self):
        lat, lon, tz = LONDON
        rise = sunrise(date(2026, 6, 21), lat, lon, tz)
        fall = sunset(date(2026, 6, 21), lat, lon, tz)
        assert close_to(rise, 4, 43) and close_to(fall, 21, 21)
        assert rise.date() == fall.date() == date(2026, 6, 21)
        assert rise.utcoffset() == timedelta(hours=1)  # BST
        assert rise.microsecond == 0

    def test_london_midwinter_uses_gmt(self):
        lat, lon, tz = LONDON
        rise = sunrise(date(2026, 12, 21), lat, lon, tz)
        fall = sunset(date(2026, 12, 21), lat, lon, tz)
        assert rise.utcoffset() == timedelta(0)
        assert close_to(rise, 8, 4, 5) and close_to(fall, 15, 54, 5)
        assert (fall - rise) < timedelta(hours=8)

    def test_sydney_southern_summer(self):
        lat, lon, tz = SYDNEY
        rise = sunrise(date(2026, 12, 21), lat, lon, tz)
        fall = sunset(date(2026, 12, 21), lat, lon, tz)
        assert close_to(rise, 5, 41) and close_to(fall, 20, 5)
        assert rise.utcoffset() == timedelta(hours=11)  # AEDT

    def test_seasons_are_inverted_between_hemispheres(self):
        june, dec = date(2026, 6, 21), date(2026, 12, 21)

        def day_length(loc, d):
            lat, lon, tz = loc
            return sunset(d, lat, lon, tz) - sunrise(d, lat, lon, tz)

        assert day_length(LONDON, june) > day_length(LONDON, dec)
        assert day_length(SYDNEY, dec) > day_length(SYDNEY, june)

    def test_polar_day_and_night_return_none(self):
        lat, lon, tz = TROMSO
        assert sunset(date(2026, 6, 21), lat, lon, tz) is None
        assert sunrise(date(2026, 12, 21), lat, lon, tz) is None

    def test_tromso_has_both_events_at_equinox(self):
        lat, lon, tz = TROMSO
        rise = sunrise(date(2026, 3, 21), lat, lon, tz)
        fall = sunset(date(2026, 3, 21), lat, lon, tz)
        assert rise is not None and fall is not None and rise < fall

    def test_result_is_on_requested_local_day_far_from_utc(self):
        # Auckland is UTC+12/13: the UTC calendar day differs from the local one.
        tz = ZoneInfo("Pacific/Auckland")
        for d in (date(2026, 1, 10), date(2026, 7, 10)):
            rise = sunrise(d, -36.85, 174.76, tz)
            fall = sunset(d, -36.85, 174.76, tz)
            assert rise.date() == d and fall.date() == d and rise < fall

    def test_sun_event_matches_wrappers(self):
        lat, lon, tz = LONDON
        d = date(2026, 9, 1)
        assert sun_event(d, lat, lon, tz, rising=True) == sunrise(d, lat, lon, tz)
        assert sun_event(d, lat, lon, tz, rising=False) == sunset(d, lat, lon, tz)

    def test_consecutive_days_change_smoothly(self):
        lat, lon, tz = LONDON
        prev = None
        for i in range(30):
            d = date(2026, 4, 1) + timedelta(days=i)
            rise = sunrise(d, lat, lon, tz)
            if prev is not None:
                delta = (rise.replace(year=2000, month=1, day=1) - prev.replace(year=2000, month=1, day=1))
                assert abs(delta.total_seconds()) < 5 * 60
            prev = rise


class TestCronParsing:
    def test_fields(self):
        c = CronExpression("30 6 * * 1-5")
        assert c.minutes == [30] and c.hours == [6]
        assert c.weekdays == {1, 2, 3, 4, 5}
        assert c.months == set(range(1, 13))
        assert repr(c) == "CronExpression('30 6 * * 1-5')"

    def test_lists_ranges_steps(self):
        c = CronExpression("*/15 8-18/2 1,15 * *")
        assert c.minutes == [0, 15, 30, 45]
        assert c.hours == [8, 10, 12, 14, 16, 18]
        assert c.days == {1, 15}

    def test_single_value_with_step_runs_to_end(self):
        assert CronExpression("5/20 * * * *").minutes == [5, 25, 45]

    def test_names_case_insensitive(self):
        c = CronExpression("0 0 * JAN,jul MON-fri")
        assert c.months == {1, 7}
        assert c.weekdays == {1, 2, 3, 4, 5}

    def test_zero_and_seven_are_sunday(self):
        assert CronExpression("0 0 * * 7").weekdays == {0}
        assert CronExpression("0 0 * * 0").weekdays == {0}
        assert CronExpression("0 0 * * sun").weekdays == {0}

    @pytest.mark.parametrize("expr,fragment", [
        ("* * * *", "5 fields"),
        ("* * * * * *", "5 fields"),
        ("60 * * * *", "out of range"),
        ("* 24 * * *", "out of range"),
        ("* * 0 * *", "out of range"),
        ("* * * 13 *", "out of range"),
        ("* * * * 8", "out of range"),
        ("10-5 * * * *", "out of range"),
        ("*/0 * * * *", "step must be >= 1"),
        ("abc * * * *", "Invalid minute value"),
        ("* * * foo *", "Invalid month value"),
        ("* * * * fri-mon", "out of range"),
        ("*/x * * * *", "Invalid minute value"),
    ])
    def test_errors(self, expr, fragment):
        with pytest.raises(ConfigurationError, match=fragment):
            CronExpression(expr)


class TestCronNext:
    def test_next_after_same_day_and_rollover(self):
        c = CronExpression("30 6 * * *")
        assert c.next_after(datetime(2026, 10, 15, 5, 0)) == datetime(2026, 10, 15, 6, 30)
        assert c.next_after(datetime(2026, 10, 15, 6, 30)) == datetime(2026, 10, 16, 6, 30)
        assert c.next_after(datetime(2026, 12, 31, 23, 0)) == datetime(2027, 1, 1, 6, 30)

    def test_strictly_after_and_seconds_dropped(self):
        c = CronExpression("* * * * *")
        assert c.next_after(datetime(2026, 1, 1, 10, 0, 30)) == datetime(2026, 1, 1, 10, 1)

    def test_weekdays_only(self):
        c = CronExpression("0 9 * * mon-fri")
        # 2026-10-17 is a Saturday.
        assert c.next_after(datetime(2026, 10, 16, 10, 0)) == datetime(2026, 10, 19, 9, 0)

    def test_dom_or_dow_when_both_restricted(self):
        c = CronExpression("0 12 13 * fri")
        runs = list(_take(c.iter_after(datetime(2026, 11, 1)), 6))
        assert all(r.day == 13 or r.weekday() == 4 for r in runs)
        assert datetime(2026, 11, 6, 12) in runs   # a Friday, not the 13th
        assert datetime(2026, 11, 13, 12) in runs  # Friday the 13th
        # 2026-12-13 is a Sunday yet still matches through day-of-month.
        assert datetime(2026, 12, 13, 12) in list(_take(c.iter_after(datetime(2026, 12, 5)), 3))

    def test_dom_only_when_dow_is_star(self):
        c = CronExpression("0 0 1 * *")
        assert [r.day for r in _take(c.iter_after(datetime(2026, 1, 15)), 3)] == [1, 1, 1]

    def test_month_restriction(self):
        c = CronExpression("0 0 1 jun *")
        assert c.next_after(datetime(2026, 7, 1)) == datetime(2027, 6, 1)

    def test_leap_day(self):
        assert CronExpression("0 0 29 2 *").next_after(datetime(2026, 3, 1)) == datetime(2028, 2, 29)

    def test_impossible_date_returns_none(self):
        assert CronExpression("0 0 31 2 *").next_after(datetime(2026, 1, 1)) is None

    def test_iter_is_increasing(self):
        runs = list(_take(CronExpression("*/20 9-10 * * *").iter_after(datetime(2026, 5, 5)), 8))
        assert runs == sorted(runs) and len(set(runs)) == 8
        assert runs[:3] == [datetime(2026, 5, 5, 9, 0), datetime(2026, 5, 5, 9, 20), datetime(2026, 5, 5, 9, 40)]


def _take(it, n):
    for i, v in enumerate(it):
        if i >= n:
            return
        yield v
