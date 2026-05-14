"""Cryptography helpers.

Two distinct surfaces:
  - **Fernet** (symmetric, reversible) — for mailbox passwords + OAuth tokens
    that we need to read back later (e.g. for direct IMAP login). Key from
    ENCRYPTION_KEY env var.
  - **bcrypt** (one-way) — for admin passwords. Comparison via
    `bcrypt.checkpw`.

API-key tokens are hashed with **SHA-256** (not bcrypt — they're already
high-entropy random strings and we hash for storage uniqueness, not for
password-cracking resistance). See `hash_api_key`.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets

import bcrypt
from cryptography.fernet import Fernet, InvalidToken

from codecatch.config import get_settings


# ─── Fernet (symmetric) for reversible secrets ────────────────────────────
def _fernet() -> Fernet:
    return Fernet(get_settings().encryption_key.encode())


def encrypt(plaintext: str) -> str:
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str) -> str:
    try:
        return _fernet().decrypt(ciphertext.encode()).decode()
    except InvalidToken as e:
        raise ValueError("Cannot decrypt — wrong key or corrupted ciphertext") from e


# ─── bcrypt for admin passwords ───────────────────────────────────────────
def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode(), hashed.encode())
    except (ValueError, TypeError):
        return False


# ─── API key token generation + hashing ───────────────────────────────────
API_KEY_PREFIX = "ccr_live_"


def generate_api_key() -> tuple[str, str, str]:
    """Return (full_token, prefix_for_display, sha256_hash_for_storage).

    Token format: 'ccr_live_<32-char-urlsafe>'. The prefix shown in UI is the
    first 12 chars (so user can identify which key in their secret manager
    matches a row). The hash is what we store and look up by.
    """
    random_part = secrets.token_urlsafe(24)[:32]
    full = f"{API_KEY_PREFIX}{random_part}"
    prefix = full[:16]  # 'ccr_live_' + 7 more chars
    return full, prefix, hash_api_key(full)


def hash_api_key(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def compare_hashes(a: str, b: str) -> bool:
    """Constant-time hash comparison."""
    return hmac.compare_digest(a, b)
