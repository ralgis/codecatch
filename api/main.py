"""codecatch API entrypoint — minimal scaffold.

The real surface (REST endpoints + admin UI) will be wired in subsequent
commits. For now this is the smallest runnable FastAPI app that:
  - boots
  - exposes GET /healthz returning DB connectivity
  - exposes GET / returning version + a hint to /admin (not yet implemented)
"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import Any

import asyncpg
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from api import __version__


DATABASE_URL = os.environ.get("DATABASE_URL", "")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Acquire a Postgres pool on startup, close on shutdown."""
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is required")
    app.state.db_pool = await asyncpg.create_pool(
        DATABASE_URL, min_size=1, max_size=10, command_timeout=10
    )
    try:
        yield
    finally:
        await app.state.db_pool.close()


app = FastAPI(
    title="codecatch",
    version=__version__,
    description="Email verification code router. See /admin for UI.",
    lifespan=lifespan,
)


@app.get("/")
async def root() -> dict[str, Any]:
    return {
        "service": "codecatch",
        "version": __version__,
        "admin_ui": "/admin (not yet implemented)",
        "api_docs": "/docs",
    }


@app.get("/healthz")
async def healthz(request: Request) -> JSONResponse:
    """Liveness + DB connectivity probe."""
    pool: asyncpg.Pool = request.app.state.db_pool
    db_ok = False
    db_error: str | None = None
    try:
        async with pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
        db_ok = True
    except Exception as e:  # noqa: BLE001
        db_error = str(e)[:200]

    status_code = 200 if db_ok else 503
    return JSONResponse(
        status_code=status_code,
        content={
            "status": "ok" if db_ok else "degraded",
            "db": "ok" if db_ok else "fail",
            "db_error": db_error,
            "version": __version__,
        },
    )
