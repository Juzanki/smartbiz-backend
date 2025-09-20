# backend/utils/security.py
# -*- coding: utf-8 -*-
from __future__ import annotations

"""
Security helpers for SmartBiz.

- Password hashing/verification (passlib[bcrypt])
- JWT create/verify for access & refresh tokens (python-jose)
- Strict defaults (issuer, audience, leeway)
- Compatible whether your User model stores `password`, `password_hash`, or `hashed_password`.

ENV / Settings expected (backend.config.settings):
  SECRET_KEY (required in non-dev), ALGORITHM (default HS256),
  ACCESS_TOKEN_EXPIRE_MINUTES (default 60),
  REFRESH_TOKEN_EXPIRE_DAYS (default 14),
  TOKEN_AUDIENCE (default "smartbiz-api"),
  TOKEN_ISSUER (default "smartbiz"),
  JWT_LEEWAY_SECONDS (default 10),
  BCRYPT_ROUNDS (default 12),
  ENV ("development"/"production"/etc)
"""

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, Tuple, Union
import secrets
import warnings
import uuid
import logging

from jose import jwt, JWTError  # python-jose
from passlib.context import CryptContext  # passlib[bcrypt]

from backend.config import settings

logger = logging.getLogger("smartbiz.security")

# -------------------------------------------------
# Public exports from this module
# -------------------------------------------------
__all__ = [
    "pwd_context",
    "verify_password",
    "get_password_hash",
    "create_access_token",
    "create_refresh_token",
    "decode_token",
    "safe_decode_token",
    "get_token_subject",
    "is_access_token",
    "is_refresh_token",
    "token_expires_in_seconds",
    "issue_session_tokens",
    "create_reset_token",
    "validate_reset_token",
]

# =========================
# SECRET / JWT config
# =========================
def _dev_secret() -> str:
    key = secrets.token_urlsafe(64)
    warnings.warn(
        "[SECURITY] SECRET_KEY missing; using ephemeral DEV key. "
        "Set SECRET_KEY in .env for stable tokens.",
        RuntimeWarning,
        stacklevel=2,
    )
    return key

ENV = str(getattr(settings, "ENV", "development")).lower()

if getattr(settings, "SECRET_KEY", None):
    SECRET_KEY: str = settings.SECRET_KEY  # type: ignore[attr-defined]
else:
    if ENV in ("dev", "development", "local"):
        SECRET_KEY = _dev_secret()
    else:
        raise RuntimeError("🔐 SECRET_KEY is required in non-development environments.")

ALGORITHM: str = getattr(settings, "ALGORITHM", "HS256")
ACCESS_TOKEN_EXPIRE_MINUTES: int = int(getattr(settings, "ACCESS_TOKEN_EXPIRE_MINUTES", 60))
REFRESH_TOKEN_EXPIRE_DAYS: int = int(getattr(settings, "REFRESH_TOKEN_EXPIRE_DAYS", 14))
TOKEN_AUDIENCE: str = getattr(settings, "TOKEN_AUDIENCE", "smartbiz-api")
ISSUER: str = getattr(settings, "TOKEN_ISSUER", "smartbiz")
JWT_LEEWAY_SECONDS: int = int(getattr(settings, "JWT_LEEWAY_SECONDS", 10))
BCRYPT_ROUNDS: int = int(getattr(settings, "BCRYPT_ROUNDS", 12))  # cost factor

# =========================
# Time helpers
# =========================
def _now_utc() -> datetime:
    return datetime.now(timezone.utc)

def _exp_in(minutes: float) -> datetime:
    return _now_utc() + timedelta(minutes=minutes)

def _exp_days(days: float) -> datetime:
    return _now_utc() + timedelta(days=days)

# =========================
# Password hashing
# =========================
def _build_pwd_context() -> CryptContext:
    """
    Create passlib CryptContext with bcrypt, guarding against env/version pitfalls.
    Note: requirements.txt should pin:
      - passlib[bcrypt]==1.7.4
      - bcrypt==3.2.2
    """
    ctx = CryptContext(
        schemes=["bcrypt"],
        deprecated="auto",
        bcrypt__rounds=BCRYPT_ROUNDS,
        # normalize unicode input; avoid surprise mismatches
        # see: https://passlib.readthedocs.io/en/stable/lib/passlib.context.html
    )
    # Soft self-check (avoid crashing if bcrypt backend is odd)
    try:
        test_hash = ctx.hash("self-check")
        if not ctx.verify("self-check", test_hash):
            raise RuntimeError("bcrypt self-check failed")
    except Exception as e:
        # Don't crash the app; log a clear warning so ops can fix deps.
        logger.warning("Passlib/bcrypt backend check failed: %s", e)
    return ctx

# Internal context (private), with guard
_pwd_ctx = _build_pwd_context()
# Public alias expected by the rest of the codebase
pwd_context = _pwd_ctx

def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Return True if password matches the hash (never raises)."""
    if not (isinstance(plain_password, str) and isinstance(hashed_password, str)):
        return False
    try:
        return pwd_context.verify(plain_password, hashed_password)
    except Exception as e:
        # Common when bcrypt backend/version mismatches; treat as no-match.
        logger.debug("verify_password error: %s", e)
        return False

def get_password_hash(password: str) -> str:
    """Hash password using bcrypt (salted)."""
    if not isinstance(password, str) or not password:
        raise ValueError("Password missing")
    return pwd_context.hash(password)

# =========================
# JWT helpers
# =========================
def _base_claims(subject: Union[str, int], token_type: str, audience: Optional[str]) -> Dict[str, Any]:
    """Common claims for both access and refresh."""
    now = _now_utc()
    return {
        "sub": str(subject),
        "aud": audience or TOKEN_AUDIENCE,
        "iss": ISSUER,
        "jti": uuid.uuid4().hex,
        "type": token_type,             # "access" | "refresh"
        "iat": int(now.timestamp()),
        "nbf": int(now.timestamp()),    # valid from now
    }

def create_access_token(
    data: Dict[str, Any] | None = None,
    expires_delta: Optional[timedelta] = None,
    *,
    subject: Union[str, int, None] = None,
    audience: Optional[str] = None,
) -> str:
    """
    Create a short-lived access token (default uses ACCESS_TOKEN_EXPIRE_MINUTES).
    Either pass `subject=` or include `sub` inside `data`.
    """
    to_encode: Dict[str, Any] = dict(data or {})
    sub = subject or to_encode.get("sub")
    if not sub:
        raise ValueError("subject (sub) is required for access token")

    claims = _base_claims(sub, token_type="access", audience=audience)
    expire = _now_utc() + (expires_delta or timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES))
    to_encode.update(claims)
    to_encode["exp"] = expire

    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)

def create_refresh_token(
    *,
    subject: Union[str, int],
    audience: Optional[str] = None,
    expires_days: Optional[int] = None,
) -> str:
    """Create a long-lived refresh token (default REFRESH_TOKEN_EXPIRE_DAYS)."""
    claims = _base_claims(subject, token_type="refresh", audience=audience)
    expire = _exp_days(float(expires_days or REFRESH_TOKEN_EXPIRE_DAYS))
    payload = {**claims, "exp": expire}
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)

def decode_token(token: str, *, audience: Optional[str] = None) -> Dict[str, Any]:
    """
    Decode & validate a JWT and return its claims.
    Raises JWTError on invalid/expired token.
    """
    return jwt.decode(
        token,
        SECRET_KEY,
        algorithms=[ALGORITHM],
        audience=audience or TOKEN_AUDIENCE,
        issuer=ISSUER,
        options={"leeway": JWT_LEEWAY_SECONDS},
    )

def safe_decode_token(token: str, *, audience: Optional[str] = None) -> tuple[bool, Dict[str, Any] | None, str | None]:
    """
    Like decode_token but never raises.
    Returns (ok, claims, error_message)
    """
    try:
        claims = decode_token(token, audience=audience)
        return True, claims, None
    except JWTError as e:
        return False, None, str(e)

def get_token_subject(token_or_claims: Union[str, Dict[str, Any]]) -> Optional[str]:
    """Get `sub` from token string or claims dict."""
    claims = token_or_claims
    if isinstance(token_or_claims, str):
        ok, claims, _err = safe_decode_token(token_or_claims)
        if not ok or not isinstance(claims, dict):
            return None
    return str(claims.get("sub")) if claims and "sub" in claims else None

def is_access_token(claims: Dict[str, Any]) -> bool:
    return claims.get("type") == "access"

def is_refresh_token(claims: Dict[str, Any]) -> bool:
    return claims.get("type") == "refresh"

def token_expires_in_seconds(claims: Dict[str, Any]) -> int:
    """Return seconds until expiry (<=0 if expired or missing)."""
    exp = claims.get("exp")
    if not exp:
        return 0
    now = int(_now_utc().timestamp())  # python-jose uses seconds since epoch
    return int(exp) - now

def issue_session_tokens(
    subject: Union[str, int],
    *,
    audience: Optional[str] = None,
    access_minutes: Optional[int] = None,
    refresh_days: Optional[int] = None,
) -> Tuple[str, str, int, int]:
    """
    Convenience: create both access & refresh tokens.
    Returns (access_token, refresh_token, access_exp_seconds, refresh_exp_seconds).
    """
    access = create_access_token(
        data={"sub": str(subject)},
        expires_delta=timedelta(minutes=access_minutes or ACCESS_TOKEN_EXPIRE_MINUTES),
        audience=audience,
    )
    refresh = create_refresh_token(
        subject=str(subject),
        audience=audience,
        expires_days=refresh_days or REFRESH_TOKEN_EXPIRE_DAYS,
    )

    # decode once to compute TTLs (errors here mean config mismatch)
    a_ok, a_claims, _ = safe_decode_token(access, audience=audience)
    r_ok, r_claims, _ = safe_decode_token(refresh, audience=audience)

    a_ttl = token_expires_in_seconds(a_claims or {}) if a_ok else 0
    r_ttl = token_expires_in_seconds(r_claims or {}) if r_ok else 0

    return access, refresh, a_ttl, r_ttl

# =========================
# Optional: reset-token helpers (simple)
# =========================
def create_reset_token(email: str, *, minutes: int = 30) -> str:
    """
    Create a short-lived password-reset token bound to email.
    NOTE: Store a one-time nonce/jti if you want revocation.
    """
    return create_access_token(
        data={"sub": email, "scope": "password_reset"},
        expires_delta=timedelta(minutes=minutes),
        audience=TOKEN_AUDIENCE,
    )

def validate_reset_token(token: str) -> str:
    """Validate a reset token and return the email (sub) if valid."""
    claims = decode_token(token, audience=TOKEN_AUDIENCE)
    if claims.get("scope") != "password_reset":
        raise JWTError("invalid_scope")
    return str(claims["sub"])
