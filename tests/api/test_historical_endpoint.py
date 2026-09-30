"""Tests for GET /stocks/{symbol}/historical with the cache in front of PSX."""
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pandas as pd
import pytest
import redis
from fastapi.testclient import TestClient
from psxdata.exceptions import PSXRateLimitError

from api.cache.historical import HistoricalService
from api.cache.store import MemoryLRU, RedisStore, TieredStore
from api.main import app


def _history() -> pd.DataFrame:
    # PSX order: newest first
    return pd.DataFrame({
        "date": pd.to_datetime(["2024-01-03", "2024-01-02", "2024-01-01"]),
        "open": [3.0, 2.0, 1.0], "high": [3.5, 2.5, 1.5], "low": [2.5, 1.5, 0.5],
        "close": [3.2, 2.2, 1.2], "volume": [300, 200, 100],
        "is_anomaly": [False, False, False],
    })


class Clock:
    """Tuesday 2024-01-02 10:00 PKT — in the past, so real-clock Age is positive."""

    def __init__(self) -> None:
        self.t = datetime(2024, 1, 2, 5, 0, tzinfo=timezone.utc)
        self.mono = 1000.0

    def now(self) -> datetime:
        return self.t

    def monotonic(self) -> float:
        return self.mono

    def advance(self, seconds: float) -> None:
        self.t += timedelta(seconds=seconds)
        self.mono += seconds


class DownRedis:
    def get(self, key):
        raise redis.ConnectionError("down")

    def set(self, key, value, ex=None):
        raise redis.ConnectionError("down")

    def close(self) -> None:
        pass


@pytest.fixture
def client() -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


def _install(service_clock: Clock | None = None, l2_client=None) -> None:
    kwargs = {}
    if service_clock is not None:
        kwargs = {"now": service_clock.now, "monotonic": service_clock.monotonic}
    store = TieredStore(MemoryLRU(), RedisStore(l2_client))
    app.state.historical_service = HistoricalService(store, **kwargs)


def test_first_request_is_miss_then_hit(client: TestClient) -> None:
    with patch("psxdata.stocks", return_value=_history()) as mock_stocks:
        first = client.get("/stocks/SYS/historical")
        second = client.get("/stocks/SYS/historical")
    assert first.status_code == 200 and second.status_code == 200
    assert first.headers["X-Cache"] == "MISS"
    assert second.headers["X-Cache"] == "HIT"
    assert "Age" not in first.headers
    assert int(second.headers["Age"]) >= 0
    assert first.json()["meta"]["cached"] is False
    assert second.json()["meta"]["cached"] is True
    assert second.json()["data"] == first.json()["data"]
    assert [r["date"] for r in first.json()["data"]] == ["2024-01-03", "2024-01-02", "2024-01-01"]
    mock_stocks.assert_called_once_with("SYS", cache=False)


def test_symbol_case_shares_cache_entry(client: TestClient) -> None:
    with patch("psxdata.stocks", return_value=_history()) as mock_stocks:
        client.get("/stocks/sys/historical")
        resp = client.get("/stocks/SyS/historical")
    assert resp.headers["X-Cache"] == "HIT"
    mock_stocks.assert_called_once_with("SYS", cache=False)


def test_unknown_symbol_empty_history_is_cached(client: TestClient) -> None:
    with patch("psxdata.stocks", return_value=pd.DataFrame()) as mock_stocks:
        first = client.get("/stocks/NOPE/historical")
        second = client.get("/stocks/NOPE/historical")
    assert first.status_code == 200 and first.json()["data"] == []
    assert second.headers["X-Cache"] == "HIT" and second.json()["data"] == []
    assert mock_stocks.call_count == 1


@pytest.mark.parametrize(("query", "expected"), [
    ("", ["2024-01-03", "2024-01-02", "2024-01-01"]),
    ("?start=2024-01-02", ["2024-01-03", "2024-01-02"]),
    ("?end=2024-01-02", ["2024-01-02", "2024-01-01"]),
    ("?start=2024-01-02&end=2024-01-02", ["2024-01-02"]),
    ("?start=2025-01-01", []),
])
def test_date_range_is_sliced_from_cached_history(
    client: TestClient, query: str, expected: list[str]
) -> None:
    with patch("psxdata.stocks", return_value=_history()):
        resp = client.get(f"/stocks/SYS/historical{query}")
    assert resp.status_code == 200
    assert [r["date"] for r in resp.json()["data"]] == expected
    assert resp.json()["meta"]["count"] == len(expected)


@pytest.mark.parametrize("query", [
    "?start=2024-13-01",
    "?end=yesterday",
    "?start=20240102",
    "?end=2024-W01-2",
    "?start=2024-01-03&end=2024-01-01",
])
def test_bad_dates_return_422_without_calling_psx(client: TestClient, query: str) -> None:
    with patch("psxdata.stocks", return_value=_history()) as mock_stocks:
        resp = client.get(f"/stocks/SYS/historical{query}")
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "bad_request"
    mock_stocks.assert_not_called()


def test_missing_values_behave_like_uncached_on_miss_and_hit(client: TestClient) -> None:
    # NaN close: 0.2.x returned 200 with close=null (pydantic serialises NaN as null).
    nan_close = _history()
    nan_close.loc[1, "close"] = float("nan")
    with patch("psxdata.stocks", return_value=nan_close):
        miss = client.get("/stocks/NANC/historical")
        hit = client.get("/stocks/NANC/historical")
    assert miss.status_code == 200 and hit.status_code == 200
    assert hit.headers["X-Cache"] == "HIT"
    assert miss.json()["data"][1]["close"] is None
    assert hit.json()["data"] == miss.json()["data"]

    # Missing Int64 volume: 0.2.x returned 502 (OHLCVRow.volume is required). Unchanged.
    na_volume = _history()
    na_volume["volume"] = pd.array([300, pd.NA, 100], dtype="Int64")
    with patch("psxdata.stocks", return_value=na_volume) as mock_stocks:
        miss = client.get("/stocks/NAVOL/historical")
        hit = client.get("/stocks/NAVOL/historical")
    assert miss.status_code == 502 and hit.status_code == 502
    assert hit.json()["error"]["code"] == "upstream_data_error"
    assert mock_stocks.call_count == 1


def test_psx_rate_limit_serves_stale_copy(client: TestClient) -> None:
    clock = Clock()
    _install(clock)
    with patch("psxdata.stocks", return_value=_history()):
        client.get("/stocks/SYS/historical")
    clock.advance(31 * 60)
    with patch("psxdata.stocks", side_effect=PSXRateLimitError("429")):
        resp = client.get("/stocks/SYS/historical")
    assert resp.status_code == 200
    assert resp.headers["X-Cache"] == "STALE"
    assert int(resp.headers["Age"]) > 0
    assert resp.json()["meta"]["cached"] is True
    assert len(resp.json()["data"]) == 3


def test_psx_rate_limit_without_copy_returns_503(client: TestClient) -> None:
    with patch("psxdata.stocks", side_effect=PSXRateLimitError("429")):
        resp = client.get("/stocks/SYS/historical")
    assert resp.status_code == 503
    assert resp.headers["Retry-After"] == "60"
    assert resp.json()["error"]["code"] == "psx_unavailable"


def test_redis_down_still_serves(client: TestClient) -> None:
    _install(l2_client=DownRedis())
    with patch("psxdata.stocks", return_value=_history()) as mock_stocks:
        first = client.get("/stocks/SYS/historical")
        second = client.get("/stocks/SYS/historical")
    assert first.status_code == 200 and first.headers["X-Cache"] == "MISS"
    assert second.status_code == 200 and second.headers["X-Cache"] == "HIT"
    assert mock_stocks.call_count == 1


def test_openapi_documents_cache_headers_and_503() -> None:
    op = app.openapi()["paths"]["/stocks/{symbol}/historical"]["get"]
    assert "X-Cache" in op["responses"]["200"]["headers"]
    assert "Age" in op["responses"]["200"]["headers"]
    assert "Retry-After" in op["responses"]["503"]["headers"]
