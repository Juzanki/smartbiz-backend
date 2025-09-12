# backend/routes/auth_routes.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import base64
import json as _json
import logging
import os
import re
import time
from functools import lru_cache
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, EmailStr, Field, validator
from sqlalchemy import func, or_, text
from sqlalchemy.exc import (
    DBAPIError,
    OperationalError,
    SQLAlchemyError,
    IntegrityError,
    ProgrammingError,
)
from sqlalchemy.orm import Session, load_only, noload

# ── DB & Models (layout-safe imports)
try:
    from db import get_db, engine  # type: ignore
except Exception:
    from backend.db import get_db, engine  # type: ignore

try:
    from models.user import User  # type: ignore
except Exception:
    from backend.models.user import User  # type: ignore

# ── Security helpers & token
try:
    from backend.utils.security import verify_password, get_password_hash  # type: ignore
except Exception:  # dev fallback (simple sha256)
    import hashlib, hmac

    def get_password_hash(pw: str) -> str:
        return hashlib.sha256(pw.encode("utf-8")).hexdigest()

    def verify_password(pw: str, hashed: str) -> bool:
        return hmac.compare_digest(get_password_hash(pw), hashed)

try:
    from backend.auth import create_access_token, get_current_user  # type: ignore
except Exception:  # dev fallback (unsigned base64 "token")
    def create_access_token(data: dict, minutes: int = 60 * 24) -> str:
        payload = data.copy()
        payload["exp"] = int(time.time()) + minutes * 60
        return base64.urlsafe_b64encode(_json.dumps(payload).encode()).decode()

    def get_current_user():
        raise RuntimeError("get_current_user not wired")

logger = logging.getLogger("smartbiz.auth")

router = APIRouter(prefix="/auth", tags=["Auth"])
legacy_router = APIRouter(tags=["Auth"])  # e.g. /login-form

# ── Config / Flags
def _flag(name: str, default: str = "true") -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "on", "y"}

ALLOW_REG = (
    _flag("ALLOW_REGISTRATION", "true")
    or _flag("REGISTRATION_ENABLED", "true")
    or _flag("SIGNUP_ENABLED", "true")
    or _flag("SMARTBIZ_ALLOW_SIGNUP", "true")
)

ALLOW_PHONE_LOGIN = _flag("AUTH_LOGIN_ALLOW_PHONE", "false")
LOGIN_RATE_MAX_PER_MIN = int(os.getenv("LOGIN_RATE_LIMIT_PER_MIN", "20"))
LOGIN_MAINTENANCE = _flag("AUTH_LOGIN_MAINTENANCE", "0")

USE_COOKIE_AUTH = _flag("USE_COOKIE_AUTH", "true")
COOKIE_NAME = os.getenv("AUTH_COOKIE_NAME", "sb_access")
COOKIE_MAX_AGE = int(os.getenv("AUTH_COOKIE_MAX_AGE", str(7 * 24 * 3600)))
COOKIE_PATH = os.getenv("AUTH_COOKIE_PATH", "/")
COOKIE_SECURE = _flag("AUTH_COOKIE_SECURE", "true")
COOKIE_SAMESITE = os.getenv("AUTH_COOKIE_SAMESITE", "none")
COOKIE_DOMAIN = os.getenv("AUTH_COOKIE_DOMAIN", "").strip() or None

# ── Rate limit (per identifier+IP)
_RATE_WIN = 60.0
_LOGIN_BUCKET: Dict[str, List[float]] = {}

def _rate_ok(key: string) -> bool:  # type: ignore[name-defined]
    now = time.time()
    q = _LOGIN_BUCKET.setdefault(key, [])
    while q and (now - q[0]) > _RATE_WIN:
        q.pop(0)
    if len(q) >= LOGIN_RATE_MAX_PER_MIN:
        return False
    q.append(now)
    return True

# ── Normalizers
_phone_digits = re.compile(r"\D+")

def _norm(s: Optional[str]) -> str:
    return (s or "").strip()

def _norm_email(s: Optional[str]) -> str:
    return _norm(s).lower()

def _norm_username(s: Optional[str]) -> str:
    return " ".join(_norm(s).split()).lower()

def _norm_phone(s: Optional[str]) -> str:
    s = _norm(s)
    if not s:
        return ""
    return "+" + _phone_digits.sub("", s) if s.startswith("+") else _phone_digits.sub("", s)

# ── DB column & model helpers
@lru_cache(maxsize=1)
def _users_columns() -> set[str]:
    from sqlalchemy import inspect as _inspect
    try:
        insp = _inspect(engine)
        cols = {c["name"] for c in insp.get_columns("users")}
        logger.info("auth._users_columns = %s", sorted(cols))
        return cols
    except Exception as e:
        logger.warning("Could not inspect users table: %s", e)
        return {"id", "email"}

def _col_exists(name: str) -> bool:
    return name in _users_columns()

def _is_mapped(name: str) -> bool:
    return hasattr(User, name)

def _mapped_attr(model, name: str):
    try:
        if hasattr(model, name):
            return getattr(model, name)
    except Exception:
        pass
    return None

def _model_col(model, name: str):
    """Prefer mapped attribute; else fallback to raw table column."""
    attr = _mapped_attr(model, name)
    if attr is not None:
        return attr
    try:
        return model.__table__.c[name]  # type: ignore[attr-defined]
    except Exception:
        return None

def _first_existing_attr(candidates: List[str]) -> Tuple[Optional[Any], Optional[str]]:
    """Pick the password column actually mapped on the model."""
    for n in candidates:
        if _col_exists(n) and hasattr(User, n):
            return getattr(User, n), n
    return None, None

# ── Schemas
class LoginJSON(BaseModel):
    email: Optional[EmailStr] = None
    username: Optional[str] = None
    phone: Optional[str] = None
    password: str = Field(..., min_length=1, max_length=256)

class LoginOutput(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: Dict[str, Any]

class RegisterInput(BaseModel):
    username: str = Field(..., min_length=3, max_length=50)
    email: EmailStr
    full_name: Optional[str] = Field(None, max_length=120)
    password: str = Field(..., min_length=6, max_length=128)
    phone_number: Optional[str] = None  # itaheshimiwa tu kama column ipo

    @validator("username")
    def _u_norm(cls, v: str) -> str:
        v = _norm_username(v)
        if not v:
            raise ValueError("username required")
        return v

class MeResponse(BaseModel):
    id: int
    email: EmailStr
    username: Optional[str] = None
    full_name: Optional[str] = None
    role: Optional[str] = None
    phone_number: Optional[str] = None

# ── Small helpers
def _safe_eq(col, value: str):
    """citext/text-safe equality fallback."""
    try:
        return col == value
    except Exception:
        return func.lower(col) == value.lower()

# ── User summary helper (hakutap lazy relationships)
def _user_summary_loaded(u: User, loaded_names: set[str]) -> Dict[str, Any]:
    def val(n: str):
        return getattr(u, n) if n in loaded_names and hasattr(u, n) else None
    phone = None
    for n in ("phone_number", "phone", "mobile", "msisdn"):
        if n in loaded_names:
            phone = val(n)
            if phone:
                break
    username = None
    for n in ("username", "user_name", "handle"):
        if n in loaded_names:
            username = val(n)
            if username:
                break
    return {
        "id": val("id"),
        "email": val("email"),
        "username": username,
        "full_name": val("full_name"),
        "role": val("role"),
        "phone_number": phone,
    }

# ── Login core (safe, no relationships)
async def _do_login(request: Request, db: Session, response: Response) -> LoginOutput:
    if LOGIN_MAINTENANCE:
        raise HTTPException(status_code=503, detail="Login temporarily unavailable")

    # Parse JSON or form payload
    try:
        ctype = (request.headers.get("content-type") or "").lower()
        if "application/json" in ctype:
            body = await request.json()
            data = LoginJSON(**(body or {}))
            ident_raw = _norm(data.email or data.username or data.phone)
            password = _norm(data.password)
        else:
            form = await request.form()
            ident_raw = _norm(form.get("email") or form.get("username") or form.get("phone"))
            password = _norm(form.get("password"))
            if not ident_raw or not password:
                qp = request.query_params
                ident_raw = _norm(qp.get("email") or qp.get("username") or qp.get("phone"))
                password = _norm(qp.get("password"))
    except Exception:
        raise HTTPException(status_code=400, detail="invalid_login_payload")

    if not ident_raw or not password:
        raise HTTPException(status_code=400, detail="missing_credentials")

    # Rate limit
    ip = request.client.host if request.client else "0.0.0.0"
    if not _rate_ok(f"{ident_raw}|{ip}"):
        raise HTTPException(status_code=429, detail="too_many_attempts")

    cols = _users_columns()

    # Build OR conditions: email → username → phone
    conds: List[Any] = []
    email = _norm_email(ident_raw)
    uname = _norm_username(ident_raw)
    phone = _norm_phone(ident_raw)

    if email and "email" in cols:
        col = _model_col(User, "email")
        if col is not None:
            conds.append(_safe_eq(col, email))

    if uname:
        for cand in ("username", "user_name", "handle"):
            if cand in cols:
                col = _model_col(User, cand)
                if col is not None:
                    conds.append(_safe_eq(col, uname))
                    break

    if ALLOW_PHONE_LOGIN and phone:
        for cand in ("phone_number", "phone", "mobile", "msisdn"):
            if cand in cols:
                col = _model_col(User, cand)
                if col is not None:
                    conds.append(col == phone)
                    break

    if not conds:
        raise HTTPException(status_code=401, detail="Invalid credentials")

    # Determine password column actually mapped
    pwd_attr, pwd_name = _first_existing_attr(["hashed_password", "password_hash", "password"])
    if not pwd_attr:
        logger.error("No password column mapped on User among expected names.")
        raise HTTPException(status_code=500, detail="Password storage not configured")

    # Only load mapped scalar columns (no relationships)
    load_names: List[str] = ["id", pwd_name]
    for n in (
        "email", "full_name", "role", "language", "subscription_status",
        "username", "user_name", "handle",
        "phone_number", "phone", "mobile", "msisdn", "is_active",
    ):
        if _col_exists(n) and _is_mapped(n):
            load_names.append(n)

    seen, only_names = set(), []
    for n in load_names:
        if n not in seen and _is_mapped(n):
            seen.add(n)
            only_names.append(n)
    only_attrs = [getattr(User, n) for n in only_names]

    try:
        q = (
            db.query(User)
              .options(noload("*"), load_only(*only_attrs))
              .filter(or_(*conds))
              .limit(1)
        )
        user: Optional[User] = q.first()
    except Exception as e:
        logger.exception("login.query failed: %s", e)
        if isinstance(e, (OperationalError, DBAPIError, SQLAlchemyError)):
            raise HTTPException(status_code=500, detail="Database error")
        raise HTTPException(status_code=500, detail="Login service error")

    if not user:
        # constant-time burn
        try:
            verify_password("dummy", "$2b$12$S3JtM3fE9pZ4oE2e7I5tQe3Cz7M6Ykz8tZc0V0c8w2o8JH7m6J7zS")
        except Exception:
            pass
        raise HTTPException(status_code=401, detail="Invalid credentials")

    try:
        hashed_value = getattr(user, pwd_name, None)
        ok = bool(hashed_value and verify_password(password, hashed_value))
    except Exception as e:
        logger.exception("login.password_verify failed: %s", e)
        raise HTTPException(status_code=500, detail="Password verification error")

    if not ok:
        raise HTTPException(status_code=401, detail="Invalid credentials")

    if _is_mapped("is_active") and getattr(user, "is_active", True) is False:
        raise HTTPException(status_code=403, detail="Account disabled")

    try:
        token = create_access_token({"sub": str(user.id), "email": getattr(user, "email", None)})
    except Exception as e:
        logger.exception("login.token_create failed: %s", e)
        raise HTTPException(status_code=500, detail="Could not create token")

    if USE_COOKIE_AUTH:
        response.set_cookie(
            key=COOKIE_NAME,
            value=token,
            max_age=COOKIE_MAX_AGE,
            expires=COOKIE_MAX_AGE,
            path=COOKIE_PATH,
            domain=COOKIE_DOMAIN,
            secure=COOKIE_SECURE,
            httponly=True,
            samesite=COOKIE_SAMESITE,
        )

    return LoginOutput(access_token=token, user=_user_summary_loaded(user, set(only_names)))

# ── Register helpers
def _build_user_kwargs(data: RegisterInput, cols: set[str]) -> Dict[str, Any]:
    """ Tengeneza kwargs zinazolingana na *mapped* attributes pekee. """
    kw: Dict[str, Any] = {}

    if "email" in cols and _is_mapped("email"):
        kw["email"] = _norm_email(data.email)

    if "full_name" in cols and _is_mapped("full_name"):
        kw["full_name"] = (_norm(data.full_name) or None)

    for cand in ("username", "user_name", "handle"):
        if cand in cols and _is_mapped(cand) and data.username:
            kw[cand] = _norm_username(data.username)
            break

    if ALLOW_PHONE_LOGIN and data.phone_number:
        ph = _norm_phone(data.phone_number)
        for cand in ("phone_number", "phone", "mobile", "msisdn"):
            if cand in cols and _is_mapped(cand):
                kw[cand] = ph
                break

    # defaults – weka tu kama zime-map-iwa
    if "is_active" in cols and _is_mapped("is_active"):
        kw.setdefault("is_active", True)
    if "is_verified" in cols and _is_mapped("is_verified"):
        kw.setdefault("is_verified", False)
    if "subscription_status" in cols and _is_mapped("subscription_status"):
        kw.setdefault("subscription_status", "free")
    if "role" in cols and _is_mapped("role"):
        kw.setdefault("role", "user")

    return kw

def _register_core(data: RegisterInput, db: Session) -> Dict[str, Any]:
    if not ALLOW_REG:
        raise HTTPException(status_code=403, detail="Registration disabled")

    cols = _users_columns()

    # Unique checks — query ID only (never loads relationships)
    uniq_conds: List[Any] = []
    if "email" in cols:
        col = _model_col(User, "email")
        if col is not None:
            uniq_conds.append(_safe_eq(col, _norm_email(data.email)))

    if data.username:
        for cand in ("username", "user_name", "handle"):
            if cand in cols:
                col = _model_col(User, cand)
                if col is not None:
                    uniq_conds.append(_safe_eq(col, _norm_username(data.username)))
                    break

    if ALLOW_PHONE_LOGIN and data.phone_number:
        ph = _norm_phone(data.phone_number)
        for cand in ("phone_number", "phone", "mobile", "msisdn"):
            if cand in cols:
                col = _model_col(User, cand)
                if col is not None:
                    uniq_conds.append(col == ph)
                    break

    try:
        exists = False
        if uniq_conds:
            exists = db.query(User.id).options(noload("*")).filter(or_(*uniq_conds)).limit(1).first() is not None
        if exists:
            raise HTTPException(status_code=409, detail="User already exists")
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("register.unique_checks failed: %s", e)
        raise HTTPException(status_code=500, detail="Registration unavailable")

    # Build user fields mapped-only
    user_kwargs = _build_user_kwargs(data, cols)

    # Choose real password column (must be mapped)
    _, pwd_name = _first_existing_attr(["hashed_password", "password_hash", "password"])
    if not pwd_name:
        raise HTTPException(status_code=500, detail="Password storage not configured")
    user_kwargs[pwd_name] = get_password_hash(data.password)

    try:
        new_user = User(**user_kwargs)
        db.add(new_user)
        db.commit()
        db.refresh(new_user)  # safe — summary avoids relationships
    except IntegrityError as e:
        db.rollback()
        msg = str(getattr(e, "orig", e)).lower()
        if "unique" in msg and "email" in msg:
            raise HTTPException(status_code=409, detail="Email already registered")
        if "unique" in msg and "username" in msg:
            raise HTTPException(status_code=409, detail="Username already taken")
        if "not-null" in msg:
            raise HTTPException(status_code=422, detail="Missing required field for users table")
        logger.exception("register.integrity error: %s", e)
        raise HTTPException(status_code=400, detail="Invalid user data")
    except ProgrammingError as e:
        db.rollback()
        logger.exception("register.programming error: %s", e)
        raise HTTPException(
            status_code=500,
            detail="Registration failed (DB columns mismatch with ORM model).",
        )
    except Exception as e:
        db.rollback()
        logger.exception("register.create failed: %s", e)
        raise HTTPException(status_code=500, detail="Registration failed")

    names_loaded = {n for n in user_kwargs.keys() if _is_mapped(n)} | {"id"}
    return {"message": "Registration successful", "user": _user_summary_loaded(new_user, names_loaded)}

# ── Routes
@router.post("/login", response_model=LoginOutput, summary="Login (email/username/phone)")
async def login(request: Request, response: Response, db: Session = Depends(get_db)):
    return await _do_login(request, db, response)

@legacy_router.post("/login-form", response_model=LoginOutput, summary="Legacy form login")
async def login_form_legacy(request: Request, response: Response, db: Session = Depends(get_db)):
    return await _do_login(request, db, response)

@router.post("/register", status_code=status.HTTP_201_CREATED, summary="Register a new user")
def register(data: RegisterInput, db: Session = Depends(get_db)):
    return _register_core(data, db)

@router.post("/signup", status_code=status.HTTP_201_CREATED, summary="Signup (alias of register)")
def signup(data: RegisterInput, db: Session = Depends(get_db)):
    return _register_core(data, db)

@router.get("/me", response_model=MeResponse, summary="Get current user")
def me(current_user: User = Depends(get_current_user)):
    cols = _users_columns()
    fields = ("id", "email", "username", "full_name", "role", "phone_number", "phone", "mobile", "msisdn")
    loaded = {n for n in fields if _col_exists(n) and _is_mapped(n)}
    return _user_summary_loaded(current_user, loaded)

@router.get("/session/verify", summary="Verify current session")
def verify_session(current_user: User = Depends(get_current_user)):
    return {"valid": True, "user": {"id": current_user.id, "email": getattr(current_user, "email", None)}}

@router.get("/_diag", tags=["Auth"], summary="Auth diagnostics")
def auth_diag():
    return {"users_columns": sorted(list(_users_columns()))}

@router.get("/_diag/columns/{table}", tags=["Auth"], include_in_schema=False)
def diag_columns(table: str, db: Session = Depends(get_db)):
    rows = db.execute(text("""
      SELECT column_name, data_type, is_nullable
      FROM information_schema.columns
      WHERE table_name = :t
      ORDER BY ordinal_position
    """), {"t": table}).mappings().all()
    return {"table": table, "columns": list(rows)}

__all__ = ["router", "legacy_router"]
