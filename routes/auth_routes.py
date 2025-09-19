# backend/routes/auth_routes.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import base64
import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import jwt
from fastapi import (
    APIRouter, Depends, HTTPException, Request, Response, status, Form
)
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import func, or_
from sqlalchemy.exc import IntegrityError, SQLAlchemyError, DBAPIError, OperationalError, ProgrammingError, StatementError
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
    from passlib.hash import bcrypt  # type: ignore

    def get_password_hash(pw: str) -> str:
        return bcrypt.hash(pw)

    def verify_password(pw: str, hashed: str) -> bool:
        try:
            return bcrypt.verify(pw, hashed)
        except Exception:
            return False

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
    if "sub" in payload:
        payload["sub"] = str(payload["sub"])
    return jwt.encode(payload, SECRET_KEY, algorithm=JWT_ALG)

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
COOKIE_SAMESITE = os.getenv("AUTH_COOKIE_SAMESITE", "none")  # "lax" for same-site apps
COOKIE_DOMAIN   = (os.getenv("AUTH_COOKIE_DOMAIN") or "").strip() or None

ALLOW_PHONE_LOGIN = _flag("AUTH_LOGIN_ALLOW_PHONE", "false")
ALLOW_REG = any([
    _flag("ALLOW_REGISTRATION", "true"),
    _flag("REGISTRATION_ENABLED", "true"),
    _flag("SIGNUP_ENABLED", "true"),
    _flag("SMARTBIZ_ALLOW_SIGNUP", "true"),
])

# ──────────────── Rate limiter (login + signup) ────────────────
LOGIN_RATE_MAX_PER_MIN = int(os.getenv("LOGIN_RATE_LIMIT_PER_MIN", "20"))
SIGNUP_RATE_MAX_PER_MIN = int(os.getenv("SIGNUP_RATE_LIMIT_PER_MIN", "10"))
_RATE_WIN = 60.0
_LOGIN_BUCKET: Dict[str, List[float]] = {}
_SIGNUP_BUCKET: Dict[str, List[float]] = {}

def _bucket_ok(bucket: Dict[str, List[float]], key: str, max_per_min: int) -> bool:
    now = time.time()
    arr = bucket.setdefault(key, [])
    while arr and (now - arr[0]) > _RATE_WIN:
        arr.pop(0)
    if len(arr) >= max_per_min:
        return False
    arr.append(now)
    return True

def _rate_ok_or_429_login(identifier: str, ip: str) -> None:
    key = f"login::{identifier}::{ip}"
    if not _bucket_ok(_LOGIN_BUCKET, key, LOGIN_RATE_MAX_PER_MIN):
        raise HTTPException(status_code=429, detail="Too many login attempts")

def _rate_ok_or_429_signup(identifier: str, ip: str) -> None:
    key = f"signup::{identifier}::{ip}"
    if not _bucket_ok(_SIGNUP_BUCKET, key, SIGNUP_RATE_MAX_PER_MIN):
        raise HTTPException(status_code=429, detail="Too many signup attempts")

# ──────────────── Normalizers ────────────────
_phone_digits = re.compile(r"\D+")

def _norm(s: Optional[str]) -> str:
    return (s or "").strip()

def _norm_email(s: Optional[str]) -> str:
    return _norm(s).lower()

def _norm_username(s: Optional[str]) -> str:
    # keep case as-is for display, but do duplicate checks case-insensitively
    return " ".join(_norm(s).split())

def _norm_cc(s: Optional[str]) -> str:
    s = _norm(s)
    return s.lstrip("+") if s else ""

def _norm_local(s: Optional[str]) -> str:
    s = _norm(s)
    return s.lstrip("0") if s else ""

def _e164(cc: str, local: str) -> str:
    if not cc or not local:
        return ""
    return f"+{cc}{local}"

# ──────────────── Cookie helpers ────────────────
def _set_auth_cookie(resp: Response, token: str) -> None:
    if not USE_COOKIE_AUTH:
        return
    resp.set_cookie(
        key=COOKIE_NAME,
        value=token,
        max_age=COOKIE_MAX_AGE,
        path=COOKIE_PATH,
        domain=COOKIE_DOMAIN,
        secure=COOKIE_SECURE,
        httponly=True,
        samesite=COOKIE_SAMESITE,  # "none" for cross-site (Netlify↔Render)
    )

# ──────────────── Public “whoami” (optional) ────────────────
@router.get("/whoami")
def whoami(request: Request) -> Dict[str, Any]:
    token = request.headers.get("authorization") or request.headers.get("Authorization")
    cookie = request.cookies.get(COOKIE_NAME)
    return {"has_auth_header": bool(token), "has_auth_cookie": bool(cookie)}

# ──────────────── Login Schemas ────────────────
class LoginJSON(BaseModel):
    email: Optional[EmailStr] = None
    username: Optional[str] = None
    phone: Optional[str] = None
    password: str = Field(..., min_length=1, max_length=256)

class LoginOutput(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: Dict[str, Any]

# ──────────────── Login (JSON or form) ────────────────
@router.post("/login", response_model=LoginOutput)
async def login(request: Request, response: Response, db: Session = Depends(get_db)) -> LoginOutput:
    # Parse
    ctype = (_norm(request.headers.get("content-type"))).lower()
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
        raise HTTPException(status_code=422, detail="missing_credentials")

    _rate_ok_or_429_login(ident, request.client.host if request.client else "0.0.0.0")

    # Build conditions
    cols = {c.key for c in User.__table__.columns}  # type: ignore
    conds: List[Any] = []

    if "email" in cols:
        conds.append(func.lower(User.email) == _norm_email(ident))

    for cand in ("username", "user_name", "handle"):
        if cand in cols:
            conds.append(func.lower(getattr(User, cand)) == _norm(ident).lower())
            break

    if ALLOW_PHONE_LOGIN:
        phone_digits = _phone_digits.sub("", ident)
        for cand in ("phone_e164", "msisdn", "phone", "phone_number"):
            if cand in cols:
                if cand == "phone_e164":
                    conds.append(getattr(User, cand) == ("+" + phone_digits if phone_digits and not phone_digits.startswith("+") else phone_digits))
                else:
                    conds.append(getattr(User, cand) == phone_digits)
                break

    if not conds:
        raise HTTPException(status_code=401, detail="Invalid credentials")

    # Choose password column
    pwd_name = None
    for name in ("password_hash", "hashed_password", "password"):
        if name in cols:
            pwd_name = name
            break
    if not pwd_name:
        raise HTTPException(status_code=500, detail="Password storage not configured")

    # Query minimal fields
    load_names = ["id", pwd_name]
    for n in ("email", "full_name", "role", "username", "user_name", "handle", "phone_e164", "phone_number", "is_active", "preferred_language"):
        if n in cols:
            load_names.append(n)

    only_attrs = [getattr(User, n) for n in dict.fromkeys(load_names).keys()]  # unique order-preserving
    try:
        user = (
            db.query(User)
            .options(noload("*"), load_only(*only_attrs))  # type: ignore
            .filter(or_(*conds))
            .limit(1)
            .first()
        )
    except (ProgrammingError, DBAPIError, OperationalError, SQLAlchemyError, StatementError) as e:
        logger.exception("Login query failed: %s", e)
        raise HTTPException(status_code=500, detail="login_failed")

    if not user:
        try: verify_password("dummy", "x" * 60)
        except Exception: pass
        raise HTTPException(status_code=401, detail="Invalid credentials")

    hashed_value = getattr(user, pwd_name, None)
    if not (hashed_value and verify_password(pwd, hashed_value)):
        raise HTTPException(status_code=401, detail="Invalid credentials")

    if hasattr(user, "is_active") and getattr(user, "is_active") is False:
        raise HTTPException(status_code=403, detail="Account disabled")

    token = create_access_token({"sub": getattr(user, "id"), "email": getattr(user, "email", None)})
    _set_auth_cookie(response, token)

    # Build user summary
    out: Dict[str, Any] = {"id": getattr(user, "id")}
    for n in ("email", "full_name", "role", "preferred_language"):
        if n in cols:
            out[n] = getattr(user, n, None)
    uname = None
    for cand in ("username", "user_name", "handle"):
        if cand in cols:
            uname = getattr(user, cand, None)
            break
    if uname is not None:
        out["username"] = uname

    return LoginOutput(access_token=token, user=out)

# ──────────────── Signup / Register (JSON or form) ────────────────
class RegisterIn(BaseModel):
    email: EmailStr
    username: str = Field(min_length=3, max_length=50)
    password: str = Field(min_length=8, max_length=256)
    phone_country_code: Optional[str] = None
    phone_number: Optional[str] = None
    preferred_language: Optional[str] = "en"
    agree_terms: bool = True
    full_name: Optional[str] = None  # optional

class RegisterOut(BaseModel):
    id: int
    email: EmailStr
    username: str

def _parse_signup_payload(
    request: Request,
    body_json: Optional[dict] = None,
    form: Optional[dict] = None,
) -> RegisterIn:
    if body_json is not None:
        return RegisterIn(**body_json)
    assert form is not None
    return RegisterIn(
        email=form.get("email"),
        username=form.get("username"),
        password=form.get("password"),
        phone_country_code=form.get("phone_country_code"),
        phone_number=form.get("phone_number"),
        preferred_language=form.get("preferred_language") or "en",
        agree_terms=(str(form.get("agree_terms")).lower() in {"1", "true", "yes", "on"}),
        full_name=form.get("full_name"),
    )

def _safe_user_set(u: User, name: str, value: Any) -> None:
    if hasattr(u, name):
        setattr(u, name, value)

def _has_col(name: str) -> bool:
    return name in {c.key for c in User.__table__.columns}  # type: ignore

def _diag_dup_checks(db: Session, email: str, username: str, phone_e164: str) -> Tuple[bool, bool, bool]:
    email_taken = bool(db.query(User.id).filter(func.lower(User.email) == email).first()) if _has_col("email") else False
    uname_col = "username" if _has_col("username") else ("user_name" if _has_col("user_name") else ("handle" if _has_col("handle") else None))
    username_taken = bool(db.query(User.id).filter(func.lower(getattr(User, uname_col)) == username.lower()).first()) if uname_col else False
    phone_taken = False
    if phone_e164:
        if _has_col("phone_e164"):
            phone_taken = bool(db.query(User.id).filter(User.phone_e164 == phone_e164).first())
        else:
            # fallback to pair
            if _has_col("phone_country_code") and _has_col("phone_number"):
                cc = phone_e164[1:][0:3]  # rough (won't be used if you also store pair)
                # safer: check by provided pair instead of deriving from e164
                pass
    return email_taken, username_taken, phone_taken

def _signup_core(data: RegisterIn, db: Session) -> Tuple[User, str]:
    if not ALLOW_REG:
        raise HTTPException(status_code=403, detail="registration_disabled")

    # Normalize
    email = _norm_email(data.email)
    username = _norm_username(data.username)
    cc = _norm_cc(data.phone_country_code or "")
    local = _norm_local(data.phone_number or "")
    phone_e = _e164(cc, local) if (cc and local) else ""

    if not data.agree_terms:
        raise HTTPException(status_code=422, detail="terms_not_accepted")

    # Duplicate pre-checks
    if _has_col("email") and db.query(User.id).filter(func.lower(User.email) == email).first():
        raise HTTPException(status_code=409, detail="email_taken")

    uname_field = None
    for cand in ("username", "user_name", "handle"):
        if _has_col(cand):
            uname_field = cand
            if db.query(User.id).filter(func.lower(getattr(User, cand)) == username.lower()).first():
                raise HTTPException(status_code=409, detail="username_taken")
            break

    if phone_e:
        if _has_col("phone_e164"):
            if db.query(User.id).filter(User.phone_e164 == phone_e).first():
                raise HTTPException(status_code=409, detail="phone_taken")
        elif _has_col("phone_country_code") and _has_col("phone_number"):
            if db.query(User.id).filter(
                getattr(User, "phone_country_code") == cc,
                getattr(User, "phone_number") == local
            ).first():
                raise HTTPException(status_code=409, detail="phone_taken")

    # Build user
    user = User()
    _safe_user_set(user, "email", email)
    _safe_user_set(user, "password_hash", get_password_hash(data.password))
    if not hasattr(user, "password_hash") and hasattr(user, "hashed_password"):
        _safe_user_set(user, "hashed_password", get_password_hash(data.password))

    if uname_field:
        _safe_user_set(user, uname_field, username)
    _safe_user_set(user, "full_name", data.full_name)
    _safe_user_set(user, "preferred_language", data.preferred_language or "en")
    if cc: _safe_user_set(user, "phone_country_code", cc)
    if local: _safe_user_set(user, "phone_number", local)
    if phone_e and _has_col("phone_e164"):
        _safe_user_set(user, "phone_e164", phone_e)
    _safe_user_set(user, "is_active", True)
    if _has_col("agreed_terms_at"):
        _safe_user_set(user, "agreed_terms_at", func.now())

    db.add(user)
    try:
        db.commit()
        db.refresh(user)
    except IntegrityError as e:
        db.rollback()
        msg = str(getattr(e.orig, "diag", None) or e).lower()
        # Precise mapping
        if "email" in msg:
            raise HTTPException(status_code=409, detail="email_taken")
        if "user" in msg or "name" in msg or "handle" in msg:
            raise HTTPException(status_code=409, detail="username_taken")
        if "phone" in msg or "msisdn" in msg:
            raise HTTPException(status_code=409, detail="phone_taken")
        raise HTTPException(status_code=500, detail="register_failed")

    token = create_access_token({"sub": getattr(user, "id"), "email": email})
    return user, token

def _register_response(user: User, token: str, response: Response) -> RegisterOut:
    _set_auth_cookie(response, token)
    # Figure username field
    uname = None
    for cand in ("username", "user_name", "handle"):
        if hasattr(user, cand):
            uname = getattr(user, cand, None)
            if uname: break
    return RegisterOut(id=getattr(user, "id"), email=getattr(user, "email"), username=uname or "")

# Unified handler used by both /register and /signup
@router.post("/register", response_model=RegisterOut, status_code=201)
@router.post("/signup",   response_model=RegisterOut, status_code=201)
async def signup_or_register(request: Request, response: Response, db: Session = Depends(get_db)) -> RegisterOut:
    # Rate limit
    ip = request.client.host if request.client else "0.0.0.0"

    ctype = (_norm(request.headers.get("content-type"))).lower()
    try:
        if "application/json" in ctype:
            body = await request.json()
            data = _parse_signup_payload(request, body_json=body)
            _rate_ok_or_429_signup(_norm_email(data.email), ip)
            user, token = _signup_core(data, db)
            return _register_response(user, token, response)
        else:
            form = await request.form()
            data = _parse_signup_payload(request, form=form)
            _rate_ok_or_429_signup(_norm_email(data.email), ip)
            user, token = _signup_core(data, db)
            return _register_response(user, token, response)
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("signup/register failed: %s", e)
        raise HTTPException(status_code=500, detail="register_failed")

# ──────────────── Diagnostics: check availability ────────────────
@router.get("/_diag/availability")
def diag_availability(
    email: Optional[str] = None,
    username: Optional[str] = None,
    phone_country_code: Optional[str] = None,
    phone_number: Optional[str] = None,
    db: Session = Depends(get_db),
):
    cols = {c.key for c in User.__table__.columns}  # type: ignore
    out: Dict[str, Any] = {}
    if email and "email" in cols:
        out["email_taken"] = bool(db.query(User.id).filter(func.lower(User.email) == _norm_email(email)).first())
    if username:
        for cand in ("username", "user_name", "handle"):
            if cand in cols:
                out["username_taken"] = bool(db.query(User.id).filter(func.lower(getattr(User, cand)) == _norm(username).lower()).first())
                break
    if phone_country_code and phone_number:
        cc = _norm_cc(phone_country_code)
        local = _norm_local(phone_number)
        e = _e164(cc, local) if (cc and local) else ""
        if e and "phone_e164" in cols:
            out["phone_taken"] = bool(db.query(User.id).filter(getattr(User, "phone_e164") == e).first())
        elif "phone_country_code" in cols and "phone_number" in cols:
            out["phone_taken"] = bool(db.query(User.id).filter(
                getattr(User, "phone_country_code") == cc,
                getattr(User, "phone_number") == local
            ).first())
    return out
