"""
Minimal cron expressions: ``minute hour day-of-month month day-of-week``.

Supports ``*``, lists (``1,15``), ranges (``1-5``), steps (``*/10``, ``8-18/2``)
and names for months (``jan``) and weekdays (``mon``; 0 and 7 are Sunday).
Like classic cron, when both day-of-month and day-of-week are restricted a
day matches if *either* does.
"""
from datetime import datetime, timedelta
from typing import Iterator, List, Optional, Set, Tuple

from ..exceptions import ConfigurationError

_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}
_DAYS = {d: i for i, d in enumerate(["sun", "mon", "tue", "wed", "thu", "fri", "sat"])}
_FIELDS: List[Tuple[str, int, int, dict]] = [
    ("minute", 0, 59, {}),
    ("hour", 0, 23, {}),
    ("day of month", 1, 31, {}),
    ("month", 1, 12, _MONTHS),
    ("day of week", 0, 7, _DAYS),
]


def _value(token: str, names: dict, field: str) -> int:
    token = token.strip().lower()
    if token in names:
        return names[token]
    try:
        return int(token)
    except ValueError:
        raise ConfigurationError(f"Invalid {field} value {token!r} in cron expression") from None


def _parse_field(text: str, field: str, lo: int, hi: int, names: dict) -> Tuple[Set[int], bool]:
    values: Set[int] = set()
    for part in text.split(","):
        step = 1
        if "/" in part:
            part, step_text = part.split("/", 1)
            step = _value(step_text, {}, field)
            if step < 1:
                raise ConfigurationError(f"Cron step must be >= 1 in {field}")
        if part in ("*", ""):
            start, end = lo, hi
        elif "-" in part:
            a, b = part.split("-", 1)
            start, end = _value(a, names, field), _value(b, names, field)
        else:
            start = _value(part, names, field)
            end = hi if step > 1 else start
        if not (lo <= start <= hi and lo <= end <= hi) or start > end:
            raise ConfigurationError(f"Cron {field} out of range {lo}-{hi}: {text!r}")
        values.update(range(start, end + 1, step))
    return values, text.strip() == "*"


class CronExpression:
    """A parsed cron expression with :meth:`next_after`."""

    def __init__(self, expression: str):
        parts = expression.split()
        if len(parts) != 5:
            raise ConfigurationError(
                f"Cron expression needs 5 fields (minute hour day month weekday), got {expression!r}"
            )
        parsed = [_parse_field(p, f, lo, hi, names) for p, (f, lo, hi, names) in zip(parts, _FIELDS)]
        self.expression = expression
        self.minutes = sorted(parsed[0][0])
        self.hours = sorted(parsed[1][0])
        self.days, self._dom_any = parsed[2]
        self.months = parsed[3][0]
        dows, self._dow_any = parsed[4]
        self.weekdays = {d % 7 for d in dows}  # 0 = Sunday

    def _day_matches(self, day: datetime) -> bool:
        if day.month not in self.months:
            return False
        cron_dow = (day.weekday() + 1) % 7  # Python Monday=0 -> cron Sunday=0
        dom_ok = day.day in self.days
        dow_ok = cron_dow in self.weekdays
        if self._dom_any and self._dow_any:
            return True
        if self._dom_any:
            return dow_ok
        if self._dow_any:
            return dom_ok
        return dom_ok or dow_ok

    def iter_after(self, after: datetime) -> Iterator[datetime]:
        """Naive local datetimes strictly after ``after`` (seconds dropped)."""
        start = after.replace(second=0, microsecond=0)
        day = start.replace(hour=0, minute=0)
        for _ in range(366 * 5):
            if self._day_matches(day):
                for hour in self.hours:
                    for minute in self.minutes:
                        candidate = day.replace(hour=hour, minute=minute)
                        if candidate > after:
                            yield candidate
            day += timedelta(days=1)

    def next_after(self, after: datetime) -> Optional[datetime]:
        return next(self.iter_after(after), None)

    def __repr__(self) -> str:
        return f"CronExpression({self.expression!r})"
