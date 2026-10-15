"""
Sunrise and sunset times (NOAA algorithm, accurate to about two minutes).

No network access or external libraries are needed; only the location.
"""
import math
from datetime import date, datetime, timedelta, timezone, tzinfo
from typing import Optional

_ZENITH_OFFICIAL = 90.833  # sun centre 50' below the horizon (refraction + radius)


def _utc_hours(day: date, latitude: float, longitude: float, rising: bool) -> Optional[float]:
    """UTC hour of the event on ``day`` (may fall outside 0-24), or None in polar day/night."""
    n = day.timetuple().tm_yday
    lng_hour = longitude / 15.0
    t = n + ((6.0 if rising else 18.0) - lng_hour) / 24.0
    m = 0.9856 * t - 3.289
    true_long = (m + 1.916 * math.sin(math.radians(m)) + 0.020 * math.sin(math.radians(2 * m)) + 282.634) % 360
    ra = math.degrees(math.atan(0.91764 * math.tan(math.radians(true_long)))) % 360
    ra += (math.floor(true_long / 90) * 90) - (math.floor(ra / 90) * 90)
    ra /= 15.0
    sin_dec = 0.39782 * math.sin(math.radians(true_long))
    cos_dec = math.cos(math.asin(sin_dec))
    cos_h = (math.cos(math.radians(_ZENITH_OFFICIAL)) - sin_dec * math.sin(math.radians(latitude))) / (
        cos_dec * math.cos(math.radians(latitude))
    )
    if cos_h > 1 or cos_h < -1:
        return None  # the sun never rises / never sets on this day
    h = (360 - math.degrees(math.acos(cos_h))) if rising else math.degrees(math.acos(cos_h))
    h /= 15.0
    local_mean = h + ra - 0.06571 * t - 6.622
    return (local_mean - lng_hour) % 24


def sun_event(day: date, latitude: float, longitude: float, tz: tzinfo, rising: bool) -> Optional[datetime]:
    """
    Local time of sunrise (``rising=True``) or sunset on the *local* calendar ``day``.

    Returns None when the event does not happen that day (polar regions).
    """
    for candidate in (day, day - timedelta(days=1), day + timedelta(days=1)):
        hours = _utc_hours(candidate, latitude, longitude, rising)
        if hours is None:
            continue
        utc = datetime(candidate.year, candidate.month, candidate.day, tzinfo=timezone.utc) + timedelta(hours=hours)
        local = utc.astimezone(tz)
        if local.date() == day:
            return local.replace(microsecond=0)
    return None


def sunrise(day: date, latitude: float, longitude: float, tz: tzinfo) -> Optional[datetime]:
    return sun_event(day, latitude, longitude, tz, rising=True)


def sunset(day: date, latitude: float, longitude: float, tz: tzinfo) -> Optional[datetime]:
    return sun_event(day, latitude, longitude, tz, rising=False)
