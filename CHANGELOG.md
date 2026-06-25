# Changelog

All notable changes to this project will be documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
This project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [0.1.0] — 2026-06-25

### Added

- Initial release — REST API service extracted from [mtauha/psxdata](https://github.com/mtauha/psxdata) with full git history preserved via `git filter-repo`.
- `GET /health` — liveness check returning `{"data": {"status": "ok"}, "meta": {"timestamp": ..., "cached": false}}`.
- `GET /stocks` — real-time trading panel data across all 15 board combinations.
- `GET /indices` — all 18 PSX index values.
- `GET /sectors` — 37 sector summaries.
- `GET /sectors/{name}/stocks` — symbol lookup filtered by sector.
- Standardised response envelope: `{"data": ..., "meta": {"count": N}}` for list endpoints; `{"error": {"status", "code", "message"}}` for errors.
- Six Pydantic v2 response models in `api/schemas.py`: `MetaSingle`, `MetaList`, `ErrorDetail`, `ErrorEnvelope`, `HealthData`, `HealthResponse`.
- Slowapi rate-limiting middleware (60 req/min per IP by default).
- Multi-stage `Dockerfile`: `builder` stage installs deps into a venv; `runtime` stage copies only the venv and `api/` source. Runs as non-root `psxuser`. Supports `PORT` env var. Includes `HEALTHCHECK`.
- `.dockerignore` stripping all non-essential paths from the build context.
- CI workflow: `ruff` + `mypy` lint, `pytest` test matrix (Python 3.11/3.12), docs smoke-test (uvicorn boot + curl `/health`, `/docs`, `/redoc`).
- Docker Hub publish workflow: builds and pushes `mtauha/psxdata-api:latest` and `:<version>` on `v*` tag push.
- FastAPI Cloud auto-deploy configured via `[tool.fastapi] entrypoint = "api.main:app"` in `pyproject.toml`.

### Known Issues

- `GET /sectors/{name}/stocks` returns an empty list — `psxdata.symbols()` is not yet part of the public `psxdata` API (tracked in [#1](https://github.com/mtauha/psxdata-api/issues/1)).

---

[0.1.0]: https://github.com/mtauha/psxdata-api/releases/tag/v0.1.0
