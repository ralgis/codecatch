"""Pydantic request/response schemas for REST API."""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, EmailStr, Field


class MailboxUpsertRequest(BaseModel):
    address: EmailStr
    password: str = Field(..., min_length=1, description="Mailbox password — required even for OAuth providers (used for headless consent)")
    purpose: str | None = None
    notes: str | None = None
    proxy_url: str | None = Field(None, description="SOCKS5/HTTP proxy for OAuth headless consent (optional)")
    is_group: bool = False
    mode: str = Field(
        "auto",
        pattern="^(auto|direct_only|group_only|both)$",
        description="auto (default), direct_only, group_only, both",
    )
    forwarding_target: str | None = Field(None, description="Where this address forwards (documentation only)")


class MailboxResponse(BaseModel):
    address: str
    tenant_slug: str | None = None
    provider: str | None = None
    status: str
    is_group: bool
    purpose: str | None = None
    last_code_at: datetime | None = None
    created_at: datetime
    note: str | None = None
    consent_url: str | None = None


class CodeResponse(BaseModel):
    code_id: int
    target_address: str
    code: str
    platform: str | None
    sender: str | None
    subject: str | None
    body_excerpt: str | None
    source_mailbox: str | None
    received_at: datetime
    consumed_at: datetime | None = None


class CodesListResponse(BaseModel):
    count: int
    items: list[CodeResponse]


class CodeWaitRequest(BaseModel):
    address: EmailStr
    platform: str | None = None
    since: datetime | None = None
    timeout_sec: int = Field(90, ge=1, le=300)


class ConsumeRequest(BaseModel):
    note: str | None = None


class ConsumeResponse(BaseModel):
    code_id: int
    consumed_at: datetime
    consumed_by: str


class MeResponse(BaseModel):
    key_name: str
    tenant_id: int
    tenant_slug: str | None = None
    is_admin_scope: bool


class ErrorResponse(BaseModel):
    error: str
    message: str
