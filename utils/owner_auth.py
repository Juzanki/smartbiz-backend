# ============================ backend/dependencies/authz.py ============================
from __future__ import annotations

import os
from contextlib import suppress
from typing import Any, Dict, Iterable, Optional, Sequence, Callable, List

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.orm import Session, load_only

# DB & model
try:
    from backend.db import get_db
except Exception:  # pragma: no cover
    from db import get_db  # type: ignore

try:
    from backend.models.user import User
except Exception:  # pragma: no cover
    from models.user import User  # type: ignore

# Optional blacklist hook (ignore if not present)
with suppress(Exception):
    from backend.routes.logout import verify_not_blacklisted as _verify_not_blacklisted  # type: ignore

# ---------------------------- JWT / ENV (aligned with auth_routes.py) ----------------------------
import jwt  # PyJWT

SECRET_KEY: Optional[str] = os.getenv("SECRET_KEY") or os.getenv("JWT_SECRET")
if not SECRET_KEY:
    # If your auth_routes.py creates tokens with a random fallback, you should set
    # SECRET_KEY in the environment for consistency between create/verify.
    # Here we deliberately *do not* generate a random key to avoid verify failures.
    raise RuntimeError("SECRET_KEY/JWT_SECRET is required for token verification")

JWT_ALG: str = os.getenv("JWT_ALG", os.getenv("JWT_ALGORITHM", "HS256"))
JWT_ISSUER: Optional[str] = os.getenv("JWT_ISSUER")  # optional
JWT_AUDIENCE: Optional[str] = os.getenv("JWT_AUDIENCE")  # optional
JWT_LEEWAY: int = int(os.getenv("JWT_LEEWAY_SECONDS", "30"))  # clock skew (seconds)

# Token locations
USE_COOKIE_AUTH: bool = (os.getenv("USE_COOKIE_AUTH", "true").strip().lower() in {"1", "true", "yes", "on", "y"})
COOKIE_NAME: str = os.getenv("AUTH_COOKIE_NAME", "sb_access")
QUERY_TOKEN_NAME: str = os.getenv("AUTH_QUERY_NAME", "access_token")

# Optional user gates
REQUIRE_EMAIL_VERIFIED: bool = (os.getenv("REQUIRE_EMAIL_VERIFIED", "0").strip().lower() in {"1", "true", "yes", "on"})

# OAuth2 helper (not used for validation—only for reading the header without auto 401)
AUTH_TOKEN_URL: str = os.getenv("AUTH_TOKEN_URL", "/auth/login")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl=AUTH_TOKEN_URL, auto_error=False)

# ---------------------------- HTTP helpers ----------------------------
def _unauth(detail: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )

def _forbidden(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=detail)

# ---------------------------- Token extraction / decoding ----------------------------
def _extract_bearer_from_cookie(request: Request) -> Optional[str]:
    if not USE_COOKIE_AUTH:
        return None
    raw = request.cookies.get(COOKIE_NAME)
    if not raw:
        return None
    return raw.split(" ", 1)[1].strip() if raw.lower().startswith("bearer ") else raw.strip()

def _extract_bearer_from_query(request: Request) -> Optional[str]:
    tok = request.query_params.get(QUERY_TOKEN_NAME)
    return tok.strip() if tok else None

def get_bearer_token(request: Request, header_token: Optional[str] = Depends(oauth2_scheme)) -> str:
    """
    Accept token from:
      1) Authorization: Bearer <token>
      2) Cookie: AUTH_COOKIE_NAME (sb_access by default)
      3) Query:  ?access_token=<token> (disabled unless provided)
    """
    token = header_token or _extract_bearer_from_cookie(request) or _extract_bearer_from_query(request)
    if not token:
        raise _unauth("missing_token")
    return token

def _decode_token(token: str) -> Dict[str, Any]:
    try:
        # Verify issuer/audience only if provided
        options = {"verify_aud": bool(JWT_AUDIENCE)}
        payload = jwt.decode(
            token,
            SECRET_KEY,
            algorithms=[JWT_ALG],
            issuer=JWT_ISSUER if JWT_ISSUER else None,
            audience=JWT_AUDIENCE if JWT_AUDIENCE else None,
            options=options,
            leeway=JWT_LEEWAY,  # type: ignore[arg-type]
        )
        if not isinstance(payload, dict):
            raise _unauth("invalid_or_expired_token")
        return payload
    except jwt.ExpiredSignatureError:
        raise _unauth("invalid_or_expired_token")
    except Exception:
        # keep detail aligned with auth_routes.decode_token
        raise _unauth("invalid_or_expired_token")

# ---------------------------- Safe SQLAlchemy helpers ----------------------------
def _has_col(model, name: str) -> bool:
    try:
        return name in model.__table__.c
    except Exception:
        return hasattr(model, name)

def _col(model, name: str):
    return getattr(model, name)

def _load_only_existing(*names: str):
    cols: List[Any] = []
    for n in names:
        if _has_col(User, n):
            cols.append(_col(User, n))
    return load_only(*cols) if cols else None

# ---------------------------- Primary dependency: get_current_user ----------------------------
def get_current_user(token: str = Depends(get_bearer_token), db: Session = Depends(get_db)) -> User:
    # Optional blacklist check (no-op if not wired)
    if "_verify_not_blacklisted" in globals():
        try:
            globals()["_verify_not_blacklisted"](token)  # type: ignore[misc]
        except HTTPException:
            # Bubble up any 401 from blacklist
            raise

    claims = _decode_token(token)

    # Mirror auth_routes.py expectations
    if claims.get("typ") != "access":
        raise _unauth("invalid_token_type")

    sub = claims.get("sub")
    if not sub:
        raise _unauth("invalid_token_payload")

    try:
        uid = int(sub)
    except Exception:
        raise _unauth("invalid_sub")

    # Fetch user with minimal columns that actually exist
    query = db.query(User)
    opt = _load_only_existing("id", "role", "is_active", "is_deleted", "email_verified")
    if opt is not None:
        query = query.options(opt)

    user = None
    try:
        # Prefer SA 1.4+
        user = db.get(User, uid)  # type: ignore[attr-defined]
    except Exception:
        user = query.filter(User.id == uid).first()

    if not user:
        raise _unauth("user_not_found")

    if _has_col(User, "is_deleted") and bool(getattr(user, "is_deleted", False)):
        raise _unauth("user_not_found")  # do not reveal deletion state

    if _has_col(User, "is_active") and not bool(getattr(user, "is_active", True)):
        # Keep same shape as auth_routes.py
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="user_inactive")

    if REQUIRE_EMAIL_VERIFIED and _has_col(User, "email_verified") and not bool(getattr(user, "email_verified", False)):
        # treat as unauthorized to avoid user enumeration
        raise _unauth("invalid_or_expired_token")

    # Attach passthrough claims/scopes for downstream use
    scopes: List[str] = []
    if "scopes" in claims and isinstance(claims["scopes"], (list, tuple)):
        scopes = [str(s).lower() for s in claims["scopes"] if s]
    elif "scope" in claims:
        scopes = [s.strip().lower() for s in str(claims["scope"]).split() if s.strip()]

    setattr(user, "_jwt_claims", claims)
    setattr(user, "_jwt_scopes", scopes)
    return user

# ---------------------------- Guards (roles & scopes) ----------------------------
def require_roles(*roles: str) -> Callable[[User], User]:
    want = {r.lower() for r in roles if r}
    def _dep(current_user: User = Depends(get_current_user)) -> User:
        role = (getattr(current_user, "role", None) or "").lower()
        if want and role not in want:
            raise _forbidden(f"requires_role_in_{sorted(want)}")
        return current_user
    return _dep

def require_scopes(*scopes: str) -> Callable[[User], User]:
    want = {s.lower() for s in scopes if s}
    def _dep(current_user: User = Depends(get_current_user)) -> User:
        have = {s.lower() for s in getattr(current_user, "_jwt_scopes", [])}
        if want and not want.issubset(have):
            raise _forbidden(f"requires_scopes_{sorted(want)}")
        return current_user
    return _dep

# ---------------------------- Example routers (optional) ----------------------------
owner_router = APIRouter(prefix="/owner", tags=["Owner"])

@owner_router.get(
    "/dashboard",
    summary="Owner dashboard (role=owner)",
    dependencies=[Depends(require_roles("owner"))],
)
def owner_dashboard() -> Dict[str, Any]:
    return {"status": "ok", "message": "Welcome, Owner!"}

secure_router = APIRouter(prefix="/secure", tags=["Secure"])

@secure_router.get(
    "/reports",
    summary="Reports (role in admin/owner AND scope=reports.read)",
    dependencies=[Depends(require_roles("admin", "owner")), Depends(require_scopes("reports.read"))],
)
def secure_reports() -> Dict[str, Any]:
    return {"ok": True, "data": "reports-list"}

__all__ = [
    "get_current_user",
    "require_roles",
    "require_scopes",
    "owner_router",
    "secure_router",
]
