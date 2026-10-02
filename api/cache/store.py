"""Two-tier byte store: an in-process LRU (L1) in front of an optional Redis-compatible
server (L2).

L1 serves almost every hit without a network round-trip. L2 keeps entries across restarts
and instances. L2 fails open: an outage costs one timeout per backoff window, never an error.
"""
from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict
from collections.abc import Callable

import redis

logger = logging.getLogger(__name__)

L1_MAX_BYTES = 64 * 1024 * 1024
L2_TTL_SECONDS = 7 * 24 * 60 * 60
L2_BACKOFF_SECONDS = 30.0


class MemoryLRU:
    """Thread-safe LRU of bytes values, bounded by their total size."""

    def __init__(self, max_bytes: int = L1_MAX_BYTES) -> None:
        self._max_bytes = max_bytes
        self._data: OrderedDict[str, bytes] = OrderedDict()
        self._size = 0
        self._lock = threading.Lock()

    @property
    def size_bytes(self) -> int:
        return self._size

    def get(self, key: str) -> bytes | None:
        with self._lock:
            value = self._data.get(key)
            if value is not None:
                self._data.move_to_end(key)
            return value

    def set(self, key: str, value: bytes) -> None:
        if len(value) > self._max_bytes:
            return
        with self._lock:
            old = self._data.pop(key, None)
            if old is not None:
                self._size -= len(old)
            self._data[key] = value
            self._size += len(value)
            while self._size > self._max_bytes:
                _, evicted = self._data.popitem(last=False)
                self._size -= len(evicted)


class RedisStore:
    """Fail-open wrapper around a Redis-compatible client (``None`` means not configured)."""

    def __init__(
        self,
        client: redis.Redis | None,
        *,
        ttl_seconds: int = L2_TTL_SECONDS,
        backoff_seconds: float = L2_BACKOFF_SECONDS,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client = client
        self._ttl = ttl_seconds
        self._backoff = backoff_seconds
        self._monotonic = monotonic
        self._disabled_until = 0.0
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return self._client is not None

    def get(self, key: str) -> bytes | None:
        client = self._usable_client()
        if client is None:
            return None
        try:
            value = client.get(key)
        except (redis.RedisError, OSError) as exc:
            self._fail("read", exc)
            return None
        return value if isinstance(value, bytes) else None

    def set(self, key: str, value: bytes) -> None:
        client = self._usable_client()
        if client is None:
            return
        try:
            client.set(key, value, ex=self._ttl)
        except (redis.RedisError, OSError) as exc:
            self._fail("write", exc)

    def close(self) -> None:
        if self._client is None:
            return
        try:
            self._client.close()
        except (redis.RedisError, OSError):
            pass

    def _usable_client(self) -> redis.Redis | None:
        if self._client is None or self._monotonic() < self._disabled_until:
            return None
        return self._client

    def _fail(self, operation: str, exc: Exception) -> None:
        with self._lock:
            now = self._monotonic()
            if now >= self._disabled_until:
                logger.warning(
                    "Cache backend %s failed (%s); bypassing it for %.0fs",
                    operation, type(exc).__name__, self._backoff,
                )
            self._disabled_until = now + self._backoff


class TieredStore:
    """L1 first; on an L1 miss, L2 (filling L1). Writes go to both."""

    def __init__(self, l1: MemoryLRU, l2: RedisStore) -> None:
        self.l1 = l1
        self.l2 = l2

    def get(self, key: str) -> bytes | None:
        return self.get_with_tier(key)[0]

    def get_with_tier(self, key: str) -> tuple[bytes | None, str]:
        """Like ``get``, plus which tier answered: "memory", "redis" or "none"."""
        value = self.l1.get(key)
        if value is not None:
            return value, "memory"
        value = self.l2.get(key)
        if value is not None:
            self.l1.set(key, value)
            return value, "redis"
        return None, "none"

    def put(self, key: str, value: bytes) -> None:
        self.l1.set(key, value)
        self.l2.set(key, value)

    def close(self) -> None:
        self.l2.close()
