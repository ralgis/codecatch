"""Centralised env-driven settings (pydantic-settings).

Read once at process start; passed around as a dependency. Keeping the
surface small — anything that should be runtime-configurable lives in the
`settings` table (db-backed), not here.
"""
from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # ── Database ───────────────────────────────────────────────────────────
    database_url: str = Field(..., alias="DATABASE_URL")

    # ── Cryptography ───────────────────────────────────────────────────────
    secret_key: str = Field(..., alias="SECRET_KEY", min_length=32)
    encryption_key: str = Field(..., alias="ENCRYPTION_KEY", min_length=44)

    # ── Bootstrap (consumed once on first run) ─────────────────────────────
    bootstrap_admin_user: str = Field("admin", alias="BOOTSTRAP_ADMIN_USER")
    bootstrap_admin_password: str = Field(..., alias="BOOTSTRAP_ADMIN_PASSWORD", min_length=8)

    # ── Networking ─────────────────────────────────────────────────────────
    base_url: str = Field("http://localhost:8080", alias="BASE_URL")
    host: str = Field("0.0.0.0", alias="HOST")
    port: int = Field(8080, alias="PORT")

    # ── OAuth ──────────────────────────────────────────────────────────────
    oauth_default_strategy: str = Field("thunderbird", alias="OAUTH_DEFAULT_STRATEGY")

    # ── Ops ────────────────────────────────────────────────────────────────
    log_level: str = Field("INFO", alias="LOG_LEVEL")
    sentry_dsn: str | None = Field(None, alias="SENTRY_DSN")
    code_retention_days: int = Field(30, alias="CODE_RETENTION_DAYS")

    # ── Workers ────────────────────────────────────────────────────────────
    max_imap_workers: int = Field(50, alias="MAX_IMAP_WORKERS")
    playwright_headless: bool = Field(True, alias="PLAYWRIGHT_HEADLESS")
    playwright_timeout_sec: int = Field(60, alias="PLAYWRIGHT_TIMEOUT_SEC")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
