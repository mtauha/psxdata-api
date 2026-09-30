"""Tests for the gzip JSON cache envelope."""
import gzip
import json
import logging
import math
from datetime import datetime, timezone

import numpy as np

from api.cache.codec import COLUMNS, CacheEntry, decode, encode

T0 = datetime(2026, 9, 29, 5, 0, tzinfo=timezone.utc)
T1 = datetime(2026, 9, 29, 5, 30, tzinfo=timezone.utc)
ROWS = [
    {"date": "2026-09-29", "open": 10.0, "high": 11.0, "low": 9.5, "close": 10.5,
     "volume": 1000, "is_anomaly": False},
    {"date": "2026-09-28", "open": 9.0, "high": 10.0, "low": 8.5, "close": 9.5,
     "volume": 900, "is_anomaly": True},
]


def test_round_trip_preserves_entry_and_row_order() -> None:
    entry = CacheEntry("SYS", T0, T1, ROWS)
    assert decode(encode(entry)) == entry


def test_round_trip_preserves_nulls_nan_and_numpy_scalars() -> None:
    rows = [{"date": "2026-09-29", "open": np.float64(1.5), "high": np.float64(2.0),
             "low": np.float64(1.0), "close": float("nan"), "volume": None,
             "is_anomaly": np.bool_(True)}]
    decoded = decode(encode(CacheEntry("SYS", T0, T1, rows)))
    assert decoded is not None
    row = decoded.rows[0]
    assert math.isnan(row.pop("close"))      # NaN stays NaN, exactly as uncached
    assert row == {"date": "2026-09-29", "open": 1.5, "high": 2.0, "low": 1.0,
                   "volume": None, "is_anomaly": True}


def test_empty_history_round_trips() -> None:
    entry = CacheEntry("NOPE", T0, T1, [])
    assert decode(encode(entry)) == entry


def test_only_known_columns_are_kept() -> None:
    rows = [dict(ROWS[0], symbol="SYS", extra=1)]
    decoded = decode(encode(CacheEntry("SYS", T0, T1, rows)))
    assert decoded is not None
    assert set(decoded.rows[0]) == {"date", "open", "high", "low", "close", "volume",
                                    "is_anomaly"}


def test_payload_is_versioned_gzip_json() -> None:
    payload = json.loads(gzip.decompress(encode(CacheEntry("SYS", T0, T1, ROWS))))
    assert payload["v"] == 1
    assert payload["columns"][0] == "date"


def test_unreadable_input_returns_none(caplog) -> None:
    caplog.set_level(logging.WARNING)
    good = encode(CacheEntry("SYS", T0, T1, ROWS))
    assert decode(b"not gzip at all") is None
    assert decode(good[:20]) is None
    assert decode(gzip.compress(json.dumps({"v": 99}).encode())) is None
    assert decode(gzip.compress(json.dumps({"v": 1}).encode())) is None
    assert decode(gzip.compress(b"[1, 2]")) is None
    assert "cache entry" in caplog.text


def _envelope(**overrides) -> bytes:
    payload = {
        "v": 1, "symbol": "SYS",
        "fetched_at": T0.isoformat(), "fresh_until": T1.isoformat(),
        "columns": list(COLUMNS),
        "rows": [["2026-09-29", 1.0, 1.0, 1.0, 1.0, 1, False]],
    }
    payload.update(overrides)
    return gzip.compress(json.dumps(payload).encode())


def test_structurally_wrong_envelopes_return_none() -> None:
    assert decode(_envelope()) is not None                                  # control
    assert decode(_envelope(columns=["other"], rows=[[1]])) is None         # wrong columns
    assert decode(_envelope(columns=list(reversed(COLUMNS)))) is None       # reordered
    assert decode(_envelope(rows=[["2026-09-29", 1.0]])) is None            # short row
    assert decode(_envelope(rows=[[20260929, 1.0, 1.0, 1.0, 1.0, 1, False]])) is None  # date type
    assert decode(_envelope(rows=["not a list"])) is None
    assert decode(_envelope(rows={"a": 1})) is None
    assert decode(_envelope(symbol=7)) is None
    assert decode(_envelope(fetched_at="2026-09-29T05:00:00")) is None      # naive timestamp
    assert decode(_envelope(fresh_until=12345)) is None
