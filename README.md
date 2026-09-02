# psxdata-api — REST API for Pakistan Stock Exchange (PSX) Data

[![CI](https://github.com/mtauha/psxdata-api/actions/workflows/ci.yml/badge.svg)](https://github.com/mtauha/psxdata-api/actions/workflows/ci.yml)
[![Documentation](https://img.shields.io/badge/docs-mintlify-blue)](https://psxdata.mintlify.app/rest-api)
[![API](https://img.shields.io/badge/api-live-brightgreen)](https://psxdata-api.fastapicloud.dev)
[![Docker Hub](https://img.shields.io/docker/v/mtauha/psxdata-api?label=Docker+Hub)](https://hub.docker.com/r/mtauha/psxdata-api)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE.md)

**psxdata-api** is a FastAPI REST service that exposes Pakistan Stock Exchange data over HTTP. It wraps the [psxdata](https://pypi.org/project/psxdata/) Python library and ships its own Docker image, CI/CD pipeline, and auto-deploy config.

**Base URL:** `https://psxdata-api.fastapicloud.dev`  
**Documentation:** [https://psxdata.mintlify.app/rest-api](https://psxdata.mintlify.app/rest-api)

---

## Quick Start

```bash
# Run with Docker
docker run -p 8000:8000 mtauha/psxdata-api

# Try it
curl http://localhost:8000/health
curl http://localhost:8000/stocks
curl "http://localhost:8000/stocks/ENGRO/historical?start=2024-01-01&end=2024-12-31"
curl http://localhost:8000/screener
```

Interactive docs available at `http://localhost:8000/docs` (Swagger UI) and `/redoc`.

---

## Endpoints

### Health

| Method | Path | Description |
| ------ | ---- | ----------- |
| `GET` | `/health` | API liveness check |

### Stocks

| Method | Path | Query params | Description |
| ------ | ---- | ------------ | ----------- |
| `GET` | `/stocks` | `index` (optional) | All listed tickers, optionally filtered by index name |
| `GET` | `/stocks/{symbol}/historical` | `start`, `end` (ISO dates, optional) | OHLCV history for a ticker |
| `GET` | `/stocks/{symbol}/quote` | — | Live quote for a ticker |
| `GET` | `/stocks/{symbol}/fundamentals` | — | Financial report links for a ticker |

### Indices

| Method | Path | Description |
| ------ | ---- | ----------- |
| `GET` | `/indices` | All 18 PSX index names |
| `GET` | `/indices/{name}` | Constituents of a named index (e.g. `KSE100`) |

### Sectors

| Method | Path | Description |
| ------ | ---- | ----------- |
| `GET` | `/sectors` | All 37 sector summaries |
| `GET` | `/sectors/{name}/stocks` | Tickers in a named sector |

### Screener

| Method | Path | Description |
| ------ | ---- | ----------- |
| `GET` | `/screener` | Full, unfiltered PSX screener table (~729 symbols, all columns) |

---

### Market Instruments

| Method | Path | Description |
| ------ | ---- | ----------- |
| `GET` | `/debt-market` | Debt market instruments (TFCs, Sukuks, etc.) |
| `GET` | `/eligible-scrips` | Margin trading eligible stocks |

---

## Response Envelope

Every response wraps its payload in a consistent envelope.

**Single-item response**
```json
{
  "data": { "status": "ok" },
  "meta": { "timestamp": "2024-01-15T10:30:00+00:00", "cached": false }
}
```

**List response**
```json
{
  "data": [{"symbol": "ENGRO", ...}, ...],
  "meta": { "timestamp": "2024-01-15T10:30:00+00:00", "cached": false, "count": 42 }
}
```

**Error response**
```json
{
  "error": { "status": 404, "code": "not_found", "message": "ENGRO not found" }
}
```

### Error codes

| HTTP status | `code` | Meaning |
| ----------- | ------ | ------- |
| 400, 422 | `bad_request` | Invalid input or query parameters |
| 404 | `not_found` | Symbol or index does not exist |
| 429 | `rate_limited` | Exceeded 60 requests/minute per IP |
| 502 | `upstream_data_error` | Upstream PSX data failed validation |
| 503 | `psx_unavailable` | PSX website unreachable |
| 500 | `internal_error` | Unexpected server error |

---

## Rate Limiting

60 requests per minute per IP address. Exceeding the limit returns `429 rate_limited`.

---

## Docker

```bash
# Pull and run
docker run -p 8000:8000 mtauha/psxdata-api

# Custom port
docker run -p 9000:9000 -e PORT=9000 mtauha/psxdata-api

# Build from source
docker build -t psxdata-api .
docker run -p 8000:8000 psxdata-api
```

The image runs as a non-root user (`psxuser`) and includes a `HEALTHCHECK` against `/health`.

---

## Local Development

```bash
cd api
pip install -e ".[dev]"
uvicorn api.main:app --reload
```

Run tests:
```bash
pytest
```

Lint and type-check:
```bash
ruff check .
mypy api/
```

Requires Python 3.11+.

---

## Related

- **[psxdata](https://github.com/mtauha/psxdata)** — Python library this service wraps
- **[psxdata on PyPI](https://pypi.org/project/psxdata/)** — installable package
- **[mtauha/psxdata-api on Docker Hub](https://hub.docker.com/r/mtauha/psxdata-api)** — Docker image
