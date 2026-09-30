"""Stocks router — /stocks and /stocks/{symbol}/* endpoints."""
from __future__ import annotations

import re
from datetime import date, datetime, timezone
from typing import Any

import pandas as pd
import psxdata
from fastapi import APIRouter, Depends, HTTPException, Request, Response

from api.cache.historical import CacheStatus
from api.dependencies import get_cache, limiter
from api.proxy import PsxSource, psx_source
from api.schemas import (
    ErrorEnvelope,
    FundamentalsResponse,
    FundamentalsRow,
    HistoricalResponse,
    MetaList,
    MetaSingle,
    OHLCVRow,
    QuoteData,
    QuoteResponse,
    StringListResponse,
)

router = APIRouter(tags=["stocks"])


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _df_to_records(df: pd.DataFrame) -> list[dict]:
    """Convert DataFrame to JSON-safe records: Timestamps to ISO strings, NaN to None."""
    df = df.copy()
    for col in df.columns:
        if pd.api.types.is_datetime64_any_dtype(df[col]):
            df[col] = df[col].apply(lambda x: x.strftime("%Y-%m-%d") if pd.notna(x) else None)
    df = df.where(pd.notna(df), other=None)
    return df.to_dict("records")


_HISTORICAL_RESPONSES: dict[int | str, dict[str, Any]] = {
    200: {
        "headers": {
            "X-Cache": {
                "description": "HIT (fresh cache), MISS (fetched from PSX), STALE "
                "(PSX refused or was unreachable; last cached copy served), or BYPASS "
                "(fetched through the caller's X-PSX-Proxy; cache not used)",
                "schema": {"type": "string", "enum": ["HIT", "MISS", "STALE", "BYPASS"]},
            },
            "Age": {
                "description": "Seconds since the data was fetched from PSX (HIT and STALE only)",
                "schema": {"type": "integer"},
            },
        },
    },
    503: {
        "model": ErrorEnvelope,
        "description": "PSX is unavailable or rate-limiting, and no cached copy exists",
        "headers": {
            "Retry-After": {
                "description": "Seconds to wait before retrying (sent when PSX is rate-limiting)",
                "schema": {"type": "integer"},
            },
        },
    },
}


_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _parse_date(name: str, value: str | None) -> date | None:
    if value is None:
        return None
    # fromisoformat also accepts 20240102 and 2024-W01-2; the documented contract is YYYY-MM-DD
    if _ISO_DATE.fullmatch(value):
        try:
            return date.fromisoformat(value)
        except ValueError:
            pass
    raise HTTPException(status_code=422, detail=f"{name} must be an ISO date (YYYY-MM-DD)")


def _fetch_full_history(symbol: str) -> list[dict]:
    return _df_to_records(psxdata.stocks(symbol, cache=False))


@router.get("/stocks", response_model=StringListResponse)
@limiter.limit("60/minute")
def list_stocks(
    request: Request, index: str | None = None, psx: PsxSource = Depends(psx_source)
) -> StringListResponse:
    tickers = psx.fetch("tickers", index=index)
    return StringListResponse(
        data=tickers,
        meta=MetaList(timestamp=_now_iso(), cached=False, count=len(tickers)),
    )


@router.get(
    "/stocks/{symbol}/historical",
    response_model=HistoricalResponse,
    responses=_HISTORICAL_RESPONSES,
)
@limiter.limit("60/minute")
def get_historical(
    request: Request,
    response: Response,
    symbol: str,
    start: str | None = None,
    end: str | None = None,
    psx: PsxSource = Depends(psx_source),
) -> HistoricalResponse:
    start_date = _parse_date("start", start)
    end_date = _parse_date("end", end)
    if start_date and end_date and start_date > end_date:
        raise HTTPException(status_code=422, detail="start must not be after end")

    if psx.proxied:
        # A caller's proxy must never feed or read the shared cache
        all_rows = _df_to_records(psx.fetch("stocks", symbol.upper()))
        cached = False
        response.headers["X-Cache"] = "BYPASS"
    else:
        result = get_cache(request).get(symbol.upper(), _fetch_full_history)
        all_rows = result.rows
        cached = result.status is not CacheStatus.MISS
        response.headers["X-Cache"] = result.status.value
        if cached:
            age = (datetime.now(timezone.utc) - result.fetched_at).total_seconds()
            response.headers["Age"] = str(max(0, int(age)))

    lo = start_date.isoformat() if start_date else None
    hi = end_date.isoformat() if end_date else None
    rows = [
        OHLCVRow.model_validate(r)
        for r in all_rows
        if (lo is None or r["date"] >= lo) and (hi is None or r["date"] <= hi)
    ]
    return HistoricalResponse(
        data=rows,
        meta=MetaList(timestamp=_now_iso(), cached=cached, count=len(rows)),
    )


@router.get("/stocks/{symbol}/quote", response_model=QuoteResponse)
@limiter.limit("60/minute")
def get_quote(request: Request, symbol: str, psx: PsxSource = Depends(psx_source)) -> QuoteResponse:
    df = psx.fetch("quote", symbol.upper())
    if df.empty:
        raise HTTPException(status_code=404, detail=f"{symbol.upper()} not found")
    row = _df_to_records(df)[0]
    data = QuoteData.model_validate(row)
    return QuoteResponse(
        data=data,
        meta=MetaSingle(timestamp=_now_iso(), cached=False),
    )


@router.get("/stocks/{symbol}/fundamentals", response_model=FundamentalsResponse)
@limiter.limit("60/minute")
def get_fundamentals(
    request: Request, symbol: str, psx: PsxSource = Depends(psx_source)
) -> FundamentalsResponse:
    df = psx.fetch("fundamentals", symbol=symbol.upper())
    rows: list[FundamentalsRow] = []
    if not df.empty:
        rows = [FundamentalsRow.model_validate(r) for r in _df_to_records(df)]
    return FundamentalsResponse(
        data=rows,
        meta=MetaList(timestamp=_now_iso(), cached=False, count=len(rows)),
    )
