# ================= backend/auth/__init__.py =================
"""
SmartBiz Auth (JWT facade) — production-ready, backward compatible.

Features
- Uses python-jose if available; falls back to PyJWT.
- HS*, RS*, ES* algorithms supported via env keys.
- Single source of truth for tokenUrl (OAuth2PasswordBearer).
- Works with Bearer header and optional auth cookie.
- Can return raw JWT claims OR a DB User (opt-in via env).
- Exposes: create_access_token, decode_token, get_current_user, get_current_user_id.

ENV (optional unless noted)
  # Keys / algorithm
  JWT_ALG=HS256
  SECRET_KEY / JWT_SECRET                (required for HS*)
  JWT_PRIVATE_KEY / PRIVATE_KEY_PEM      (required for RS/ES sign)
  JWT_PUBLIC_KEY  / PUBLIC_KEY_PEM       (verify for RS/ES)

  # Claims / validation
  JWT_ISSUER
  JWT_AUDIENCE
  ACCESS_TOKEN_EXPIRE_MINUTES=60
  JWT_LEEWAY_SECONDS=30

  # OAuth2PasswordBearer
  AUTH_TOKEN_URL=/auth/login

  # Cookies (optional)
  AUTH_COOKIE_NAME=sb_access

  # DB user fetching (optional)
  AUTH_FETCH_DB_USER=0|1                  (default 0 → return claims dict)
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Iterable
from datetime import datetime, timedelta, timezone
from contextlib import suppress
import os
import base64
import json

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
    try:
        import jwt as _pyjwt  # PyJWT
        _jwt = _pyjwt  # type: ignore
        _USE_JOSE = False

        class JWTError(Exception):  # pyjwt compat
            pass

        class ExpiredSignatureError(JWTError):
            pass
    except Exception:  # ultimate fallback: unsigned
        _jwt = None  # type: ignore
        class JWTError(Exception): ...
        class ExpiredSignatureError(JWTError): ...

# Settings
_JWT_ALG   = os.getenv("JWT_ALG", "HS256").upper()
_SECRET    = _pick_first(os.getenv("SECRET_KEY"), os.getenv("JWT_SECRET"))
_PRIVKEY   = _pick_first(os.getenv("JWT_PRIVATE_KEY"), os.getenv("PRIVATE_KEY_PEM"))
_PUBKEY    = _pick_first(os.getenv("JWT_PUBLIC_KEY"), os.getenv("PUBLIC_KEY_PEM"))
_ISSUER    = os.getenv("JWT_ISSUER") or None
_AUDIENCE  = os.getenv("JWT_AUDIENCE") or None
_EXP_MIN   = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "60"))
_LEEWAY    = int(os.getenv("JWT_LEEWAY_SECONDS", "30"))
_TOKEN_URL = os.getenv("AUTH_TOKEN_URL", "/auth/login")
_COOKIE    = os.getenv("AUTH_COOKIE_NAME", "sb_access")
_FETCH_DB  = _env_bool("AUTH_FETCH_DB_USER", False)

# Optional project-native overrides (keeps old code working)
with suppress(ImportError):
    from backend.utils.security import create_access_token as _create_access_token  # type: ignore
with suppress(ImportError):
    from backend.dependencies.auth import get_current_user as _project_get_current_user  # type: ignore
with suppress(ImportError):
    from backend.utils.security import get_current_user as _project_get_current_user  # type: ignore

# Optional DB imports (used only if AUTH_FETCH_DB_USER=1)
_db_get_db = None
_UserModel = None
if _FETCH_DB:
    with suppress(Exception):
        from db import get_db as _db_get_db  # type: ignore
    with suppress(Exception):
        from backend.db import get_db as _db_get_db  # type: ignore
    with suppress(Exception):
        from models.user import User as _UserModel  # type: ignore
    with suppress(Exception):
        from backend.models.user import User as _UserModel  # type: ignore

# ───────────────────────────── Keys ─────────────────────────────
def _signing_key() -> str:
    # HS*: need SECRET; RS/ES*: need PRIVATE KEY
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
    data: Dict[str, Any] | None = None,
    expires_delta: Optional[timedelta] = None,
    *,
    subject: Optional[str] = None,
    scopes: Optional[Iterable[str]] = None,
    issuer: Optional[str] = None,
    audience: Optional[str] = None,
    jti: Optional[str] = None,
) -> str:
    """
    Create a signed JWT.
    - `subject` -> `sub`
    - `scopes`  -> space-delimited `scope`
    - `issuer`/`audience` override env defaults
    - `jti` optional ID
    """
    # Delegate to project-native if available (backward compat)
    if "_create_access_token" in globals():
        return globals()["_create_access_token"](data or {}, expires_delta)  # type: ignore[misc]

    if _jwt is None:
        # Unsigned fallback (dev-only). DO NOT USE IN PROD.
        payload = dict(data or {})
        if subject: payload["sub"] = str(subject)
        if scopes:  payload["scope"] = " ".join(map(str, scopes))
        if _ISSUER or issuer:   payload["iss"] = (issuer or _ISSUER)
        if _AUDIENCE or audience: payload["aud"] = (audience or _AUDIENCE)
        if jti: payload["jti"] = jti
        now = _to_ts(_now_utc())
        exp = now + int((expires_delta or timedelta(minutes=_EXP_MIN)).total_seconds())
        payload.update({"iat": now, "nbf": now, "exp": exp})
        return base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()

    exp_dt = _now_utc() + (expires_delta or timedelta(minutes=_EXP_MIN))
    now_ts = _to_ts(_now_utc())

    payload: Dict[str, Any] = dict(data or {})
    if subject: payload["sub"] = str(subject)
    if scopes:  payload["scope"] = " ".join(map(str, scopes))
    if _ISSUER or issuer:   payload["iss"] = (issuer or _ISSUER)
    if _AUDIENCE or audience: payload["aud"] = (audience or _AUDIENCE)
    if jti: payload["jti"] = jti

    payload["iat"] = now_ts
    payload["nbf"] = now_ts
    payload["exp"] = _to_ts(exp_dt)

    token = _jwt.encode(payload, _signing_key(), algorithm=_JWT_ALG)  # type: ignore[arg-type]
    return token if isinstance(token, str) else token.decode("utf-8")

# ───────────────────────────── Public: decode_token ─────────────────────────────
def decode_token(token: str) -> Dict[str, Any]:
    """
    Decode & validate JWT using env config (alg/keys/iss/aud/leeway).
    Raises JWTError/ExpiredSignatureError on failure.
    """
    if _jwt is None:
        # Unsigned fallback decode (dev)
        try:
            body = token.split(".")[1] if "." in token else token
            pad = "=" * ((4 - len(body) % 4) % 4)
            claims = json.loads(base64.urlsafe_b64decode(body + pad).decode())
            if not isinstance(claims, dict):
                raise JWTError("Invalid token payload")
            # naive exp check
            if "exp" in claims and isinstance(claims["exp"], int) and claims["exp"] < _to_ts(_now_utc()):
                raise ExpiredSignatureError("Token has expired")
            return claims
        except Exception as e:
            raise JWTError(str(e))

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
# Prefer project-defined dependency if it exists.
if "_project_get_current_user" in globals():
    get_current_user = globals()["_project_get_current_user"]  # type: ignore[assignment]

else:
    from fastapi import Depends, HTTPException, status, Request
    from fastapi.security import OAuth2PasswordBearer

    oauth2_scheme = OAuth2PasswordBearer(tokenUrl=_TOKEN_URL, auto_error=False)

    def _unauth(detail: str = "Invalid authentication credentials") -> HTTPException:
        return HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=detail,
            headers={"WWW-Authenticate": "Bearer"},
        )

    def _token_from_request(request: Request, bearer_token: Optional[str]) -> Optional[str]:
        # 1) Prefer Authorization: Bearer <token> from OAuth2PasswordBearer
        if bearer_token:
            return bearer_token
        # 2) Try raw Authorization header
        auth = request.headers.get("authorization") or request.headers.get("Authorization")
        if auth and auth.lower().startswith("bearer "):
            return auth.split(" ", 1)[1].strip()
        # 3) Try cookie
        cookie = request.cookies.get(_COOKIE)
        if cookie:
            return cookie
        return None

    if _FETCH_DB and _db_get_db and _UserModel is not None:
        # Returns a DB User object
        from sqlalchemy.orm import Session  # type: ignore

        def get_current_user(
            request: Request,
            token: Optional[str] = Depends(oauth2_scheme),
            db: Session = Depends(_db_get_db),  # type: ignore[arg-type]
        ):
            t = _token_from_request(request, token)
            if not t:
                raise _unauth("Missing bearer token")
            try:
                claims = decode_token(t)
            except ExpiredSignatureError:
                raise _unauth("Token has expired")
            except Exception:
                raise _unauth("Invalid token")
            sub = str(claims.get("sub") or "")
            if not sub:
                raise _unauth("Token missing subject")
            user = db.query(_UserModel).filter(_UserModel.id == sub).first()
            if not user:
                raise _unauth("User not found")
            return user
    else:
        # Returns JWT claims dict
        def get_current_user(
            request: Request,
            token: Optional[str] = Depends(oauth2_scheme),
        ) -> Dict[str, Any]:
            t = _token_from_request(request, token)
            if not t:
                raise _unauth("Missing bearer token")
            try:
                claims = decode_token(t)
            except ExpiredSignatureError:
                raise _unauth("Token has expired")
            except Exception:
                raise _unauth("Invalid token")
            if not claims.get("sub"):
                raise _unauth("Token missing subject")
            return claims

# Convenience helper
def get_current_user_id(claims_or_user: Any) -> str:
    """
    Accepts either a claims dict (when AUTH_FETCH_DB_USER=0) or a User model (when AUTH_FETCH_DB_USER=1).
    """
    if isinstance(claims_or_user, dict) and "sub" in claims_or_user:
        return str(claims_or_user["sub"])
    # Try common ORM attrs
    for attr in ("id", "user_id", "pk"):
        if hasattr(claims_or_user, attr):
            return str(getattr(claims_or_user, attr))
    raise ValueError("Missing user id")

# Public API
__all__ = [
    "create_access_token",
    "decode_token",
    "get_current_user",
    "get_current_user_id",
]
