# ================= backend/auth/__init__.py =================
"""
SmartBiz Auth facade (backward compatible).

Goals
- Keep legacy imports working: `from backend.auth import get_current_user`
- Prefer the canonical dependencies in `backend.dependencies.authz`
- Avoid circular imports and surprise 500s
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

# -----------------------------------------------------------------------------
# Prefer canonical auth dependencies from backend.dependencies.authz
# -----------------------------------------------------------------------------
with suppress(Exception):
    from backend.dependencies.authz import (  # type: ignore
        oauth2_scheme,
        get_bearer_token,
        get_current_user,
        require_roles,
        require_scopes,
        get_current_user_id,
    )

# Token URL (used only if you import oauth2_scheme from here)
AUTH_TOKEN_URL: str = (os.getenv("AUTH_TOKEN_URL", "/auth/login") or "/auth/login").strip()

# -----------------------------------------------------------------------------
# Create/Decode token: prefer auth_routes implementation to stay consistent
# -----------------------------------------------------------------------------
# 1) Try to import the exact implementations from auth_routes.py
_create_from_routes = None
_decode_from_routes = None
with suppress(Exception):
    from backend.routes.auth_routes import (  # type: ignore
        create_access_token as _create_from_routes,  # same semantics as your login
        decode_token as _decode_from_routes,
    )

# 2) Fallback to a local, compatible implementation (matches auth_routes defaults)
#    We keep env names identical so behavior is consistent.
JWT_ALG = (os.getenv("JWT_ALG") or os.getenv("JWT_ALGORITHM") or "HS256").upper()
SECRET_KEY = os.getenv("SECRET_KEY") or os.getenv("JWT_SECRET")
if not SECRET_KEY:
    # Development fallback only; set a real SECRET_KEY/JWT_SECRET in prod!
    SECRET_KEY = base64.urlsafe_b64encode(os.urandom(48)).decode()

ISSUER_DEFAULT = os.getenv("JWT_ISSUER") or "smartbiz-api"
ACCESS_MIN = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "60"))
LEEWAY_SECONDS = int(os.getenv("JWT_LEEWAY_SECONDS", "30"))

# jose → pyjwt fallback (don’t crash if none is installed)
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
    except Exception:
        _jwt = None  # type: ignore

        class _JWTError(Exception): ...
        class _Expired(_JWTError): ...


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _ensure_str(x: Any) -> str:
    return x.decode("utf-8") if isinstance(x, (bytes, bytearray)) else str(x)


def _secs(minutes: int) -> int:
    return int(timedelta(minutes=minutes).total_seconds())


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
            # soft-expiry check
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
        # Normalize exceptions to a single error to avoid 500 traces
        raise _JWTError("invalid_or_expired_token") from e


# Public surface (prefer auth_routes functions when available)
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
            # Some older versions returned only token; compute expires
            tok = _create_from_routes(claims, minutes)  # type: ignore[misc]
            return _ensure_str(tok), _secs(minutes)
        except Exception:
            # Fall back safely
            pass
    return _local_create_access_token(claims, minutes)


def decode_token(token: str) -> Dict[str, Any]:
    """
    Validate and decode a token.
    If backend.routes.auth_routes.decode_token exists, use it.
    Otherwise, use the local compatible implementation above.
    """
    if _decode_from_routes:
        try:
            return _decode_from_routes(token)  # type: ignore[misc]
        except Exception:
            # Fall back safely
            pass
    return _local_decode_token(token)


# -----------------------------------------------------------------------------
# Public API
# -----------------------------------------------------------------------------
__all__ = [
    # from dependencies.authz (if available)
    "oauth2_scheme",
    "get_bearer_token",
    "get_current_user",
    "require_roles",
    "require_scopes",
    "get_current_user_id",
    # token helpers
    "create_access_token",
    "decode_token",
    # extras
    "AUTH_TOKEN_URL",
]
