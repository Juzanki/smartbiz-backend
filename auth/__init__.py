# ================= backend/auth/__init__.py =================
"""
SmartBiz Auth facade (backward compatible).

Goals
- Keep legacy imports working: `from backend.auth import get_current_user`
- Prefer the canonical dependencies in `backend.dependencies.authz`
- Fallback cleanly to local deps or internal minimal impl (no circulars / no crashes)
- Provide create_access_token / decode_token that match auth_routes if present

Exports
- oauth2_scheme, get_bearer_token, get_current_user, require_roles, require_scopes, get_current_user_id
- create_access_token, decode_token
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, Optional
from datetime import datetime, timedelta, timezone
from contextlib import suppress
import os
import base64
import json

# FastAPI / Security
with suppress(Exception):
    from fastapi import Depends, HTTPException, Request, status  # type: ignore
with suppress(Exception):
    from fastapi.security import OAuth2PasswordBearer  # type: ignore

# -----------------------------------------------------------------------------
# Env / Defaults
# -----------------------------------------------------------------------------
AUTH_TOKEN_URL: str = (os.getenv("AUTH_TOKEN_URL", "/auth/login") or "/auth/login").strip()

# Token semantics (sync with your auth_routes if customized)
JWT_ALG = (os.getenv("JWT_ALG") or os.getenv("JWT_ALGORITHM") or "HS256").upper()
SECRET_KEY = os.getenv("SECRET_KEY") or os.getenv("JWT_SECRET")
if not SECRET_KEY:
    # Dev-only fallback. In production make sure SECRET_KEY/JWT_SECRET is set.
    SECRET_KEY = base64.urlsafe_b64encode(os.urandom(48)).decode()

ISSUER_DEFAULT = os.getenv("JWT_ISSUER") or "smartbiz-api"
ACCESS_MIN = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "60"))
LEEWAY_SECONDS = int(os.getenv("JWT_LEEWAY_SECONDS", "30"))

# jose → pyjwt fallback (best-effort; don't crash if not installed)
_use_jose = True
try:
    from jose import jwt as _jwt  # type: ignore
    from jose.exceptions import JWTError as _JWTError, ExpiredSignatureError as _Expired  # type: ignore
except Exception:  # pragma: no cover
    _use_jose = False
    try:
        import jwt as _pyjwt  # type: ignore
        _jwt = _pyjwt  # type: ignore

        class _JWTError(Exception): ...
        class _Expired(_JWTError): ...
    except Exception:  # pragma: no cover
        _jwt = None  # type: ignore

        class _JWTError(Exception): ...
        class _Expired(_JWTError): ...


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _ensure_str(x: Any) -> str:
    return x.decode("utf-8") if isinstance(x, (bytes, bytearray)) else str(x)


def _secs(minutes: int) -> int:
    return int(timedelta(minutes=minutes).total_seconds())


# -----------------------------------------------------------------------------
# Prefer canonical auth dependencies from backend.dependencies.authz
# -----------------------------------------------------------------------------
# These names may or may not be provided; we fill missing ones later.
oauth2_scheme = None
get_bearer_token = None
get_current_user = None
require_roles = None
require_scopes = None
get_current_user_id = None

with suppress(Exception):
    from backend.dependencies.authz import (  # type: ignore
        oauth2_scheme as _can_oauth2_scheme,
        get_bearer_token as _can_get_bearer_token,
        get_current_user as _can_get_current_user,
        require_roles as _can_require_roles,
        require_scopes as _can_require_scopes,
        get_current_user_id as _can_get_current_user_id,
    )

    oauth2_scheme = _can_oauth2_scheme
    get_bearer_token = _can_get_bearer_token
    get_current_user = _can_get_current_user
    require_roles = _can_require_roles
    require_scopes = _can_require_scopes
    get_current_user_id = _can_get_current_user_id

# -----------------------------------------------------------------------------
# Explicit request (legacy): also allow local deps:
#   from .deps import get_current_user
# This keeps old imports working if canonical isn't present.
# -----------------------------------------------------------------------------
with suppress(Exception):
    if get_current_user is None:
        from .deps import get_current_user as _deps_get_current_user  # type: ignore

        get_current_user = _deps_get_current_user


# -----------------------------------------------------------------------------
# Fallback: minimal local implementations (only for names still missing)
# -----------------------------------------------------------------------------
if oauth2_scheme is None:
    # Avoid auto_error to control 401 shape ourselves.
    with suppress(Exception):
        oauth2_scheme = OAuth2PasswordBearer(tokenUrl=AUTH_TOKEN_URL, auto_error=False)  # type: ignore


if get_bearer_token is None:
    def get_bearer_token(  # type: ignore
        request: "Request",  # noqa: F821
        token: Optional[str] = Depends(oauth2_scheme) if oauth2_scheme else None,  # type: ignore
    ) -> Optional[str]:
        """
        Extract bearer token from:
        - Authorization: Bearer <token>
        - Cookie: access_token=<token>
        - Query param: ?access_token=<token> (last resort)
        """
        # 1) From oauth2_scheme (Authorization header)
        if token:
            return token

        # 2) From raw Authorization header (if oauth2_scheme missing)
        with suppress(Exception):
            auth = request.headers.get("Authorization") or request.headers.get("authorization")
            if auth and auth.lower().startswith("bearer "):
                return auth.split(" ", 1)[1].strip()

        # 3) Cookie
        with suppress(Exception):
            cookie_tok = request.cookies.get("access_token")
            if cookie_tok:
                return cookie_tok

        # 4) Query (discouraged, but helpful in dev)
        with suppress(Exception):
            q = request.query_params.get("access_token")
            if q:
                return q

        return None


# Create/Decode token: prefer auth_routes implementation to stay consistent
_create_from_routes = None
_decode_from_routes = None
with suppress(Exception):
    from backend.routes.auth_routes import (  # type: ignore
        create_access_token as _create_from_routes,
        decode_token as _decode_from_routes,
    )


def _local_create_access_token(
    claims: Dict[str, Any],
    minutes: int = ACCESS_MIN,
) -> tuple[str, int]:
    """
    Local fallback that mirrors auth_routes payload:
      typ=access, iss=smartbiz-api, iat/exp, and stringified sub.
    Returns (token, expires_in_seconds).
    """
    now = _now_utc()
    exp = now + timedelta(minutes=minutes)
    payload = {
        "typ": "access",
        "iss": ISSUER_DEFAULT,
        "iat": int(now.timestamp()),
        "exp": int(exp.timestamp()),
        **(claims or {}),
    }
    if "sub" in payload:
        payload["sub"] = str(payload["sub"])

    if _jwt is None:
        # last-resort dev mode (unsigned-ish)
        token = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()
        return _ensure_str(token), _secs(minutes)

    token = _jwt.encode(payload, SECRET_KEY, algorithm=JWT_ALG)  # type: ignore[arg-type]
    return _ensure_str(token), _secs(minutes)


def _local_decode_token(token: str) -> Dict[str, Any]:
    if _jwt is None:
        # naive decode for dev fallback
        try:
            body = token.split(".")[1] if "." in token else token
            pad = "=" * ((4 - len(body) % 4) % 4)
            claims = json.loads(base64.urlsafe_b64decode(body + pad).decode())
            if not isinstance(claims, dict):
                raise _JWTError("invalid_token_payload")
            if "exp" in claims and int(claims["exp"]) < int(_now_utc().timestamp()) - LEEWAY_SECONDS:
                raise _Expired("token_expired")
            return claims
        except Exception as e:
            raise _JWTError(str(e))

    # jose/pyjwt proper verify
    try:
        claims = _jwt.decode(  # type: ignore[call-arg]
            token,
            SECRET_KEY,
            algorithms=[JWT_ALG],
            options={"verify_aud": False},
            issuer=ISSUER_DEFAULT,
            leeway=LEEWAY_SECONDS,  # type: ignore[arg-type]
        )
        if not isinstance(claims, dict):
            raise _JWTError("invalid_token_payload")
        return claims
    except Exception as e:
        raise _JWTError("invalid_or_expired_token") from e


def create_access_token(claims: Dict[str, Any], minutes: int = ACCESS_MIN) -> tuple[str, int]:
    """
    Create a token identical to the one produced in /auth/login.
    If backend.routes.auth_routes.create_access_token exists, use it.
    Otherwise, use the local compatible implementation above.
    """
    if _create_from_routes:
        try:
            tok, exp = _create_from_routes(claims, minutes)  # type: ignore[misc]
            return _ensure_str(tok), int(exp)
        except TypeError:
            tok = _create_from_routes(claims, minutes)  # type: ignore[misc]
            return _ensure_str(tok), _secs(minutes)
        except Exception:
            pass
    return _local_create_access_token(claims, minutes)


def decode_token(token: str) -> Dict[str, Any]:
    """
    Validate and decode a token.
    If backend.routes.auth_routes.decode_token exists, use it.
    Otherwise, use the local compatible implementation above.
    """
    if _decode_from_routes:
        with suppress(Exception):
            return _decode_from_routes(token)  # type: ignore[misc]
    return _local_decode_token(token)


# -----------------------------------------------------------------------------
# Fallback get_current_user / require_* only if still missing
# -----------------------------------------------------------------------------
if get_current_user is None:
    def get_current_user(  # type: ignore
        request: "Request",  # noqa: F821
        token: Optional[str] = Depends(get_bearer_token) if get_bearer_token else None,  # type: ignore
    ):
        """
        Minimal, lazy-import version to avoid circulars.
        Decodes token, fetches user from DB, returns user model.
        """
        if token is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing token")  # type: ignore

        try:
            claims = decode_token(token)
        except Exception:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired token")  # type: ignore

        sub = str(claims.get("sub") or "").strip()
        if not sub:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token payload")  # type: ignore

        # Lazy imports to avoid circular refs
        with suppress(Exception):
            from sqlalchemy.orm import Session  # type: ignore
            from backend.db import get_db  # type: ignore
            from backend.models.user import User  # type: ignore

            def _fetch_user() -> "User | None":  # noqa: F821
                db: "Session"
                # We avoid FastAPI Depends here to stay generic:
                from contextlib import contextmanager

                @contextmanager
                def _db_ctx():
                    db = None
                    try:
                        db = next(get_db())
                        yield db
                    finally:
                        with suppress(Exception):
                            if db:
                                db.close()

                with _db_ctx() as db:
                    return db.query(User).get(int(sub))  # type: ignore

            user = _fetch_user()
            if not user:
                raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")  # type: ignore
            # Optional: check disabled/blocked flags if your model has them
            if getattr(user, "is_blocked", False) or getattr(user, "disabled", False):
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Account disabled")  # type: ignore
            return user

        # If we couldn't import DB/model, at least return claims
        return claims  # last resort for environments without DB


if get_current_user_id is None:
    def get_current_user_id(  # type: ignore
        request: "Request",  # noqa: F821
        token: Optional[str] = Depends(get_bearer_token) if get_bearer_token else None,  # type: ignore
    ) -> str:
        if token is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing token")  # type: ignore
        claims = decode_token(token)
        sub = str(claims.get("sub") or "").strip()
        if not sub:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token payload")  # type: ignore
        return sub


def _has_any(item: Iterable[str] | None, required: Iterable[str] | None) -> bool:
    if not required:
        return True
    have = {str(x).lower() for x in (item or [])}
    need = {str(x).lower() for x in (required or [])}
    return not need.isdisjoint(have)


if require_roles is None:
    def require_roles(  # type: ignore
        roles: Iterable[str],
    ):
        """
        Dependency to enforce at least one of the given roles.
        Reads from user.roles (list/str) or JWT 'roles' claim.
        """
        def _dep(user=Depends(get_current_user)):  # type: ignore
            # try user.roles
            user_roles = None
            with suppress(Exception):
                r = getattr(user, "roles", None)
                if isinstance(r, str):
                    user_roles = [r]
                elif isinstance(r, (list, tuple, set)):
                    user_roles = list(r)

            if _has_any(user_roles, roles):
                return user

            # try claims (if user is dict-like)
            if isinstance(user, dict) and _has_any(user.get("roles"), roles):
                return user

            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient role")  # type: ignore
        return _dep


if require_scopes is None:
    def require_scopes(  # type: ignore
        scopes: Iterable[str],
    ):
        """
        Dependency to enforce at least one of the given OAuth2 scopes.
        Looks at JWT 'scope'/'scopes' or user.scopes if present.
        """
        def _dep(user=Depends(get_current_user)):  # type: ignore
            # user.scopes
            user_scopes = None
            with suppress(Exception):
                s = getattr(user, "scopes", None)
                if isinstance(s, str):
                    user_scopes = s.split()
                elif isinstance(s, (list, tuple, set)):
                    user_scopes = list(s)

            if _has_any(user_scopes, scopes):
                return user

            # claims scopes
            if isinstance(user, dict):
                claim_scopes = None
                if "scopes" in user:
                    claim_scopes = user["scopes"]
                elif "scope" in user:
                    claim_scopes = str(user["scope"]).split()
                if _has_any(claim_scopes, scopes):
                    return user

            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient scope")  # type: ignore
        return _dep


# -----------------------------------------------------------------------------
# Public API (__all__)
# -----------------------------------------------------------------------------
__all__ = [
    "oauth2_scheme",
    "get_bearer_token",
    "get_current_user",
    "require_roles",
    "require_scopes",
    "get_current_user_id",
    "create_access_token",
    "decode_token",
    "AUTH_TOKEN_URL",
]
