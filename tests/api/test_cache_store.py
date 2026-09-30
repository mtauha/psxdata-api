"""Tests for the L1 memory LRU, the fail-open Redis store, and the tiered store."""
import logging

import fakeredis
import redis

from api.cache.store import L2_TTL_SECONDS, MemoryLRU, RedisStore, TieredStore


class FakeMonotonic:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


class DownRedis:
    """Stands in for an unreachable server: every call raises."""

    def __init__(self) -> None:
        self.calls = 0

    def get(self, key):
        self.calls += 1
        raise redis.ConnectionError("connection refused")

    def set(self, key, value, ex=None):
        self.calls += 1
        raise redis.ConnectionError("connection refused")

    def close(self) -> None:
        pass


class CountingRedis(fakeredis.FakeRedis):
    gets = 0

    def get(self, name):
        type(self).gets += 1
        return super().get(name)


# ---- MemoryLRU ----------------------------------------------------------------------------

def test_lru_get_set_and_miss() -> None:
    lru = MemoryLRU(max_bytes=100)
    lru.set("a", b"1234")
    assert lru.get("a") == b"1234"
    assert lru.get("missing") is None


def test_lru_evicts_least_recently_used_by_bytes() -> None:
    lru = MemoryLRU(max_bytes=10)
    lru.set("a", b"aaaa")
    lru.set("b", b"bbbb")
    lru.get("a")                 # a is now most recently used
    lru.set("c", b"cccc")        # 12 bytes > 10: evict b
    assert lru.get("b") is None
    assert lru.get("a") == b"aaaa"
    assert lru.get("c") == b"cccc"
    assert lru.size_bytes == 8


def test_lru_replacing_a_key_updates_size() -> None:
    lru = MemoryLRU(max_bytes=100)
    lru.set("a", b"1234")
    lru.set("a", b"12345678")
    assert lru.size_bytes == 8


def test_lru_skips_values_larger_than_capacity() -> None:
    lru = MemoryLRU(max_bytes=4)
    lru.set("big", b"12345")
    assert lru.get("big") is None
    assert lru.size_bytes == 0


# ---- RedisStore ---------------------------------------------------------------------------

def test_redis_store_without_client_is_a_noop() -> None:
    store = RedisStore(None)
    store.set("k", b"v")
    assert store.get("k") is None
    assert store.enabled is False
    store.close()


def test_redis_store_round_trip_with_expiry() -> None:
    client = fakeredis.FakeRedis()
    store = RedisStore(client)
    store.set("k", b"v")
    assert store.get("k") == b"v"
    assert 0 < client.ttl("k") <= L2_TTL_SECONDS
    assert store.enabled is True


def test_redis_store_failure_is_swallowed_logged_once_and_backs_off(caplog) -> None:
    caplog.set_level(logging.WARNING)
    clock = FakeMonotonic()
    client = DownRedis()
    store = RedisStore(client, monotonic=clock)

    assert store.get("k") is None           # fails, starts 30 s backoff
    store.set("k", b"v")                    # within backoff: client not touched
    assert store.get("k") is None
    assert client.calls == 1
    assert caplog.text.count("Cache backend") == 1

    clock.t += 31                           # backoff over: tries again
    assert store.get("k") is None
    assert client.calls == 2


# ---- TieredStore --------------------------------------------------------------------------

def test_tiered_l2_hit_fills_l1() -> None:
    client = fakeredis.FakeRedis()
    client.set("k", b"v")
    tiered = TieredStore(MemoryLRU(), RedisStore(client))
    assert tiered.get("k") == b"v"
    assert tiered.l1.get("k") == b"v"


def test_tiered_l1_hit_skips_l2() -> None:
    CountingRedis.gets = 0
    tiered = TieredStore(MemoryLRU(), RedisStore(CountingRedis()))
    tiered.put("k", b"v")
    assert tiered.get("k") == b"v"
    assert CountingRedis.gets == 0


def test_tiered_put_writes_both_tiers() -> None:
    client = fakeredis.FakeRedis()
    tiered = TieredStore(MemoryLRU(), RedisStore(client))
    tiered.put("k", b"v")
    assert tiered.l1.get("k") == b"v"
    assert client.get("k") == b"v"


def test_tiered_works_when_l2_is_down() -> None:
    tiered = TieredStore(MemoryLRU(), RedisStore(DownRedis()))
    tiered.put("k", b"v")
    assert tiered.get("k") == b"v"
