# backend/routes/auth_routes.py
# -*- coding: utf-8 -*-
from __future__ import annotations

"""
SmartBiz Auth Router (production-ready, URL-aware) — no /api prefix here.

Main endpoints (all under /auth/*):
- POST /auth/login                 (email | username | phone; JSON au form)
- POST /auth/register              (JSON)
- POST /auth/register-form         (multipart/form-data)
- POST /auth/signup                (alias of register)
- GET  /auth/me                    (current user by token/cookie)
- POST /auth/logout                (clear cookie)
- POST /auth/token/refresh         (rotate token)
- POST /auth/change-password       (change current password)
- GET  /auth/session/verify        (boolean)
- GET  /auth/_meta                 (absolute URLs)
- GET  /auth/_diag                 (diagnostics)
- GET  /auth/_diag/columns/{table}
- GET  /auth/_diag/pwhash
- OPTIONS /auth/{path}             (CORS preflight 204)

Optional legacy router is exported to ease migration from /api/auth/*.
"""

import base64
import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from typing import Any, Dict, List, Optional, Tuple

import jwt
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status, Form
from pydantic import BaseModel, EmailStr, Field, validator
from sqlalchemy import func, or_, text
from sqlalchemy.exc import (
    DBAPIError,
    IntegrityError,
    OperationalError,
    ProgrammingError,
    SQLAlchemyError,
    StatementError,
)
from sqlalchemy.orm import Session, load_only, noload

# ──────────────── DB wiring (layout-safe) ────────────────
try:
    from db import get_db, engine  # type: ignore
except Exception:  # pragma: no cover
    from backend.db import get_db, engine  # type: ignore

try:
    from models.user import User  # type: ignore
except Exception:  # pragma: no cover
    from backend.models.user import User  # type: ignore

# ──────────────── Security helpers (hash/verify) ────────────────
try:
    from backend.utils.security import verify_password, get_password_hash  # type: ignore
except Exception:
    # Fallback kwa passlib
    from passlib.hash import bcrypt  # type: ignore
    def get_password_hash(pw: str) -> str: return bcrypt.hash(pw)
    def verify_password(pw: str, hashed: str) -> bool:
        try: return bcrypt.verify(pw, hashed)
        except Exception: return False

# ──────────────── JWT (PyJWT) ────────────────
SECRET_KEY = os.getenv("SECRET_KEY") or base64.urlsafe_b64encode(os.urandom(48)).decode()
JWT_ALG = os.getenv("JWT_ALG", "HS256")
ACCESS_MIN = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "60"))        # 1h
REFRESH_MIN = int(os.getenv("REFRESH_TOKEN_EXPIRE_MINUTES", "43200"))   # 30d

def _now() -> datetime: return datetime.now(timezone.utc)
def _ts(dt: datetime) -> int: return int(dt.timestamp())

def create_access_token(claims: dict, minutes: int = ACCESS_MIN) -> str:
    now = _now()
    payload = {"typ": "access", "iss": "smartbiz-api", "iat": _ts(now), "exp": _ts(now + timedelta(minutes=minutes)), **claims}
    if "sub" in payload: payload["sub"] = str(payload["sub"])
    return jwt.encode(payload, SECRET_KEY, algorithm=JWT_ALG)

def decode_token(token: str) -> Dict[str, Any]:
    return jwt.decode(token, SECRET_KEY, algorithms=[JWT_ALG])

def _extract_token(request: Request) -> Optional[str]:
    auth = request.headers.get("authorization") or request.headers.get("Authorization")
    if auth and auth.lower().startswith("bearer "):
        return auth.split(" ", 1)[1].strip()
    cookie_name = os.getenv("AUTH_COOKIE_NAME", "sb_access")
    return request.cookies.get(cookie_name)

def get_current_user_from_token(request: Request) -> Dict[str, Any]:
    token = _extract_token(request)
    if not token:
        raise HTTPException(status_code=401, detail="Missing token")
    try:
        return decode_token(token)
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

# ──────────────── Config / Flags ────────────────
logger = logging.getLogger("smartbiz.auth")
router = APIRouter(prefix="/auth", tags=["Auth"])
# Legacy router (utaweza kui-mount chini ya /api/auth/* ikiwa bado unahitaji)
legacy_router = APIRouter(tags=["AuthLegacy"])

def _flag(name: str, default: str = "true") -> bool:
    return (os.getenv(name, default) or "").strip().lower() in {"1", "true", "yes", "on", "y"}

USE_COOKIE_AUTH = _flag("USE_COOKIE_AUTH", "true")
COOKIE_NAME     = os.getenv("AUTH_COOKIE_NAME", "sb_access")
COOKIE_MAX_AGE  = int(os.getenv("AUTH_COOKIE_MAX_AGE", str(7 * 24 * 3600)))
COOKIE_PATH     = os.getenv("AUTH_COOKIE_PATH", "/")
COOKIE_SECURE   = _flag("AUTH_COOKIE_SECURE", "true")
COOKIE_SAMESITE = os.getenv("AUTH_COOKIE_SAMESITE", "none")
COOKIE_DOMAIN   = (os.getenv("AUTH_COOKIE_DOMAIN") or "").strip() or None

ALLOW_PHONE_LOGIN = _flag("AUTH_LOGIN_ALLOW_PHONE", "false")
ALLOW_REG = any([
    _flag("ALLOW_REGISTRATION", "true"),
    _flag("REGISTRATION_ENABLED", "true"),
    _flag("SIGNUP_ENABLED", "true"),
    _flag("SMARTBIZ_ALLOW_SIGNUP", "true"),
])

# simple in-memory rate limiter
LOGIN_RATE_MAX_PER_MIN = int(os.getenv("LOGIN_RATE_LIMIT_PER_MIN", "20"))
_RATE_WIN = 60.0
_LOGIN_BUCKET: Dict[str, List[float]] = {}
def _rate_ok(key: str) -> bool:
    now = time.time()
    bucket = _LOGIN_BUCKET.setdefault(key, [])
    while bucket and (now - bucket[0]) > _RATE_WIN:
        bucket.pop(0)
    if len(bucket) >= LOGIN_RATE_MAX_PER_MIN: return False
    bucket.append(now); return True

# ──────────────── URL helpers ────────────────
def _base_url(request: Request) -> str:
    env = (os.getenv("BACKEND_PUBLIC_URL") or "").strip()
    return (env.rstrip("/") if env else str(request.base_url).rstrip("/"))

def _url(request: Request, path: str) -> str:
    base = _base_url(request)
    path = path if path.startswith("/") else ("/" + path)
    return base + path

# ──────────────── Normalizers ────────────────
_phone_digits = re.compile(r"\D+")
def _norm(s: Optional[str]) -> str: return (s or "").strip()
def _norm_email(s: Optional[str]) -> str: return _norm(s).lower()
def _norm_username(s: Optional[str]) -> str: return " ".join(_norm(s).split()).lower()
def _norm_phone(s: Optional[str]) -> str:
    s = _norm(s)
    if not s: return ""
    return "+" + _phone_digits.sub("", s) if s.startswith("+") else _phone_digits.sub("", s)

# ──────────────── DB inspection helpers ────────────────
@lru_cache(maxsize=1)
def _users_columns() -> set[str]:
    from sqlalchemy import inspect as _inspect
    try:
        insp = _inspect(engine)
        return {c["name"] for c in insp.get_columns("users")}
    except Exception as e:
        logger.warning("Could not inspect users table: %s", e)
        return {"id", "email"}

def _col_exists(name: str) -> bool: return name in _users_columns()
def _is_mapped(name: str) -> bool: return hasattr(User, name)

def _model_col(model, name: str):
    try:
        if hasattr(model, name): return getattr(model, name)
        return model.__table__.c[name]  # type: ignore[attr-defined]
    except Exception:
        return None

def _first_existing_attr(candidates: List[str]):
    prefer = os.getenv("SMARTBIZ_PWHASH_COL")
    if prefer and _col_exists(prefer) and hasattr(User, prefer):
        return getattr(User, prefer), prefer
    for n in candidates:
        if _col_exists(n) and hasattr(User, n):
            return getattr(User, n), n
    return None, None

def _pwd_col_name() -> Optional[str]:
    prefer = os.getenv("SMARTBIZ_PWHASH_COL")
    if prefer and _col_exists(prefer): return prefer
    for n in ("hashed_password", "password_hash", "password", "pass_hash"):
        if _col_exists(n): return n
    return None

# ──────────────── Schemas ────────────────
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
    phone_number: Optional[str] = None

    @validator("username")
    def _v_username(cls, v: str) -> str:
        v = _norm_username(v)
        if not v: raise ValueError("username required")
        return v

class MeResponse(BaseModel):
    id: Any
    email: Optional[EmailStr] = None
    username: Optional[str] = None
    full_name: Optional[str] = None
    role: Optional[str] = None
    phone_number: Optional[str] = None
    class Config: orm_mode = True

class ChangePasswordInput(BaseModel):
    old_password: str = Field(..., min_length=1, max_length=256)
    new_password: str = Field(..., min_length=6, max_length=128)

# ──────────────── Small helpers ────────────────
def _safe_eq(col, value: str):
    try: return col == value
    except Exception: return func.lower(col) == value.lower()

def _user_summary_loaded(u: Any, names: set[str]) -> Dict[str, Any]:
    def val(n: str): return getattr(u, n) if n in names and hasattr(u, n) else None
    phone = None
    for n in ("phone_number", "phone", "mobile", "msisdn"):
        if n in names: phone = val(n); break
    username = None
    for n in ("username", "user_name", "handle"):
        if n in names: username = val(n); break
    out = {
        "id": val("id"),
        "email": val("email"),
        "username": username,
        "full_name": val("full_name"),
        "role": val("role"),
        "phone_number": phone,
    }
    if out.get("id") is not None: out["id"] = str(out["id"])
    return out

def _issue_tokens_for_user(user: Any) -> str:
    return create_access_token({"sub": str(getattr(user, "id", None)),
                                "email": getattr(user, "email", None)})

def _set_auth_cookie(response: Response, token: str) -> None:
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

def _clear_auth_cookie(response: Response) -> None:
    response.delete_cookie(key=COOKIE_NAME, path=COOKIE_PATH, domain=COOKIE_DOMAIN)

# ──────────────── RAW SQL fallbacks ────────────────
def _sql_fetch_user_by_ident(db: Session, ident: str) -> Optional[Dict[str, Any]]:
    cols = _users_columns()
    pwcol = _pwd_col_name()
    if not pwcol:
        raise HTTPException(status_code=500, detail="Password storage not configured")

    select_cols = ["id"]
    for cand in ("email","full_name","role","username","user_name","handle","phone_number","phone","mobile","msisdn",pwcol):
        if cand in cols: select_cols.append(cand)
    sel = ", ".join(f'"{c}"' for c in select_cols)

    where_parts, params = [], {}
    if "email" in cols:
        where_parts.append('LOWER("email") = LOWER(:email)'); params["email"] = ident
    for cand in ("username","user_name","handle"):
        if cand in cols:
            where_parts.append(f'LOWER("{cand}") = LOWER(:uname)'); params.setdefault("uname", ident); break
    for cand in ("phone_number","phone","mobile","msisdn"):
        if cand in cols:
            where_parts.append(f'"{cand}" = :phone'); params.setdefault("phone", _phone_digits.sub("", ident)); break
    if not where_parts:
        raise HTTPException(status_code=401, detail="Invalid credentials")

    sql = f'SELECT {sel} FROM "users" WHERE ' + " OR ".join(where_parts) + " LIMIT 1"
    row = db.execute(text(sql), params).mappings().first()
    return dict(row) if row else None

def _sql_insert_user(db: Session, data: RegisterInput) -> Dict[str, Any]:
    cols = _users_columns()
    pwcol = _pwd_col_name()
    if not pwcol:
        raise HTTPException(status_code=500, detail="Password storage not configured")

    fields: Dict[str, Any] = {}
    if "email" in cols: fields["email"] = _norm_email(data.email)
    if "full_name" in cols and data.full_name: fields["full_name"] = _norm(data.full_name)
    for cand in ("username","user_name","handle"):
        if cand in cols:
            fields[cand] = _norm_username(data.username); break
    if ALLOW_PHONE_LOGIN and data.phone_number:
        ph = _phone_digits.sub("", data.phone_number)
        for cand in ("phone_number","phone","mobile","msisdn"):
            if cand in cols:
                fields[cand] = ph; break
    if "is_active" in cols: fields.setdefault("is_active", True)
    if "is_verified" in cols: fields.setdefault("is_verified", False)
    if "subscription_status" in cols: fields.setdefault("subscription_status", "free")
    if "role" in cols: fields.setdefault("role", "user")
    fields[pwcol] = get_password_hash(data.password)

    cols_sql = ", ".join(f'"{k}"' for k in fields.keys())
    vals_sql = ", ".join(f":{k}" for k in fields.keys())
    sql = f'INSERT INTO "users" ({cols_sql}) VALUES ({vals_sql}) RETURNING id'
    new_id = db.execute(text(sql), fields).scalar()
    db.commit()

    out = {"id": new_id}
    for k in ("email","full_name","role","username","user_name","handle","phone_number","phone","mobile","msisdn"):
        if k in fields: out[k] = fields[k]
    return {"message": "Registration successful", "user": out}

# ──────────────── Login (ORM → SQL fallback) ────────────────
class _LoginPayload(BaseModel):
    identifier: str
    password: str

async def _parse_login(request: Request) -> _LoginPayload:
    ctype = (request.headers.get("content-type") or "").lower()
    if "application/json" in ctype:
        body = await request.json()
        data = LoginJSON(**(body or {}))
        ident = _norm(data.email or data.username or data.phone)
        pwd = _norm(data.password)
    else:
        form = await request.form()
        ident = _norm(form.get("email") or form.get("username") or form.get("phone"))
        pwd = _norm(form.get("password"))
        if not ident or not pwd:
            qp = request.query_params
            ident = _norm(qp.get("email") or qp.get("username") or qp.get("phone"))
            pwd = _norm(qp.get("password"))
    if not ident or not pwd:
        raise HTTPException(status_code=422, detail="missing_credentials")
    return _LoginPayload(identifier=ident, password=pwd)

def _rate_ok_or_429(identifier: str, ip: str):
    if not _rate_ok(f"{identifier}|{ip}"):
        raise HTTPException(status_code=429, detail="too_many_attempts")

async def _do_login(request: Request, db: Session, response: Response) -> LoginOutput:
    data = await _parse_login(request)
    _rate_ok_or_429(data.identifier, request.client.host if request.client else "0.0.0.0")

    cols = _users_columns()

    # ORM path
    try:
        conds: List[Any] = []
        email = _norm_email(data.identifier)
        uname = _norm_username(data.identifier)
        phone = _phone_digits.sub("", data.identifier)

        if email and "email" in cols:
            col = _model_col(User, "email")
            if col is not None: conds.append(_safe_eq(col, email))
        if uname:
            for cand in ("username","user_name","handle"):
                if cand in cols:
                    col = _model_col(User, cand)
                    if col is not None:
                        conds.append(_safe_eq(col, uname)); break
        if ALLOW_PHONE_LOGIN and phone:
            for cand in ("phone_number","phone","mobile","msisdn"):
                if cand in cols:
                    col = _model_col(User, cand)
                    if col is not None:
                        conds.append(col == phone); break

        if not conds:
            raise HTTPException(status_code=401, detail="Invalid credentials")

        pwd_attr, pwd_name = _first_existing_attr(["hashed_password", "password_hash", "password"])
        if not pwd_attr:
            raise HTTPException(status_code=500, detail="Password storage not configured")

        load_names: List[str] = ["id", pwd_name]
        for n in ("email","full_name","role","username","user_name","handle","phone_number","phone","mobile","msisdn","is_active"):
            if _col_exists(n) and _is_mapped(n): load_names.append(n)

        seen, only_names = set(), []
        for n in load_names:
            if n not in seen and _is_mapped(n):
                seen.add(n); only_names.append(n)
        only_attrs = [getattr(User, n) for n in only_names]

        user = db.query(User).options(noload("*"), load_only(*only_attrs)).filter(or_(*conds)).limit(1).first()
        if not user:
            try: verify_password("dummy", "x"*60)
            except Exception: pass
            raise HTTPException(status_code=401, detail="Invalid credentials")

        hashed_value = getattr(user, pwd_name, None)
        if not (hashed_value and verify_password(data.password, hashed_value)):
            raise HTTPException(status_code=401, detail="Invalid credentials")

        if hasattr(user, "is_active") and getattr(user, "is_active") is False:
            raise HTTPException(status_code=403, detail="Account disabled")

        token = _issue_tokens_for_user(user)
        _set_auth_cookie(response, token)
        return LoginOutput(access_token=token, user=_user_summary_loaded(user, set(only_names)))

    except (ProgrammingError, DBAPIError, OperationalError, SQLAlchemyError, StatementError) as e:
        logger.exception("ORM login failed; falling back to SQL: %s", e)

    # SQL fallback
    row = _sql_fetch_user_by_ident(db, data.identifier)
    if not row:
        raise HTTPException(status_code=401, detail="Invalid credentials")

    pwcol = _pwd_col_name()
    hashed_value = row.get(pwcol) if pwcol else None
    if not (hashed_value and verify_password(data.password, str(hashed_value))):
        raise HTTPException(status_code=401, detail="Invalid credentials")

    token = create_access_token({"sub": str(row.get("id")), "email": row.get("email")})
    _set_auth_cookie(response, token)
    if pwcol and pwcol in row: row.pop(pwcol, None)
    return LoginOutput(access_token=token, user=row)

# ──────────────── Register (ORM → SQL fallback) ────────────────
def _build_user_kwargs(data: RegisterInput, cols: set[str]) -> Dict[str, Any]:
    kw: Dict[str, Any] = {}
    if "email" in cols and _is_mapped("email"): kw["email"] = _norm_email(data.email)
    if "full_name" in cols and _is_mapped("full_name"): kw["full_name"] = (_norm(data.full_name) or None)
    for cand in ("username","user_name","handle"):
        if cand in cols and _is_mapped(cand) and data.username:
            kw[cand] = _norm_username(data.username); break
    if ALLOW_PHONE_LOGIN and data.phone_number:
        ph = _phone_digits.sub("", data.phone_number)
        for cand in ("phone_number","phone","mobile","msisdn"):
            if cand in cols and _is_mapped(cand):
                kw[cand] = ph; break
    if "is_active" in cols and _is_mapped("is_active"): kw.setdefault("is_active", True)
    if "is_verified" in cols and _is_mapped("is_verified"): kw.setdefault("is_verified", False)
    if "subscription_status" in cols and _is_mapped("subscription_status"): kw.setdefault("subscription_status", "free")
    if "role" in cols and _is_mapped("role"): kw.setdefault("role", "user")
    return kw

def _register_core(data: RegisterInput, db: Session) -> Dict[str, Any]:
    if not ALLOW_REG:
        raise HTTPException(status_code=403, detail="Registration disabled")

    cols = _users_columns()

    # unique checks
    uniq_conds: List[Any] = []
    if "email" in cols:
        col = _model_col(User, "email")
        if col is not None: uniq_conds.append(_safe_eq(col, _norm_email(data.email)))
    if data.username:
        for cand in ("username","user_name","handle"):
            if cand in cols:
                col = _model_col(User, cand)
                if col is not None:
                    uniq_conds.append(_safe_eq(col, _norm_username(data.username))); break
    if ALLOW_PHONE_LOGIN and data.phone_number:
        ph = _phone_digits.sub("", data.phone_number)
        for cand in ("phone_number","phone","mobile","msisdn"):
            if cand in cols:
                col = _model_col(User, cand)
                if col is not None:
                    uniq_conds.append(col == ph); break

    try:
        if uniq_conds:
            exists = db.query(User.id).options(noload("*")).filter(or_(*uniq_conds)).limit(1).first() is not None
            if exists:
                raise HTTPException(status_code=409, detail="User already exists")
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("register.unique_checks(ORM) failed: %s", e)

    # ORM create
    try:
        user_kwargs = _build_user_kwargs(data, cols)
        _, pwd_name = _first_existing_attr(["hashed_password", "password_hash", "password"])
        if not pwd_name:
            raise HTTPException(status_code=500, detail="Password storage not configured")
        user_kwargs[pwd_name] = get_password_hash(data.password)

        new_user = User(**user_kwargs)
        db.add(new_user); db.commit(); db.refresh(new_user)
        names_loaded = {n for n in user_kwargs.keys() if _is_mapped(n)} | {"id"}
        return {"message": "Registration successful", "user": _user_summary_loaded(new_user, names_loaded)}

    except (ProgrammingError, IntegrityError, DBAPIError, OperationalError, SQLAlchemyError, StatementError) as e:
        logger.exception("ORM register failed; falling back to SQL: %s", e)
        return _sql_insert_user(db, data)

# ──────────────── Routes ────────────────
@router.post("/login", response_model=LoginOutput, summary="Login (email/username/phone)")
async def login(request: Request, response: Response, db: Session = Depends(get_db)):
    return await _do_login(request, db, response)

@router.post("/register", status_code=status.HTTP_201_CREATED, summary="Register a new user")
def register(data: RegisterInput, db: Session = Depends(get_db)):
    return _register_core(data, db)

@router.post("/register-form", status_code=status.HTTP_201_CREATED, summary="Register via form-data")
def register_form(
    username: str = Form(...),
    email: EmailStr = Form(...),
    password: str = Form(...),
    full_name: Optional[str] = Form(None),
    phone_number: Optional[str] = Form(None),
    db: Session = Depends(get_db),
):
    payload = RegisterInput(username=username, email=email, password=password, full_name=full_name, phone_number=phone_number)
    return _register_core(payload, db)

@router.post("/signup", status_code=status.HTTP_201_CREATED, summary="Signup (alias of register)")
def signup(data: RegisterInput, db: Session = Depends(get_db)):
    return _register_core(data, db)

@router.get("/me", response_model=MeResponse, response_model_exclude_none=True, summary="Get current user")
def me(request: Request, db: Session = Depends(get_db)):
    claims = get_current_user_from_token(request)
    uid = claims.get("sub")
    u = None
    try:
        if hasattr(User, "id") and uid:
            u = db.query(User).options(noload("*")).filter(User.id == uid).first()
    except Exception:
        u = None
    if u:
        cols = _users_columns()
        fields = ("id","email","username","full_name","role","phone_number","phone","mobile","msisdn")
        loaded = {n for n in fields if n in cols and _is_mapped(n)}
        return _user_summary_loaded(u, loaded)
    return {"id": str(claims.get("sub") or ""), "email": claims.get("email") or None}

@router.get("/session/verify", summary="Verify current session")
def verify_session(request: Request):
    try:
        claims = get_current_user_from_token(request)
        return {"valid": True, "user": {"id": str(claims.get("sub") or ""), "email": claims.get("email")}}
    except HTTPException:
        return {"valid": False}

@router.post("/logout", status_code=204, summary="Logout (clear cookie)")
def logout(response: Response):
    _clear_auth_cookie(response)
    return Response(status_code=204)

@router.post("/token/refresh", summary="Rotate token")
def token_refresh(request: Request, response: Response):
    token = _extract_token(request)
    if not token:
        raise HTTPException(status_code=401, detail="Missing token")
    try:
        claims = decode_token(token)
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    sub = str(claims.get("sub") or "")
    if not sub:
        raise HTTPException(status_code=401, detail="Invalid token subject")
    new_token = create_access_token({"sub": sub, "email": claims.get("email")}, minutes=ACCESS_MIN)
    _set_auth_cookie(response, new_token)
    return {"access_token": new_token, "token_type": "bearer"}

@router.post("/change-password", status_code=204, summary="Change current account password")
def change_password(data: ChangePasswordInput, db: Session = Depends(get_db), request: Request = None):
    claims = get_current_user_from_token(request)
    uid = claims.get("sub")
    _, pwd_name = _first_existing_attr(["hashed_password","password_hash","password"])
    if not pwd_name:
        raise HTTPException(status_code=500, detail="Password storage not configured")

    only_attrs = []
    if hasattr(User, "id"): only_attrs.append(getattr(User, "id"))
    if hasattr(User, pwd_name): only_attrs.append(getattr(User, pwd_name))
    user = db.query(User).options(noload("*"), load_only(*only_attrs)).filter(User.id == uid).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    hashed = getattr(user, pwd_name, None)
    ok = bool(hashed and verify_password(data.old_password, hashed))
    if not ok:
        try: verify_password("dummy", str(hashed or "x"*60))
        except Exception: pass
        raise HTTPException(status_code=401, detail="Old password is incorrect")

    setattr(user, pwd_name, get_password_hash(data.new_password))
    db.add(user); db.commit()
    return Response(status_code=204)

# ──────────────── Meta/discovery (absolute URLs with /auth/*) ────────────────
@router.get("/_meta", summary="Auth endpoint discovery (absolute URLs)")
def auth_meta(request: Request):
    return {
        "base_url": _base_url(request),
        "endpoints": {
            "login":           _url(request, "/auth/login"),
            "register":        _url(request, "/auth/register"),
            "register_form":   _url(request, "/auth/register-form"),
            "signup":          _url(request, "/auth/signup"),
            "me":              _url(request, "/auth/me"),
            "verify":          _url(request, "/auth/session/verify"),
            "logout":          _url(request, "/auth/logout"),
            "token_refresh":   _url(request, "/auth/token/refresh"),
            "change_password": _url(request, "/auth/change-password"),
        }
    }

# ──────────────── Diagnostics ────────────────
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

@router.get("/_diag/pwhash", tags=["Auth"], summary="Which password column is used")
def diag_pwhash():
    return {"password_column": _pwd_col_name()}

# ──────────────── CORS preflight convenience ────────────────
@router.options("/{path:path}", include_in_schema=False)
def auth_preflight(path: str):  # noqa: ARG001
    return Response(status_code=204)

# ──────────────── Optional legacy: mount these under /api/auth/* if needed ───
# (Au tumia 308 redirect kwenye main.py kama tulivyoweka.)
@legacy_router.post("/login", include_in_schema=False)
async def legacy_login(request: Request, response: Response, db: Session = Depends(get_db)):
    return await _do_login(request, db, response)

@legacy_router.post("/register", include_in_schema=False)
def legacy_register(data: RegisterInput, db: Session = Depends(get_db)):
    return _register_core(data, db)

@legacy_router.post("/signup", include_in_schema=False)
def legacy_signup(data: RegisterInput, db: Session = Depends(get_db)):
    return _register_core(data, db)

__all__ = ["router", "legacy_router"]
