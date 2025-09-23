# ============================ backend/routes/logout.py ============================
from __future__ import annotations

import os
import time
import hashlib
from datetime import datetime, timezone
from threading import RLock
from typing import Dict, Optional, Any

import jwt  # PyJWT
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.security import OAuth2PasswordBearer

# -----------------------------------------------------------------------------
# Configuration (kept aligned with auth_routes.py and dependencies/authz.py)
# -----------------------------------------------------------------------------
AUTH_TOKEN_URL = os.getenv("AUTH_TOKEN_URL", "/auth/login")

# JWT verification key/alg (no random fallback here—must match token issuer)
SECRET_KEY: Optional[str] = os.getenv("SECRET_KEY") or os.getenv("JWT_SECRET")
if not SECRET_KEY:
    raise RuntimeError("SECRET_KEY/JWT_SECRET is required for logout token verification")

JWT_ALG = os.getenv("JWT_ALG", os.getenv("JWT_ALGORITHM", "HS256"))

# Cookie settings (to actively clear the auth cookie on logout)
USE_COOKIE_AUTH = (os.getenv("USE_COOKIE_AUTH", "true").strip().lower() in {"1", "true", "yes", "on", "y"})
COOKIE_NAME = os.getenv("AUTH_COOKIE_NAME", "sb_access")
COOKIE_PATH = os.getenv("AUTH_COOKIE_PATH", "/")
COOKIE_DOMAIN = (os.getenv("AUTH_COOKIE_DOMAIN") or "").strip() or None
COOKIE_SECURE = (os.getenv("AUTH_COOKIE_SECURE", "true").strip().lower() in {"1", "true", "yes", "on", "y"})
COOKIE_SAMESITE = os.getenv("AUTH_COOKIE_SAMESITE", "none")  # 'lax' | 'strict' | 'none'

# Query param support (optional, off by default unless you pass a name)
QUERY_TOKEN_NAME = os.getenv("AUTH_QUERY_NAME", "access_token")

# Blacklist memory guard
BLACKLIST_MAX = int(os.getenv("JWT_BLACKLIST_MAX", "100000"))  # soft cap

# Diagnostics (disabled by default)
DIAG_AUTH_ENABLED = (os.getenv("DIAG_AUTH_ENABLED", "0").strip().lower() in {"1", "true", "yes", "on"})

# FastAPI router & OAuth helper
router = APIRouter(prefix="/auth", tags=["auth"])
oauth2_scheme = OAuth2PasswordBearer(tokenUrl=AUTH_TOKEN_URL, auto_error=False)

# -----------------------------------------------------------------------------
# In-memory blacklist: token_hash -> exp_epoch (int)
# -----------------------------------------------------------------------------
_BLACKLIST: Dict[str, int] = {}
_LOCK = RLock()

def _token_hash(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()

def _purge_expired(now_ts: Optional[int] = None) -> None:
    now = int(now_ts or time.time())
    with _LOCK:
        expired = [k for k, exp in _BLACKLIST.items() if exp <= now]
        for k in expired:
            _BLACKLIST.pop(k, None)

def _maybe_trim_blacklist() -> None:
    """Soft cap to avoid unbounded memory growth."""
    with _LOCK:
        if len(_BLACKLIST) <= BLACKLIST_MAX:
            return
        # Drop the oldest-by-exp entries until within cap
        for k, _exp in sorted(_BLACKLIST.items(), key=lambda kv: kv[1])[: max(0, len(_BLACKLIST) - BLACKLIST_MAX)]:
            _BLACKLIST.pop(k, None)

def _blacklist_add(token: str, exp_ts: Optional[int]) -> int:
    """
    Add token hash to blacklist until exp_ts. If exp is missing,
    blacklist for 24 hours from now. Returns the stored expiry.
    """
    now = int(time.time())
    exp = int(exp_ts) if exp_ts is not None else (now + 24 * 3600)
    with _LOCK:
        _BLACKLIST[_token_hash(token)] = exp
    _maybe_trim_blacklist()
    return exp

def _is_blacklisted(token: str) -> bool:
    _purge_expired()
    with _LOCK:
        return _token_hash(token) in _BLACKLIST

# -----------------------------------------------------------------------------
# Token extraction & decoding
# -----------------------------------------------------------------------------
def _extract_token(request: Request, header_token: Optional[str]) -> str:
    """
    Accept token from:
      1) Authorization: Bearer <token>   (preferred)
      2) Cookie: sb_access=<token>       (if USE_COOKIE_AUTH=1)
      3) Query:  ?access_token=<token>   (if provided)
    """
    if header_token:
        return header_token
    if USE_COOKIE_AUTH:
        raw = request.cookies.get(COOKIE_NAME)
        if raw:
            return raw.split(" ", 1)[1].strip() if raw.lower().startswith("bearer ") else raw.strip()
    q = request.query_params.get(QUERY_TOKEN_NAME)
    if q:
        return q.strip()
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="missing_token")

def _decode_noexp(token: str) -> dict:
    """
    Decode and verify signature (but skip exp) so users can log out
    even if the token just expired.
    """
    try:
        payload = jwt.decode(
            token,
            SECRET_KEY,
            algorithms=[JWT_ALG],
            options={"verify_exp": False},
        )
        if not isinstance(payload, dict):
            raise ValueError("payload_not_dict")
        return payload
    except Exception:
        # Keep detail consistent with the rest of the API
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid_or_expired_token")

# -----------------------------------------------------------------------------
# Public endpoints
# -----------------------------------------------------------------------------
@router.post("/logout", summary="Logout and invalidate the current token")
def logout_user(
    request: Request,
    response: Response,
    header_token: Optional[str] = Depends(oauth2_scheme),
) -> dict:
    """
    - Verifies signature (ignores `exp`)
    - Blacklists the token (by SHA-256) until its `exp` (or 24h if missing)
    - Idempotent: repeated calls remain successful
    - Clears the auth cookie (if enabled)
    """
    token = _extract_token(request, header_token)
    payload = _decode_noexp(token)

    exp_ts = None
    try:
        if "exp" in payload:
            exp_ts = int(payload["exp"])
    except Exception:
        exp_ts = None

    stored_exp = _blacklist_add(token, exp_ts)

    # Clear cookie so browsers stop sending it
    if USE_COOKIE_AUTH:
        response.delete_cookie(
            key=COOKIE_NAME,
            path=COOKIE_PATH,
            domain=COOKIE_DOMAIN,
        )

    # Friendly payload for clients
    until = datetime.fromtimestamp(int(stored_exp), tz=timezone.utc)
    return {
        "message": "logout_success",
        "blacklisted_until": until.isoformat(),
        "expires_in_seconds": max(0, int(stored_exp - time.time())),
    }

# -----------------------------------------------------------------------------
# Dependency (imported by dependencies/authz.py as an optional guard)
# -----------------------------------------------------------------------------
def verify_not_blacklisted(token: str) -> str:
    """
    Call this with a token string to enforce logout blacklisting.
    Raises 401 if blacklisted. Returns the token otherwise.

    Usage:
        from backend.routes.logout import verify_not_blacklisted
        verify_not_blacklisted(token)
    """
    if _is_blacklisted(token):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="token_logged_out")
    return token

# Optional: FastAPI dependency form (for direct use in route dependencies)
def verify_not_blacklisted_dep(
    header_token: Optional[str] = Depends(oauth2_scheme),
    request: Request = None,
) -> str:
    tok = _extract_token(request, header_token)
    return verify_not_blacklisted(tok)

# -----------------------------------------------------------------------------
# Diagnostics (enable with DIAG_AUTH_ENABLED=1)
# -----------------------------------------------------------------------------
@router.get("/_diag_blacklist", include_in_schema=False)
def diag_blacklist() -> dict:
    if not DIAG_AUTH_ENABLED:
        raise HTTPException(status_code=404, detail="not_enabled")
    _purge_expired()
    with _LOCK:
        count = len(_BLACKLIST)
        sample = list(_BLACKLIST.items())[:10]
    return {
        "size": count,
        "sample": [{"hash": h, "exp": exp} for h, exp in sample],
        "max": BLACKLIST_MAX,
    }

__all__ = ["router", "verify_not_blacklisted", "verify_not_blacklisted_dep"]
