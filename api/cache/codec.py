"""Serialisation of cached /historical entries: a versioned, gzip-compressed JSON envelope.

Rows are stored as arrays under a single ``columns`` list, which is roughly 20% smaller than
repeating the keys on every row.
"""
from __future__ import annotations

import gzip
import json
import logging
import zlib
from dataclasses import dataclass
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)

FORMAT_VERSION = 1
COLUMNS: tuple[str, ...] = ("date", "open", "high", "low", "close", "volume", "is_anomaly")


@dataclass(frozen=True)
class CacheEntry:
    symbol: str
    fetched_at: datetime
    fresh_until: datetime
    rows: list[dict[str, Any]]


def _json_default(value: Any) -> Any:
    # numpy / pandas scalars expose .item() returning the native Python value
    if hasattr(value, "item"):
        return value.item()
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serialisable")


def encode(entry: CacheEntry) -> bytes:
    payload = {
        "v": FORMAT_VERSION,
        "symbol": entry.symbol,
        "fetched_at": entry.fetched_at.isoformat(),
        "fresh_until": entry.fresh_until.isoformat(),
        "columns": list(COLUMNS),
        "rows": [[row.get(col) for col in COLUMNS] for row in entry.rows],
    }
    raw = json.dumps(payload, default=_json_default, separators=(",", ":")).encode()
    return gzip.compress(raw, compresslevel=6)


def _aware(value: Any) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("cache timestamp is not timezone-aware")
    return parsed


def _row(values: Any) -> dict[str, Any]:
    if not isinstance(values, list) or len(values) != len(COLUMNS):
        raise ValueError("cache row has the wrong shape")
    if not isinstance(values[0], str):
        raise TypeError("cache row date is not a string")
    return dict(zip(COLUMNS, values))


def decode(blob: bytes) -> CacheEntry | None:
    """Return the entry, or None for anything that is not a complete v1 envelope."""
    try:
        payload = json.loads(gzip.decompress(blob))
        if payload["v"] != FORMAT_VERSION:
            logger.warning("Ignoring cache entry with unknown format version %r", payload["v"])
            return None
        if payload["columns"] != list(COLUMNS):
            raise ValueError("cache entry has unexpected columns")
        if not isinstance(payload["symbol"], str) or not isinstance(payload["rows"], list):
            raise TypeError("cache entry has malformed fields")
        return CacheEntry(
            symbol=payload["symbol"],
            fetched_at=_aware(payload["fetched_at"]),
            fresh_until=_aware(payload["fresh_until"]),
            rows=[_row(values) for values in payload["rows"]],
        )
    except (OSError, EOFError, zlib.error, ValueError, KeyError, TypeError) as exc:
        logger.warning("Ignoring unreadable cache entry (%s)", type(exc).__name__)
        return None
