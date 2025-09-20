# backend/auth.py
from __future__ import annotations

import os, re, time, uuid, hmac, base64, hashlib, logging
from typing import Optional, Dict, Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response, Body, status
from pydantic import BaseModel, EmailStr, constr
from sqlalchemy import text
from sqlalchemy.orm import Session

# ────────────────────────── Log ──────────────────────────
logger = logging.getLogger("smartbiz.auth")

# ────────────────────────── DB glue ──────────────────────
try:
    # jaribu layout ya backend/ db.py
    from backend.db import SessionLocal, engine  # type: ignore
except Exception:  # pragma: no cover
    # fallback kwa root/db.py
    from db import SessionLocal, engine  # type: ignore

def get_db() -> Session:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# Jedwali & kolamu — zinachotolewa kwa mazingira ili tuwe “layout-safe”
USER_TABLE = os.getenv("USER_TABLE", "users")
PW_COL = os.getenv("SMARTBIZ_PWHASH_COL", "password_hash")

# Tumia introspection kama ipo ili kupata kolamu ya username iliyo sahihi
_USERNAME_CANDIDATES = ("username", "user_name", "handle")

def _users_columns() -> set[str]:
    try:
        from sqlalchemy import inspect as _sa_inspect
        insp = _sa_inspect(engine)
        return {c["name"] for c in insp.get_columns(USER_TABLE)}
    except Exception:
        return set()

def _username_col() -> str:
    cols = _users_columns()
    for c in _USERNAME_CANDIDATES:
        if c in cols:
            return c
    return "username"

# ────────────────────────── Token (HMAC) ─────────────────
SECRET = (os.getenv("AUTH_SECRET") or os.getenv("SECRET_KEY") or "dev-secret").encode("utf-8")
ACCESS_COOKIE = os.getenv("ACCESS_COOKIE", "sb_access")
ACCESS_TTL = int(os.getenv("ACCESS_TOKEN_TTL_SECONDS", "3600"))

COOKIE_SECURE = (os.getenv("AUTH_COOKIE_SECURE", "true").lower() in {"1", "true", "yes", "on"})
COOKIE_SAMESITE = os.getenv("AUTH_COOKIE_SAMESITE", "none")
COOKIE_PATH = os.getenv("AUTH_COOKIE_PATH", "/")
COOKIE_DOMAIN = (os.getenv("AUTH_COOKIE_DOMAIN") or "").strip() or None

def _sign(payload: str) -> str:
    sig = hmac.new(SECRET, payload.encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(sig).decode().rstrip("=")

def _mint_token(user_id: str, ttl: int = ACCESS_TTL) -> str:
    now = int(time.time())
    raw = f"{user_id}.{now}.{ttl}.{uuid.uuid4().hex}"
    return raw + "." + _sign(raw)

def _verify_token(token: str) -> tuple[bool, Optional[str]]:
    try:
        p, sig = token.rsplit(".", 1)
        if not hmac.compare_digest(_sign(p), sig):
            return False, None
        user_id, ts, ttl, _ = p.split(".", 3)
        if int(time.time()) > int(ts) + int(ttl):
            return False, None
        return True, user_id
    except Exception:
        return False, None

def _set_cookie(resp: Response, token: str, ttl: int = ACCESS_TTL) -> None:
    resp.set_cookie(
        key=ACCESS_COOKIE, value=token, max_age=ttl,
        secure=True if COOKIE_SAMESITE.lower()=="none" else COOKIE_SECURE,
        httponly=True, samesite=COOKIE_SAMESITE, path=COOKIE_PATH, domain=COOKIE_DOMAIN
    )

def _clear_cookie(resp: Response) -> None:
    resp.delete_cookie(key=ACCESS_COOKIE, path=COOKIE_PATH, domain=COOKIE_DOMAIN)

# ────────────────────────── Password hashing ─────────────
def _hash_password(pw: str) -> str:
    try:
        from passlib.hash import bcrypt  # type: ignore
        return bcrypt.using(rounds=12).hash(pw)
    except Exception:
        salt = os.urandom(16)
        dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, 120_000)
        return "pbkdf2$" + base64.b64encode(salt + dk).decode()

def _verify_password(pw: str, stored: str) -> bool:
    if stored.startswith("$2b$") or stored.startswith("$2a$"):
        try:
            from passlib.hash import bcrypt  # type: ignore
            return bcrypt.verify(pw, stored)
        except Exception:
            return False
    if stored.startswith("pbkdf2$"):
        try:
            blob = base64.b64decode(stored.split("$", 1)[1].encode())
            salt, dk = blob[:16], blob[16:]
            cand = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, 120_000)
            return hmac.compare_digest(dk, cand)
        except Exception:
            return False
    # plain fallback (dev only)
    return stored == pw

# ────────────────────────── Helpers (SQL) ────────────────
def _get_user_by_email(db: Session, email: str) -> Optional[dict]:
    row = db.execute(
        text(f'SELECT * FROM {USER_TABLE} WHERE LOWER(email)=LOWER(:e) LIMIT 1'),
        {"e": email}
    ).mappings().first()
    return dict(row) if row else None

def _get_user_by_username(db: Session, uname: str) -> Optional[dict]:
    ucol = _username_col()
    row = db.execute(
        text(f'SELECT * FROM {USER_TABLE} WHERE LOWER("{ucol}")=LOWER(:u) LIMIT 1'),
        {"u": uname}
    ).mappings().first()
    return dict(row) if row else None

def _get_user_by_id(db: Session, user_id: str) -> Optional[dict]:
    row = db.execute(
        text(f"SELECT * FROM {USER_TABLE} WHERE id::text=:i LIMIT 1"),
        {"i": user_id}
    ).mappings().first()
    return dict(row) if row else None

def _create_user(db: Session, email: str, username: str, password: str) -> dict:
    ucol = _username_col()
    hpw = _hash_password(password)
    sql = text(
        f'INSERT INTO {USER_TABLE} (email, "{ucol}", {PW_COL}) '
        'VALUES (:email, :username, :hpw) '
        f'RETURNING id, email, "{ucol}" AS username'
    )
    try:
        row = db.execute(sql, {"email": email, "username": username, "hpw": hpw}).mappings().first()
        db.commit()
        return dict(row) if row else {"email": email, "username": username}
    except Exception as e:
        db.rollback()
        msg = str(e).lower()
        if "unique" in msg or "duplicate" in msg or "23505" in msg:
            raise HTTPException(status_code=409, detail="Email or username already registered")
        logger.exception("create_user failed")
        raise HTTPException(status_code=500, detail="Failed to create user")

# ────────────────────────── Input models (Pydantic v2) ──
class SignupIn(BaseModel):
    full_name: Optional[str] = None
    email: EmailStr
    username: constr(min_length=3, pattern=r"^[a-z0-9_]+$")
    password: constr(min_length=8)
    language: Optional[str] = "en"

class LoginIn(BaseModel):
    identifier: str   # email or username
    password: constr(min_length=1)

# ────────────────────────── Output models ───────────────
class MeOut(BaseModel):
    id: str
    email: EmailStr
    username: Optional[str] = None

class LoginOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: MeOut

# ────────────────────────── Business rules ──────────────
def _strong(pw: str) -> bool:
    return (
        len(pw) >= 8
        and re.search(r"[A-Z]", pw) is not None
        and re.search(r"[a-z]", pw) is not None
        and re.search(r"\d", pw) is not None
        and re.search(r"[^\w\s]", pw) is not None
    )

def _norm_email(e: str) -> str:
    return e.strip().lower()

def _norm_uname(u: str) -> str:
    return u.strip().lower()

# Rate limit rahisi (per IP) – kupunguza brute force
_BUCKET: Dict[str, list[float]] = {}
def _throttle(ip: str, limit: int = 8, window_sec: int = 60) -> None:
    now = time.time()
    lst = _BUCKET.setdefault(ip, [])
    # safisha ya zamani
    _BUCKET[ip] = [t for t in lst if now - t < window_sec]
    if len(_BUCKET[ip]) >= limit:
        raise HTTPException(status_code=429, detail="Too many requests. Please wait a moment.")
    _BUCKET[ip].append(now)

# ────────────────────────── Router ──────────────────────
router = APIRouter(prefix="/auth", tags=["Auth"])

@router.post("/register", response_model=MeOut, status_code=201)
def register(payload: SignupIn = Body(...), db: Session = Depends(get_db), request: Request = None):
    ip = request.client.host if request and request.client else "?"
    _throttle(ip, limit=5, window_sec=60)

    email = _norm_email(payload.email)
    username = _norm_uname(payload.username)

    if not re.fullmatch(r"^[a-z0-9_]{3,}$", username):
        raise HTTPException(status_code=422, detail="Use lowercase letters, numbers and underscores only")

    if not _strong(payload.password):
        raise HTTPException(status_code=422, detail="Weak password: include upper, lower, number & symbol (8+ chars)")

    if _get_user_by_email(db, email):
        raise HTTPException(status_code=409, detail="Email already registered")
    if _get_user_by_username(db, username):
        raise HTTPException(status_code=409, detail="Username already taken")

    user = _create_user(db, email=email, username=username, password=payload.password)
    return MeOut(id=str(user.get("id", "")), email=user["email"], username=user["username"])

@router.post("/login", response_model=LoginOut)
def login(payload: LoginIn = Body(...), db: Session = Depends(get_db), request: Request = None, response: Response = None):
    ip = request.client.host if request and request.client else "?"
    _throttle(ip, limit=8, window_sec=60)

    ident = payload.identifier.strip().lower()
    user = _get_user_by_email(db, ident) or _get_user_by_username(db, ident)
    if not user or not _verify_password(payload.password, user.get(PW_COL) or user.get("hashed_password") or ""):
        raise HTTPException(status_code=401, detail="Invalid credentials")

    token = _mint_token(str(user["id"]))
    body = LoginOut(
        access_token=token,
        user=MeOut(id=str(user["id"]), email=user["email"], username=user.get(_username_col()))
    )
    resp = Response(content=body.model_dump_json(), media_type="application/json")
    _set_cookie(resp, token)
    return resp

@router.get("/me", response_model=MeOut)
def me(request: Request, db: Session = Depends(get_db)):
    token = request.cookies.get(ACCESS_COOKIE) or ""
    ok, uid = _verify_token(token)
    if not ok or not uid:
        raise HTTPException(status_code=401, detail="Unauthorized")
    u = _get_user_by_id(db, uid)
    if not u:
        raise HTTPException(status_code=401, detail="Unauthorized")
    return MeOut(id=str(u["id"]), email=u["email"], username=u.get(_username_col()))

@router.post("/logout")
def logout():
    resp = Response(content='{"ok":true}', media_type="application/json")
    _clear_cookie(resp)
    return resp
