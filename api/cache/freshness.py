"""Freshness policy for cached /historical entries, tied to PSX trading hours.

Past rows never change; only today's row moves while the market is open. Inside a padded
trading window an entry stays fresh for ``market_ttl`` (capped at the window close); outside
it, an entry stays fresh until the next window opens.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

PKT = ZoneInfo("Asia/Karachi")
WINDOW_OPEN = time(9, 0)
WINDOW_CLOSE = time(17, 0)
DEFAULT_MARKET_TTL = timedelta(minutes=30)


def _is_weekday(day: date) -> bool:
    return day.weekday() < 5


def fresh_until(fetched_at: datetime, market_ttl: timedelta = DEFAULT_MARKET_TTL) -> datetime:
    """Return the UTC instant after which data fetched at ``fetched_at`` is stale."""
    if fetched_at.tzinfo is None:
        raise ValueError("fetched_at must be timezone-aware")
    local = fetched_at.astimezone(PKT)
    today = local.date()
    window_open = datetime.combine(today, WINDOW_OPEN, tzinfo=PKT)
    window_close = datetime.combine(today, WINDOW_CLOSE, tzinfo=PKT)

    if _is_weekday(today) and window_open <= local < window_close:
        return min(local + market_ttl, window_close).astimezone(timezone.utc)

    day = today if _is_weekday(today) and local < window_open else today + timedelta(days=1)
    while not _is_weekday(day):
        day += timedelta(days=1)
    return datetime.combine(day, WINDOW_OPEN, tzinfo=PKT).astimezone(timezone.utc)
