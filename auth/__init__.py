# ================= backend/auth/__init__.py =================
"""
Auth facade for SmartBiz (drop-in, production-ready).

Features
- Tries project-native helpers if present (non-breaking).
- Robust JWT backend (python-jose preferred; PyJWT fallback).
- Works with HS* or RS/ES* algorithms (env-based keys).
- Single source of truth for tokenUrl used by OAuth2PasswordBearer.
- Exposes: create_access_token, decode_token, get_current_user (+ helper get_current_user_id).

Env (all optional unless noted):
  SECRET_KEY / JWT_SECRET            -> HS* signing key (required for HS*)
  JWT_PRIVATE_KEY / PRIVATE_KEY_PEM  -> private key PEM (required for RS/ES)
  JWT_PUBLIC_KEY  / PUBLIC_KEY_PEM   -> public key PEM (verify for RS/ES)
  JWT_ALG=HS256
  JWT_ISSUER, JWT_AUDIENCE
  ACCESS_TOKEN_EXPIRE_MINUTES=60
  JWT_LEEWAY_SECONDS=30
  AUTH_TOKEN_URL=/auth/login         -> path used by OAuth2PasswordBearer
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Iterable, Sequence
from datetime import datetime, timedelta, timezone
from contextlib import suppress
import os

# ───────────────────────────── Try project-native helpers ─────────────────────────────
with suppress(ImportError):
    from backend.utils.security import create_access_token as _create_access_token  # type: ignore
with suppress(ImportError):
    from backend.dependencies.auth import get_current_user as _get_current_user  # type: ignore
with suppress(ImportError):
    from backend.utils.security import get_current_user as _get_current_user  # type: ignore

# ───────────────────────────── Helpers ─────────────────────────────
def _env_bool(key: str, default: bool = False) -> bool:
    v = os.getenv(key)
    return default if v is None else v.strip().lower() in {"1", "true", "yes", "on", "y"}

def _now_utc() -> datetime:
    return datetime.now(timezone.utc)

def _to_ts(dt: datetime) -> int:
    return int(dt.timestamp())

def _pick_first(*items: Optional[str]) -> Optional[str]:
    for it in items:
        if it:
            s = it.strip()
            if s:
                return s
    return None

# ───────────────────────────── JWT backend (jose → pyjwt) ─────────────────────────────
_USE_JOSE = True
try:
    from jose import jwt as _jwt  # type: ignore
    from jose.exceptions import JWTError, ExpiredSignatureError  # type: ignore
except Exception:  # pragma: no cover
    import jwt as _jwt  # type: ignore
    _USE_JOSE = False

    class JWTError(Exception):  # pyjwt compat
        pass

    class ExpiredSignatureError(JWTError):
        pass

# Settings
_JWT_ALG   = os.getenv("JWT_ALG", "HS256").upper()
_SECRET    = _pick_first(os.getenv("SECRET_KEY"), os.getenv("JWT_SECRET"))
_PRIVKEY   = _pick_first(os.getenv("JWT_PRIVATE_KEY"), os.getenv("PRIVATE_KEY_PEM"))
_PUBKEY    = _pick_first(os.getenv("JWT_PUBLIC_KEY"), os.getenv("PUBLIC_KEY_PEM"))
_ISSUER    = os.getenv("JWT_ISSUER") or None
_AUDIENCE  = os.getenv("JWT_AUDIENCE") or None
_EXP_MIN   = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "60"))
_LEEWAY    = int(os.getenv("JWT_LEEWAY_SECONDS", "30"))  # clock-skew tolerance
_TOKEN_URL = os.getenv("AUTH_TOKEN_URL", "/auth/login")  # <— keep in sync across app

def _signing_key() -> str:
    # HS*: prefer SECRET; RS/ES*: require PRIVATE KEY
    if _JWT_ALG.startswith("HS"):
        if not _SECRET:
            raise RuntimeError("SECRET_KEY/JWT_SECRET missing for HS* algorithm")
        return _SECRET  # type: ignore[return-value]
    if not _PRIVKEY:
        raise RuntimeError("JWT_PRIVATE_KEY/PRIVATE_KEY_PEM missing for asymmetric algorithm")
    return _PRIVKEY  # type: ignore[return-value]

def _verifying_key() -> Optional[str]:
    if _JWT_ALG.startswith("HS"):
        return _SECRET
    return _PUBKEY

# ───────────────────────────── Public: create_access_token ─────────────────────────────
def create_access_token(
    data: Dict[str, Any] | None,
    expires_delta: Optional[timedelta] = None,
    *,
    subject: Optional[str] = None,
    scopes: Optional[Iterable[str]] = None,
    issuer: Optional[str] = None,
    audience: Optional[str] = None,
) -> str:
    """
    Create a signed JWT.
    - `subject` -> `sub`
    - `scopes`  -> space-delimited `scope`
    - `issuer`/`audience` override env defaults
    """
    # Delegate to project-native if available (keeps backward compat)
    if "_create_access_token" in globals():
        return globals()["_create_access_token"](data or {}, expires_delta)  # type: ignore[misc]

    exp = _now_utc() + (expires_delta or timedelta(minutes=_EXP_MIN))
    payload: Dict[str, Any] = dict(data or {})
    if subject:
        payload["sub"] = str(subject)
    if scopes:
        payload["scope"] = " ".join(map(str, scopes))
    if _ISSUER or issuer:
        payload["iss"] = (issuer or _ISSUER)
    if _AUDIENCE or audience:
        payload["aud"] = (audience or _AUDIENCE)

    # Numeric timestamps (widely compatible)
    now_ts = _to_ts(_now_utc())
    payload["iat"] = now_ts
    payload["nbf"] = now_ts
    payload["exp"] = _to_ts(exp)

    token = _jwt.encode(payload, _signing_key(), algorithm=_JWT_ALG)  # type: ignore[arg-type]
    return token if isinstance(token, str) else token.decode("utf-8")

# ───────────────────────────── Public: decode_token ─────────────────────────────
def decode_token(token: str) -> Dict[str, Any]:
    """
    Decode & validate JWT using env config (alg/keys/iss/aud/leeway).
    Raises JWTError/ExpiredSignatureError on failure.
    """
    key = _verifying_key() or _signing_key()
    options = {"verify_aud": bool(_AUDIENCE)}
    claims = _jwt.decode(  # type: ignore[call-arg]
        token,
        key,
        algorithms=[_JWT_ALG],
        audience=_AUDIENCE if _AUDIENCE else None,
        issuer=_ISSUER if _ISSUER else None,
        options=options,
        leeway=_LEEWAY,  # type: ignore[arg-type]
    )
    if not isinstance(claims, dict):
        raise JWTError("Invalid token payload")
    return claims

# ───────────────────────────── FastAPI dependencies ─────────────────────────────
if "_get_current_user" in globals():
    # Use project's own dependency (likely returns a DB user)
    get_current_user = globals()["_get_current_user"]  # type: ignore[assignment]
else:
    from fastapi import Depends, HTTPException, status, Request
    from fastapi.security import OAuth2PasswordBearer

    # Single source of truth for tokenUrl; uses env AUTH_TOKEN_URL or /auth/login
    oauth2_scheme = OAuth2PasswordBearer(tokenUrl=_TOKEN_URL, auto_error=False)

    def _unauth(detail: str = "Invalid authentication credentials") -> HTTPException:
        return HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=detail,
            headers={"WWW-Authenticate": "Bearer"},
        )

    def get_current_user(token: Optional[str] = Depends(oauth2_scheme)) -> Dict[str, Any]:
        """
        Decode JWT and return its claims.
        NOTE: If you need a DB user object, plug your own dependency that fetches a User by `claims['sub']`.
        """
        if not token:
            raise _unauth("Missing bearer token")
        try:
            claims = decode_token(token)
        except ExpiredSignatureError:
            raise _unauth("Token has expired")
        except Exception:
            raise _unauth("Invalid token")

        if not claims.get("sub"):
            raise _unauth("Token missing subject")
        return claims

# Optional convenience: get_current_user_id (keeps handlers clean)
def get_current_user_id(claims: Dict[str, Any] | None) -> str:
    if not claims or "sub" not in claims:
        raise ValueError("Missing user claims/subject")
    return str(claims["sub"])

# ───────────────────────────── Public API ─────────────────────────────
__all__ = [
    "create_access_token",
    "decode_token",
    "get_current_user",
    "get_current_user_id",
]
