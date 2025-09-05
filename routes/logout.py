# ================= backend/routes/logout.py =================
"""
Logout endpoint for SmartBiz Assistance.

Features:
- Blacklists JWTs (by SHA-256 hash) until their `exp` time elapses.
- Idempotent: logging out an already-blacklisted token still returns success.
- In-memory store with auto-purge on access (thread-safe).
- Works in sync with AUTH_TOKEN_URL from environment (default `/auth/login`).
- Includes dependency to reject blacklisted tokens in protected routes.
"""

from __future__ import annotations

import time
import hashlib
from threading import RLock
from datetime import datetime, timezone
from typing import Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jose import jwt, JWTError

from backend.utils.security import SECRET_KEY, ALGORITHM
import os

# -----------------------------------------------------------------------------
# Config
# -----------------------------------------------------------------------------
TOKEN_URL = os.getenv("AUTH_TOKEN_URL", "/auth/login")  # Keep in sync with auth/__init__.py

router = APIRouter(prefix="/logout", tags=["Auth"])

oauth2_scheme = OAuth2PasswordBearer(tokenUrl=TOKEN_URL)

# -----------------------------------------------------------------------------
# In-memory blacklist: token_hash -> exp_unix (int)
# -----------------------------------------------------------------------------
_BLACKLIST: Dict[str, int] = {}
_LOCK = RLock()

def _token_hash(raw: str) -> str:
    """Return SHA256 hash of raw JWT for memory-safe storage."""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()

def _purge_expired(now_ts: Optional[int] = None) -> None:
    """Remove expired token hashes from the blacklist."""
    now_ts = now_ts or int(time.time())
    with _LOCK:
        expired_keys = [k for k, exp in _BLACKLIST.items() if exp <= now_ts]
        for k in expired_keys:
            _BLACKLIST.pop(k, None)

def _blacklist_add(token: str, exp_ts: Optional[int]) -> None:
    """Add token hash to blacklist until exp_ts (or default 24h if missing)."""
    ttl_default = int(time.time()) + 24 * 3600
    exp = int(exp_ts) if exp_ts is not None else ttl_default
    with _LOCK:
        _BLACKLIST[_token_hash(token)] = exp

def _is_blacklisted(token: str) -> bool:
    """Check if token hash exists in blacklist."""
    _purge_expired()
    with _LOCK:
        return _token_hash(token) in _BLACKLIST

# -----------------------------------------------------------------------------
# Endpoint
# -----------------------------------------------------------------------------
@router.post("/", summary="Logout and invalidate current token")
def logout_user(token: str = Depends(oauth2_scheme)) -> dict:
    """
    Invalidate current JWT.

    Process:
    - Verify signature (ignores `exp` to allow logout even if just expired).
    - Extract `exp` (if available) to set blacklist duration.
    - Add token hash to blacklist (idempotent).
    - Return success payload with expiration info.
    """
    try:
        payload = jwt.decode(
            token,
            SECRET_KEY,
            algorithms=[ALGORITHM],
            options={"verify_exp": False},  # allow expired token for logout
        )
    except JWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token",
        ) from exc

    exp_ts = payload.get("exp")
    exp_dt = None
    if exp_ts is not None:
        try:
            exp_dt = datetime.fromtimestamp(int(exp_ts), tz=timezone.utc)
        except Exception:
            exp_dt = None

    _blacklist_add(token, int(exp_ts) if exp_ts is not None else None)

    return {
        "message": "✅ Logout successful",
        "blacklisted_until": exp_dt.isoformat() if exp_dt else "temporary",
        "expires_in": (int(exp_ts) - int(time.time())) if exp_ts else 24 * 3600,
    }

# -----------------------------------------------------------------------------
# Dependency
# -----------------------------------------------------------------------------
def verify_not_blacklisted(token: str = Depends(oauth2_scheme)) -> str:
    """
    Dependency to reject requests presenting a blacklisted token.
    Use this *together* with your get_current_user logic.
    """
    if _is_blacklisted(token):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token has been logged out",
        )
    return token
