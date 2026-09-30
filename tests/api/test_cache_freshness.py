"""Tests for the trading-hours freshness policy."""
from datetime import datetime, timedelta, timezone

import pytest

from api.cache.freshness import DEFAULT_MARKET_TTL, PKT, fresh_until


def pkt(y: int, m: int, d: int, hh: int, mm: int = 0, ss: int = 0) -> datetime:
    return datetime(y, m, d, hh, mm, ss, tzinfo=PKT)


# 2026-09-29 is a Tuesday; 2026-10-02 Friday, 10-03 Saturday, 10-04 Sunday, 10-05 Monday.
CASES = [
    ("pre-open", pkt(2026, 9, 29, 8, 50), pkt(2026, 9, 29, 9, 0)),
    ("open edge", pkt(2026, 9, 29, 9, 0), pkt(2026, 9, 29, 9, 30)),
    ("in window", pkt(2026, 9, 29, 10, 0), pkt(2026, 9, 29, 10, 30)),
    ("capped at close", pkt(2026, 9, 29, 16, 45), pkt(2026, 9, 29, 17, 0)),
    ("last second", pkt(2026, 9, 29, 16, 59, 59), pkt(2026, 9, 29, 17, 0)),
    ("close edge", pkt(2026, 9, 29, 17, 0), pkt(2026, 9, 30, 9, 0)),
    ("evening", pkt(2026, 9, 29, 17, 1), pkt(2026, 9, 30, 9, 0)),
    ("before midnight", pkt(2026, 9, 29, 23, 59), pkt(2026, 9, 30, 9, 0)),
    ("friday evening", pkt(2026, 10, 2, 17, 1), pkt(2026, 10, 5, 9, 0)),
    ("saturday midday", pkt(2026, 10, 3, 12, 0), pkt(2026, 10, 5, 9, 0)),
    ("saturday early", pkt(2026, 10, 3, 8, 0), pkt(2026, 10, 5, 9, 0)),
    ("sunday night", pkt(2026, 10, 4, 20, 0), pkt(2026, 10, 5, 9, 0)),
    ("monday pre-open", pkt(2026, 10, 5, 3, 0), pkt(2026, 10, 5, 9, 0)),
]


@pytest.mark.parametrize(("label", "fetched", "expected"), CASES, ids=[c[0] for c in CASES])
def test_fresh_until(label: str, fetched: datetime, expected: datetime) -> None:
    assert fresh_until(fetched) == expected


def test_result_is_utc() -> None:
    result = fresh_until(pkt(2026, 9, 29, 10, 0))
    assert result.utcoffset() == timedelta(0)


def test_accepts_utc_input() -> None:
    # 05:00 UTC == 10:00 PKT (UTC+5, no DST)
    fetched = datetime(2026, 9, 29, 5, 0, tzinfo=timezone.utc)
    assert fresh_until(fetched) == datetime(2026, 9, 29, 5, 30, tzinfo=timezone.utc)


def test_custom_market_ttl() -> None:
    assert fresh_until(pkt(2026, 9, 29, 10, 0), timedelta(minutes=15)) == pkt(2026, 9, 29, 10, 15)


def test_default_market_ttl_is_30_minutes() -> None:
    assert DEFAULT_MARKET_TTL == timedelta(minutes=30)


def test_naive_datetime_rejected() -> None:
    with pytest.raises(ValueError):
        fresh_until(datetime(2026, 9, 29, 10, 0))
