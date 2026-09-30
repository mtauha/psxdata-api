"""Shared FastAPI dependencies for the API layer."""
from fastapi import Request
from slowapi import Limiter
from slowapi.util import get_remote_address

from api.cache.factory import build_historical_service
from api.cache.historical import HistoricalService

limiter = Limiter(key_func=get_remote_address, default_limits=["60/minute"])


def get_rate_limiter() -> Limiter:
    return limiter


def get_cache(request: Request) -> HistoricalService:
    """Return the app's /historical cache; build a memory-only one if lifespan did not run."""
    service: HistoricalService | None = getattr(request.app.state, "historical_service", None)
    if service is None:
        service = build_historical_service({})
        request.app.state.historical_service = service
    return service
