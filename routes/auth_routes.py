# backend/routes/auth_routes.py  
# -*- coding: utf-8 -*-  
from __future__ import annotations  

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

# ──────────────── Normalizers ────────────────  
_phone_digits = re.compile(r"\D+")  
def _norm(s: Optional[str]) -> str: return (s or "").strip()  
def _norm_email(s: Optional[str]) -> str: return _norm(s).lower()  
def _norm_username(s: Optional[str]) -> str: return " ".join(_norm(s).split()).lower()  
def _norm_phone(s: Optional[str]) -> str:  
    s = _norm(s)  
    if not s: return ""  
    return "+" + _phone_digits.sub("", s) if s.startswith("+") else _phone_digits.sub("", s)  

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
