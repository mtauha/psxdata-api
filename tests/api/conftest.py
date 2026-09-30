"""Shared fixtures for API tests."""
import pytest

from api.cache.historical import HistoricalService
from api.cache.store import MemoryLRU, RedisStore, TieredStore
from api.main import app
from api.proxy import ProxyPassthrough


@pytest.fixture(autouse=True)
def proxy_passthrough_disabled() -> ProxyPassthrough:
    """Start every test with proxy passthrough off and an empty client pool."""
    passthrough = ProxyPassthrough(False)
    app.state.proxy_passthrough = passthrough
    return passthrough


@pytest.fixture(autouse=True)
def fresh_historical_service() -> HistoricalService:
    """Give every test an empty, Redis-less /historical cache so cached data never leaks."""
    service = HistoricalService(TieredStore(MemoryLRU(), RedisStore(None)))
    app.state.historical_service = service
    return service
