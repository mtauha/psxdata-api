# Changelog

All notable changes to this project will be documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
This project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [0.1.2] — 2026-07-03

### Fixed

- `GET /sectors/{name}/stocks` no longer returns a bare `500 Internal Server Error`. It called `psxdata.symbols()`, which existed in the SDK's git source but had never been published to PyPI. Bumped the `psxdata` pin to `0.1.0a4`, which ships it (fixes [#1](https://github.com/mtauha/psxdata-api/issues/1)).

### Changed

- `psxdata` dependency pin bumped from `0.1.0a3` to `0.1.0a4`.

---

## [0.1.1] — 2026-07-03

### Fixed

- `GET /stocks?index=<invalid>` no longer returns a bare `500 Internal Server Error`. `PSXParseError` (raised when PSX rejects an unknown index name) is now caught and returns `400 bad_request` with a descriptive message (fixes [#2](https://github.com/mtauha/psxdata-api/issues/2)).
- Pydantic validation failures when building response models from upstream PSX data (e.g. a missing/renamed field) now return `502 upstream_data_error` instead of an opaque `500`, and are logged server-side via `logger.exception` for diagnosis.
- The generic unhandled-exception handler now logs the exception instead of silently discarding it.

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

[0.1.2]: https://github.com/mtauha/psxdata-api/releases/tag/v0.1.2
[0.1.1]: https://github.com/mtauha/psxdata-api/releases/tag/v0.1.1
[0.1.0]: https://github.com/mtauha/psxdata-api/releases/tag/v0.1.0
