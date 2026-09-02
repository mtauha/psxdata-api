"""Root-level shim for FastAPI Cloud's app auto-discovery.

FastAPI Cloud's `fastapi run` falls back to scanning for main.py/app.py/api.py
(or app/main.py etc.) at the working directory if it can't resolve the
[tool.fastapi] entrypoint from pyproject.toml in its build environment.
`api/main.py` isn't one of those scanned paths, so this shim re-exports the
real app to guarantee discovery regardless of that resolution.
"""
from api.main import app  # noqa: F401
