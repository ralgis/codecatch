"""codecatch API entrypoint — FastAPI app + admin UI."""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from api import __version__
from api.routes import admin as admin_routes
from api.routes import api_v1, login as login_routes
from api.routes import oauth as oauth_routes
from codecatch.bootstrap import run_bootstrap
from codecatch.config import get_settings
from codecatch.db import create_pool
from codecatch.logging_setup import configure_logging, get_logger

ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = ROOT / "static"
TEMPLATES_DIR = ROOT / "templates"


@asynccontextmanager
async def lifespan(app: FastAPI):
    s = get_settings()
    configure_logging(s.log_level)
    log = get_logger("api.lifespan")
    log.info("startup.begin", version=__version__, base_url=s.base_url)

    pool = await create_pool(min_size=2, max_size=10)
    app.state.db_pool = pool
    log.info("startup.db_pool_ready")

    await run_bootstrap(pool)
    log.info("startup.bootstrap_done")

    try:
        yield
    finally:
        await pool.close()
        log.info("shutdown.complete")


app = FastAPI(
    title="codecatch",
    version=__version__,
    description="Email verification code router. See /admin for UI, /docs for API schema.",
    lifespan=lifespan,
)


# ─── Static assets ─────────────────────────────────────────────────────────
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# ─── Routes ────────────────────────────────────────────────────────────────
app.include_router(login_routes.router)
app.include_router(oauth_routes.router)
app.include_router(admin_routes.router)
app.include_router(api_v1.router)


@app.get("/")
async def root() -> RedirectResponse:
    return RedirectResponse(url="/admin", status_code=302)


@app.get("/healthz")
async def healthz(request: Request) -> JSONResponse:
    pool = request.app.state.db_pool
    db_ok = False
    db_error: str | None = None
    try:
        async with pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
        db_ok = True
    except Exception as e:  # noqa: BLE001
        db_error = str(e)[:200]
    return JSONResponse(
        status_code=200 if db_ok else 503,
        content={
            "status": "ok" if db_ok else "degraded",
            "db": "ok" if db_ok else "fail",
            "db_error": db_error,
            "version": __version__,
        },
    )
