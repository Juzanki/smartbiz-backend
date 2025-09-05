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
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import func, or_
from sqlalchemy.exc import DBAPIError, OperationalError, SQLAlchemyError
from sqlalchemy.orm import Session, load_only, noload

from backend.db import get_db, engine
from backend.models.user import User

# ───────────────── Security helpers (verify/hash) & token ─────────────────
try:
    from backend.utils.security import verify_password, get_password_hash  # type: ignore
except Exception:  # dev fallback
    import hashlib, hmac
    def get_password_hash(pw: str) -> str:
        return hashlib.sha256(pw.encode("utf-8")).hexdigest()
    def verify_password(pw: str, hashed: str) -> bool:
        return hmac.compare_digest(get_password_hash(pw), hashed)

try:
    from backend.auth import create_access_token, get_current_user  # type: ignore
except Exception:  # dev fallback
    def create_access_token(data: dict, minutes: int = 60 * 24) -> str:
        payload = data.copy()
        payload["exp"] = int(time.time()) + minutes * 60
        return base64.urlsafe_b64encode(_json.dumps(payload).encode()).decode()
    def get_current_user():
        raise RuntimeError("get_current_user not wired")

logger = logging.getLogger("smartbiz.auth")

router = APIRouter(prefix="/auth", tags=["Auth"])
legacy_router = APIRouter(tags=["Auth"])

# ───────────────────────── Config ─────────────────────────
ALLOW_REG = os.getenv("ALLOW_REGISTRATION", "true").strip().lower() in {"1","true","yes","on","y"}
ALLOW_PHONE_LOGIN = os.getenv("AUTH_LOGIN_ALLOW_PHONE", "true").strip().lower() in {"1","true","yes","on","y"}
LOGIN_RATE_MAX_PER_MIN = int(os.getenv("LOGIN_RATE_LIMIT_PER_MIN", "20"))
LOGIN_MAINTENANCE = os.getenv("AUTH_LOGIN_MAINTENANCE", "0").strip().lower() in {"1","true","yes","on","y"}

USE_COOKIE_AUTH = os.getenv("USE_COOKIE_AUTH", "true").strip().lower() in {"1","true","yes","on","y"}
COOKIE_NAME = os.getenv("AUTH_COOKIE_NAME", "sb_access")
COOKIE_MAX_AGE = int(os.getenv("AUTH_COOKIE_MAX_AGE", str(7 * 24 * 3600)))
COOKIE_PATH = os.getenv("AUTH_COOKIE_PATH", "/")
COOKIE_SECURE = os.getenv("AUTH_COOKIE_SECURE", "true").strip().lower() in {"1","true","yes","on","y"}
COOKIE_SAMESITE = os.getenv("AUTH_COOKIE_SAMESITE", "none")
COOKIE_DOMAIN = os.getenv("AUTH_COOKIE_DOMAIN", "").strip() or None

# ───────────────────────── Rate limit ─────────────────────
_RATE_WIN = 60.0
_LOGIN_BUCKET: Dict[str, List[float]] = {}
def _rate_ok(key: str) -> bool:
    now = time.time()
    q = _LOGIN_BUCKET.setdefault(key, [])
    while q and (now - q[0]) > _RATE_WIN:
        q.pop(0)
    if len(q) >= LOGIN_RATE_MAX_PER_MIN:
        return False
    q.append(now)
    return True

# ───────────────────────── Normalizers ────────────────────
_phone_digits = re.compile(r"\D+")
def _norm(s: Optional[str]) -> str: return (s or "").strip()
def _norm_email(s: Optional[str]) -> str: return _norm(s).lower()
def _norm_username(s: Optional[str]) -> str: return _norm(s).lower()
def _norm_phone(s: Optional[str]) -> str:
    s = _norm(s)
    if not s: return ""
    return "+" + _phone_digits.sub("", s) if s.startswith("+") else _phone_digits.sub("", s)

# ───────────────────── DB column helpers ──────────────────
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

def _mapped_attr(model, name: str):
    try:
        if hasattr(model, name):
            return getattr(model, name)
    except Exception:
        pass
    return None

def _model_col(model, name: str):
    """
    Prefer mapped attribute; else fallback to raw table column (model.__table__.c[name]).
    Fallback husaidia kujenga filters hata kama ORM attribute haipo.
    """
    attr = _mapped_attr(model, name)
    if attr is not None:
        return attr
    try:
        return model.__table__.c[name]
    except Exception:
        return None

def _first_existing_attr(candidates: List[str]) -> Tuple[Any | None, str | None]:
    """Chagua jina la kolamu ya password (lazima iwe mapped attribute)."""
    for n in candidates:
        if _col_exists(n) and hasattr(User, n):
            return getattr(User, n), n
    return None, None

# ───────────────────────── Schemas ────────────────────────
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
    phone_number: Optional[str] = None
    full_name: str = Field(..., min_length=2, max_length=100)
    password: str = Field(..., min_length=6, max_length=128)

class MeResponse(BaseModel):
    id: int
    email: EmailStr
    username: Optional[str] = None
    full_name: Optional[str] = None
    role: Optional[str] = None
    phone_number: Optional[str] = None

# ───────────────────── User summaries ─────────────────────
def _user_summary_loaded(u: User, loaded_names: set[str]) -> Dict[str, Any]:
    def val(n: str): return getattr(u, n) if n in loaded_names and hasattr(u, n) else None
    phone = next((val(n) for n in ("phone_number","phone","mobile","msisdn") if n in loaded_names), None)
    username = next((val(n) for n in ("username","user_name","handle") if n in loaded_names), None)
    return {
        "id": val("id"),
        "email": val("email"),
        "username": username,
        "full_name": val("full_name"),
        "role": val("role"),
        "phone_number": phone,
    }

# ───────────────────── Login core ─────────────────────────
async def _do_login(request: Request, db: Session, response: Response) -> LoginOutput:
    if LOGIN_MAINTENANCE:
        raise HTTPException(status_code=503, detail="Login temporarily unavailable")

    # Parse payload (JSON au form)
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
        # kidogo maelekezo kwa client
        raise HTTPException(status_code=429, detail="too_many_attempts")

    cols = _users_columns()

    # Utafutaji: jaribu email → username → phone
    conds: List[Any] = []
    email = _norm_email(ident_raw)
    uname = _norm_username(ident_raw)
    phone = _norm_phone(ident_raw)

    if email and "email" in cols:
        col = _model_col(User, "email")
        if col is not None:
            conds.append(func.lower(col) == email)

    if uname:
        for cand in ("username", "user_name", "handle"):
            if cand in cols:
                col = _model_col(User, cand)
                if col is not None:
                    conds.append(func.lower(col) == uname)
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

    # Password column halisi (mapped)
    pwd_attr, pwd_name = _first_existing_attr(["hashed_password", "password_hash", "password"])
    if not pwd_attr:
        logger.error("No password column mapped on User among expected names.")
        raise HTTPException(status_code=500, detail="Password storage not configured")

    # Columns za kupakia: mapped tu
    load_names: List[str] = ["id", pwd_name]
    for n in ("email", "full_name", "role", "language", "subscription_status",
              "username", "user_name", "handle", "phone_number", "phone", "mobile", "msisdn", "is_active"):
        if _col_exists(n) and hasattr(User, n):
            load_names.append(n)

    # dedupe
    seen, only_names = set(), []
    for n in load_names:
        if n not in seen:
            seen.add(n)
            only_names.append(n)
    only_attrs = [getattr(User, n) for n in only_names if hasattr(User, n)]

    try:
        # 🔒 kata relationships zote ili kuepuka SELECT zisizohitajika (chanzo cha Database error)
        q = (
            db.query(User)
              .options(noload('*'), load_only(*only_attrs))
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
        try: verify_password("dummy", "$2b$12$S3JtM3fE9pZ4oE2e7I5tQe3Cz7M6Ykz8tZc0V0c8w2o8JH7m6J7zS")
        except Exception: pass
        raise HTTPException(status_code=401, detail="Invalid credentials")

    # Hakiki nenosiri
    hashed_value = getattr(user, pwd_name, None)
    try:
        ok = bool(hashed_value and verify_password(password, hashed_value))
    except Exception as e:
        logger.exception("login.password_verify failed: %s", e)
        raise HTTPException(status_code=500, detail="Password verification error")

    if not ok:
        raise HTTPException(status_code=401, detail="Invalid credentials")

    if hasattr(user, "is_active") and getattr(user, "is_active") is False:
        raise HTTPException(status_code=403, detail="Account disabled")

    # Token
    try:
        token = create_access_token({"sub": str(user.id), "email": getattr(user, "email", None)})
    except Exception as e:
        logger.exception("login.token_create failed: %s", e)
        raise HTTPException(status_code=500, detail="Could not create token")

    # Cookie (hiari)
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

# ───────────────────────── Routes ────────────────────────
@router.post("/login", response_model=LoginOutput, summary="Login (email/username/phone)")
async def login(request: Request, response: Response, db: Session = Depends(get_db)):
    return await _do_login(request, db, response)

@legacy_router.post("/login-form", response_model=LoginOutput, summary="Legacy form login")
async def login_form_legacy(request: Request, response: Response, db: Session = Depends(get_db)):
    return await _do_login(request, db, response)

@router.post("/register", status_code=status.HTTP_201_CREATED, summary="Register a new user")
def register(data: RegisterInput, db: Session = Depends(get_db)):
    if not ALLOW_REG:
        raise HTTPException(status_code=403, detail="Registration disabled")

    cols = _users_columns()

    # Unique checks
    uniq_conds: List[Any] = []
    if "email" in cols:
        uniq_conds.append(func.lower(_model_col(User, "email")) == _norm_email(data.email))  # type: ignore
    if "username" in cols and data.username:
        col = _model_col(User, "username")
        if col is not None:
            uniq_conds.append(func.lower(col) == _norm_username(data.username))  # type: ignore
    if ALLOW_PHONE_LOGIN and data.phone_number:
        ph = _norm_phone(data.phone_number)
        for cand in ("phone_number","phone","mobile","msisdn"):
            if cand in cols:
                col = _model_col(User, cand)
                if col is not None:
                    uniq_conds.append(col == ph)
                    break

    try:
        exists = db.query(User).filter(or_(*uniq_conds)).first() if uniq_conds else None
    except Exception as e:
        logger.exception("register.unique_checks failed: %s", e)
        raise HTTPException(status_code=500, detail="Registration unavailable")

    if exists:
        raise HTTPException(status_code=409, detail="User already exists")

    # Tengeneza user mpya (kwa kolamu halisi pekee)
    user_kwargs: Dict[str, Any] = {"email": _norm_email(data.email)}
    if "full_name" in cols: user_kwargs["full_name"] = _norm(data.full_name)
    if "username" in cols: user_kwargs["username"] = _norm_username(data.username)
    if ALLOW_PHONE_LOGIN and data.phone_number:
        ph = _norm_phone(data.phone_number)
        for cand in ("phone_number","phone","mobile","msisdn"):
            if cand in cols:
                user_kwargs[cand] = ph
                break
    if "is_active" in cols: user_kwargs["is_active"] = True
    if "is_verified" in cols: user_kwargs["is_verified"] = True
    if "subscription_status" in cols: user_kwargs["subscription_status"] = "free"

    pwd_attr, pwd_name = _first_existing_attr(["hashed_password","password_hash","password"])
    if not pwd_attr:
        raise HTTPException(status_code=500, detail="Password storage not configured")
    user_kwargs[pwd_name] = get_password_hash(data.password)

    try:
        new_user = User(**user_kwargs)
        db.add(new_user); db.commit(); db.refresh(new_user)
    except Exception as e:
        db.rollback()
        logger.exception("register.create failed: %s", e)
        raise HTTPException(status_code=500, detail="Registration failed")

    names_loaded = set(user_kwargs.keys()) | {"id"}
    return {"message": "Registration successful", "user": _user_summary_loaded(new_user, names_loaded)}

@router.get("/me", response_model=MeResponse, summary="Get current user")
def me(current_user: User = Depends(get_current_user)):
    cols = _users_columns()
    loaded = {n for n in ("id","email","username","full_name","role","phone_number","phone","mobile","msisdn") if n in cols}
    return _user_summary_loaded(current_user, loaded)

@router.get("/session/verify", summary="Verify current session")
def verify_session(current_user: User = Depends(get_current_user)):
    return {"valid": True, "user": {"id": current_user.id, "email": getattr(current_user, "email", None)}}

# Diag endpoint kusaidia kuona columns halisi
@router.get("/_diag", tags=["Auth"], summary="Auth diagnostics")
def auth_diag():
    return {"users_columns": sorted(list(_users_columns()))}
