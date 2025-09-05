# ================= backend/dependencies/authz.py =================
from __future__ import annotations

import os
from typing import Any, Dict, Iterable, Optional, Sequence, Callable

from datetime import timedelta
from contextlib import suppress

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.orm import Session, load_only

# JOSE → PyJWT fallback
try:
    from jose import jwt  # type: ignore
    from jose.exceptions import ExpiredSignatureError, JWTError  # type: ignore
except Exception:  # pragma: no cover
    import jwt  # type: ignore

    class JWTError(Exception):  # pyjwt compat
        pass

    class ExpiredSignatureError(JWTError):
        pass

# Local deps
from backend.db import get_db
from backend.models.user import User

# Optional blacklist dependency (from your logout route)
with suppress(Exception):
    from backend.routes.logout import verify_not_blacklisted as _verify_not_blacklisted  # type: ignore
# -------------------------------------------------------------------------------------
# ENV / CONFIG
# -------------------------------------------------------------------------------------
from dotenv import load_dotenv

load_dotenv()

# Symmetric default; supports RS/ES if you pass PEMs
SECRET_KEY: Optional[str] = os.getenv("SECRET_KEY") or os.getenv("JWT_SECRET")
PUBLIC_KEY: Optional[str] = os.getenv("JWT_PUBLIC_KEY") or os.getenv("PUBLIC_KEY_PEM")
ALGORITHM: str = (os.getenv("ALGORITHM") or os.getenv("JWT_ALG") or "HS256").upper()
ISSUER: Optional[str] = os.getenv("JWT_ISSUER")
AUDIENCE: Optional[str] = os.getenv("JWT_AUDIENCE")
AUTH_TOKEN_URL: str = os.getenv("AUTH_TOKEN_URL", "/auth/login")
LEEWAY_SECONDS: int = int(os.getenv("JWT_LEEWAY_SECONDS", "30"))  # clock skew
REQUIRE_EMAIL_VERIFIED: bool = (os.getenv("REQUIRE_EMAIL_VERIFIED", "0").strip().lower() in {"1","true","yes","on"})
# Where to look for token besides Authorization header
COOKIE_TOKEN_NAME: str = os.getenv("AUTH_COOKIE_NAME", "access_token")
QUERY_TOKEN_NAME: str = os.getenv("AUTH_QUERY_NAME", "access_token")

# When using RS/ES algorithms, PUBLIC_KEY must be present for verify
def _verifying_key() -> str:
    if ALGORITHM.startswith("HS"):
        if not SECRET_KEY:
            raise RuntimeError("SECRET_KEY/JWT_SECRET missing")
        return SECRET_KEY
    # RS/ES
    if PUBLIC_KEY:
        return PUBLIC_KEY
    if SECRET_KEY:  # some setups still verify with private key
        return SECRET_KEY
    raise RuntimeError("JWT_PUBLIC_KEY/SECRET_KEY missing for verification")

# Single source of truth for OAuth2 tokenUrl; no auto_error => we control 401s
oauth2_scheme = OAuth2PasswordBearer(tokenUrl=AUTH_TOKEN_URL, auto_error=False)

# -------------------------------------------------------------------------------------
# Utilities
# -------------------------------------------------------------------------------------
def _http_unauth(detail: str = "Invalid authentication credentials") -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )

def _http_forbidden(detail: str = "Not enough privileges") -> HTTPException:
    return HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=detail)

def _extract_bearer_from_cookie(request: Request) -> Optional[str]:
    tok = request.cookies.get(COOKIE_TOKEN_NAME)
    if not tok:
        return None
    # Allow "Bearer xxx" or just "xxx"
    return tok.split(" ", 1)[1].strip() if tok.lower().startswith("bearer ") else tok.strip()

def _extract_bearer_from_query(request: Request) -> Optional[str]:
    tok = request.query_params.get(QUERY_TOKEN_NAME)
    if not tok:
        return None
    return tok

def _decode_token(token: str) -> Dict[str, Any]:
    opts = {"verify_aud": bool(AUDIENCE)}
    claims = jwt.decode(  # type: ignore
        token,
        _verifying_key(),
        algorithms=[ALGORITHM],
        audience=AUDIENCE if AUDIENCE else None,
        issuer=ISSUER if ISSUER else None,
        options=opts,
        leeway=LEEWAY_SECONDS,  # type: ignore[arg-type]
    )
    if not isinstance(claims, dict):
        raise JWTError("Token payload is not a dict")
    return claims

# -------------------------------------------------------------------------------------
# Primary dependency: get_current_user (+ multi-source token support)
# -------------------------------------------------------------------------------------
def get_bearer_token(
    request: Request,
    header_token: Optional[str] = Depends(oauth2_scheme),
) -> str:
    """
    Accept token from:
      1) Authorization: Bearer <token>   (preferred)
      2) Cookie: access_token=<token>    (AUTH_COOKIE_NAME)
      3) Query:  ?access_token=<token>   (AUTH_QUERY_NAME)
    """
    token = header_token or _extract_bearer_from_cookie(request) or _extract_bearer_from_query(request)
    if not token:
        raise _http_unauth("Missing bearer token")
    return token

def get_current_user(
    token: str = Depends(get_bearer_token),
    db: Session = Depends(get_db),
) -> User:
    # Optional: reject blacklisted tokens if dependency exists
    if "_verify_not_blacklisted" in globals():
        # Will raise 401 if blacklisted
        globals()["_verify_not_blacklisted"](token)  # type: ignore

    try:
        claims = _decode_token(token)
    except ExpiredSignatureError:
        raise _http_unauth("Token has expired")
    except Exception:
        raise _http_unauth("Invalid token")

    sub = claims.get("sub")
    if not sub:
        raise _http_unauth("Token missing subject")

    # Load only minimal columns needed (avoid heavy relationships)
    user: Optional[User] = (
        db.query(User)
        .options(load_only(User.id, User.role, User.is_active, User.is_deleted, User.email_verified))
        .filter(User.id == int(sub))
        .first()
    )
    if not user:
        raise _http_unauth("User not found")

    if getattr(user, "is_deleted", False):
        raise _http_unauth("Account deleted")

    if not getattr(user, "is_active", True):
        raise _http_unauth("Account disabled")

    if REQUIRE_EMAIL_VERIFIED and not getattr(user, "email_verified", False):
        raise _http_unauth("Email not verified")

    # Attach scopes if present (space-delimited per RFC 8693)
    scopes = str(claims.get("scope", "")).split() if claims.get("scope") else []
    setattr(user, "_jwt_claims", claims)
    setattr(user, "_jwt_scopes", scopes)
    return user

# -------------------------------------------------------------------------------------
# Role/Scope guards
# -------------------------------------------------------------------------------------
def require_roles(*roles: Sequence[str]) -> Callable[[User], User]:
    want = {r.lower() for r in roles}
    def _dep(current_user: User = Depends(get_current_user)) -> User:
        role = (getattr(current_user, "role", None) or "").lower()
        if role not in want:
            raise _http_forbidden(f"Requires role in {sorted(want)}")
        return current_user
    return _dep

def require_scopes(*scopes: Sequence[str]) -> Callable[[User], User]:
    want = {s.lower() for s in scopes}
    def _dep(current_user: User = Depends(get_current_user)) -> User:
        have = {s.lower() for s in getattr(current_user, "_jwt_scopes", [])}
        if not want.issubset(have):
            raise _http_forbidden(f"Requires scopes: {sorted(want)}")
        return current_user
    return _dep

# -------------------------------------------------------------------------------------
# Demo router (Owner dashboard + examples)
# -------------------------------------------------------------------------------------
owner_router = APIRouter(prefix="/owner", tags=["Owner Only"])

@owner_router.get(
    "/dashboard",
    summary="Owner dashboard (role=owner)",
    dependencies=[Depends(require_roles("owner"))],
)
def read_owner_dashboard() -> Dict[str, Any]:
    return {"status": "Welcome, Owner! This is the secure owner dashboard."}

# Example: route needing both role and scope
secure_router = APIRouter(prefix="/secure", tags=["Secure"])

@secure_router.get(
    "/reports",
    summary="View secure reports (role=admin OR owner + scope=reports.read)",
    dependencies=[Depends(require_roles("admin", "owner")), Depends(require_scopes("reports.read"))],
)
def read_reports() -> Dict[str, Any]:
    return {"ok": True, "data": "reports-list"}

