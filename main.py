# backend/main.py
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import re
import time
import uuid
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

import anyio
from fastapi import (
    Body,
    Depends,
    FastAPI,
    Form,
    HTTPException,
    Request,
    Response,
    status,
)
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.openapi.docs import (
    get_swagger_ui_html,
    get_swagger_ui_oauth2_redirect_html,
)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, EmailStr, constr
from sqlalchemy import text
from sqlalchemy.orm import Session
from starlette.datastructures import MutableHeaders
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.requests import ClientDisconnect
from starlette.responses import JSONResponse

# ────────────────────────── Env helpers ──────────────────────────

def env_bool(key: str, default: bool = False) -> bool:
    v = os.getenv(key)
    return default if v is None else v.strip().lower() in {"1", "true", "yes", "y", "on"}

def env_list(key: str) -> List[str]:
    raw = os.getenv(key, "")
    return [x.strip() for x in raw.split(",") if x and x.strip()]

def _uniq(items: Iterable[Optional[str]]) -> List[str]:
    out, seen = [], set()
    for x in items:
        if x and x not in seen:
            out.append(x)
            seen.add(x)
    return out

def _sanitize_db_url(url: str) -> str:
    if not url:
        return ""
    return re.sub(r"://([^:@/]+):([^@/]+)@", r"://\1:****@", url)

# ────────────────────────── Paths & base env ──────────────────────────

BACKEND_DIR = Path(__file__).resolve().parent
ROOT_DIR = BACKEND_DIR.parent
UPLOADS_DIR = Path(os.getenv("UPLOADS_DIR", BACKEND_DIR / "uploads")).resolve()
UPLOADS_DIR.mkdir(parents=True, exist_ok=True)

ENVIRONMENT = (os.getenv("ENVIRONMENT") or os.getenv("ENV") or "production").lower()

# DB wiring (support both relative and package import layouts)
try:
    from db import Base, SessionLocal, engine  # type: ignore
except Exception:
    from backend.db import Base, SessionLocal, engine  # type: ignore

# ────────────────────────── Logging ──────────────────────────

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
LOG_JSON = env_bool("LOG_JSON", False)

class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)

_handler = logging.StreamHandler()
_handler.setFormatter(_JsonFormatter() if LOG_JSON else logging.Formatter("%(asctime)s | %(levelname)-7s | %(name)s | %(message)s"))
root_logger = logging.getLogger()
root_logger.handlers = [_handler]
root_logger.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))
logger = logging.getLogger("smartbiz.main")

# ────────────────────────── Optional DB column autodetect ──────────────────────────
_USERS_COLS: Optional[set[str]] = None
PW_COL = os.getenv("SMARTBIZ_PWHASH_COL", "password_hash")
USER_TABLE = os.getenv("USER_TABLE", "users")

with suppress(Exception):
    from sqlalchemy import inspect as _sa_inspect
    insp = _sa_inspect(engine)
    _USERS_COLS = {c["name"] for c in insp.get_columns(USER_TABLE)}
    chosen = (
        "hashed_password"
        if "hashed_password" in _USERS_COLS
        else ("password_hash" if "password_hash" in _USERS_COLS else PW_COL)
    )
    os.environ.setdefault("SMARTBIZ_PWHASH_COL", chosen)
    PW_COL = chosen
    logger.info("PW column = %s (users cols: %s)", chosen, sorted(_USERS_COLS))

def _users_columns() -> set[str]:
    global _USERS_COLS
    if _USERS_COLS is not None:
        return _USERS_COLS
    with suppress(Exception):
        from sqlalchemy import inspect as _sa_inspect
        insp = _sa_inspect(engine)
        _USERS_COLS = {c["name"] for c in insp.get_columns(USER_TABLE)}
    return _USERS_COLS or set()

# ────────────────────────── Middlewares (security + req id) ──────────────────────────

class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        try:
            response: Response = await call_next(request)
        except (ClientDisconnect, anyio.EndOfStream):
            return Response(status_code=499)
        except Exception:
            logger.exception("security-mw")
            return JSONResponse(status_code=500, content={"detail": "Internal server error"})
        # Basic hardening (+ allow Swagger CDN)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault("Permissions-Policy", "geolocation=(), microphone=(), camera=()")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self' https: data: blob:;"
            "img-src 'self' https: data:;"
            "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net;"
            "script-src 'self' 'unsafe-inline' 'unsafe-eval' https://cdn.jsdelivr.net;"
            "connect-src 'self' https: wss:;"
            "frame-ancestors 'self'; base-uri 'self';",
        )
        return response

class RequestIDMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        rid = request.headers.get("x-request-id") or uuid.uuid4().hex
        request.state.request_id = rid
        start = time.perf_counter()
        try:
            response: Response = await call_next(request)
        except (ClientDisconnect, anyio.EndOfStream):
            response = Response(status_code=499)
        except Exception:
            logger.exception("reqid-mw")
            response = JSONResponse(status_code=500, content={"detail": "Internal Server Error"})
        try:
            response.headers["x-request-id"] = rid
            response.headers["x-process-time-ms"] = str(int((time.perf_counter() - start) * 1000))
        except Exception:
            pass
        return response

# ────────────────────────── Cookie policy (SameSite=None; Secure; HttpOnly) ──────────────────────────

FORCE_SAMESITE_NONE = env_bool("COOKIE_SAMESITE_NONE", True)
FORCE_HTTPONLY = env_bool("COOKIE_HTTPONLY_DEFAULT", True)

class CookiePolicyMiddleware(BaseHTTPMiddleware):
    """Normalize Set-Cookie for cross-site SPA use."""
    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        response: Response = await call_next(request)
        if not (FORCE_SAMESITE_NONE or FORCE_HTTPONLY):
            return response

        raw = list(response.raw_headers)
        cookie_values: List[str] = []
        mutated = False
        for name, value in raw:
            if name.lower() == b"set-cookie":
                s = value.decode("latin-1")
                if FORCE_SAMESITE_NONE:
                    if "samesite=" in s.lower():
                        s = re.sub(r"(?i)SameSite=\w+", "SameSite=None", s)
                    else:
                        s += "; SameSite=None"
                    if "secure" not in s.lower():
                        s += "; Secure"
                if FORCE_HTTPONLY and "httponly" not in s.lower():
                    s += "; HttpOnly"
                cookie_values.append(s)
                mutated = True

        if mutated:
            headers = MutableHeaders(response.headers)
            try:
                del headers["set-cookie"]
            except KeyError:
                pass
            for cv in cookie_values:
                headers.append("set-cookie", cv)
        return response

# ────────────────────────── DB ping ──────────────────────────

def _db_ping() -> bool:
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False

# ────────────────────────── Lifespan ──────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info(
        "SmartBiz starting (env=%s, db=%s)",
        ENVIRONMENT,
        _sanitize_db_url(os.getenv("DATABASE_URL", "")),
    )
    if not _db_ping():
        logger.error("Database connection failed at startup!")
    else:
        logger.info("Database connection successful")

    if ENVIRONMENT != "production" and env_bool("AUTO_CREATE_TABLES", True):
        with suppress(Exception):
            Base.metadata.create_all(bind=engine, checkfirst=True)
            logger.info("DB tables verified/created")

    try:
        yield
    finally:
        logger.info("SmartBiz shutting down")

# ────────────────────────── FastAPI app ──────────────────────────

_docs_enabled = env_bool("ENABLE_DOCS", ENVIRONMENT != "production")
app = FastAPI(
    title=os.getenv("APP_NAME", "SmartBiz Assistance API"),
    description="SmartBiz Assistance Backend (Render + Netlify)",
    version=os.getenv("APP_VERSION", "1.0.0"),
    docs_url=None,                         # tutatumia custom /docs hapa chini
    redoc_url="/redoc" if _docs_enabled else None,
    openapi_url="/openapi.json" if _docs_enabled else None,
    lifespan=lifespan,
)

if _docs_enabled:
    @app.get("/docs", include_in_schema=False)
    def custom_swagger_ui():
        return get_swagger_ui_html(
            openapi_url=app.openapi_url,
            title="SmartBiz API Docs",
            swagger_favicon_url="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/favicon-32x32.png",
            swagger_js_url="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui-bundle.js",
            swagger_css_url="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui.css",
        )

    @app.get("/docs/oauth2-redirect", include_in_schema=False)
    def swagger_ui_redirect():
        return get_swagger_ui_oauth2_redirect_html()

# ───────────── CORS (must be BEFORE other middleware & routers) ─────────────

def _resolve_cors_origins() -> List[str]:
    hardcoded = [
        "https://smartbizsite.netlify.app",
        "https://smartbiz.site",
        "https://www.smartbiz.site",
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:4173",
        "http://127.0.0.1:4173",
    ]
    env_origins = env_list("CORS_ORIGINS") + env_list("ALLOWED_ORIGINS")
    extra = [os.getenv(k) for k in ("FRONTEND_URL", "WEB_URL", "NETLIFY_PUBLIC_URL", "RENDER_PUBLIC_URL")]
    return _uniq([*env_origins, *[x for x in extra if x], *hardcoded])

ALLOW_ORIGINS = _resolve_cors_origins()
CORS_ALLOW_ALL = env_bool("CORS_ALLOW_ALL", False)

if CORS_ALLOW_ALL:
    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=".*",
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["set-cookie"],
        max_age=600,
    )
    logger.warning("CORS_ALLOW_ALL=1 (dev/diagnostic mode) — DO NOT use in production.")
else:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=ALLOW_ORIGINS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["set-cookie"],
        max_age=600,
    )
    logger.info("CORS allow_origins: %s", ALLOW_ORIGINS)

class EnsureCorsCredentialsMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        response: Response = await call_next(request)
        origin = request.headers.get("origin")
        if not origin:
            return response
        if CORS_ALLOW_ALL or origin in ALLOW_ORIGINS:
            response.headers.setdefault("Access-Control-Allow-Credentials", "true")
        return response

app.add_middleware(EnsureCorsCredentialsMiddleware)

@app.options("/{rest_of_path:path}", include_in_schema=False)
async def any_options(_: Request, rest_of_path: str):
    return Response(status_code=204)

with suppress(Exception):
    from starlette.middleware.proxy_headers import ProxyHeadersMiddleware
    app.add_middleware(ProxyHeadersMiddleware, trusted_hosts="*")

trusted_hosts = env_list("TRUSTED_HOSTS") or ["*", ".onrender.com", ".smartbiz.site", "localhost", "127.0.0.1"]
app.add_middleware(TrustedHostMiddleware, allowed_hosts=trusted_hosts)

app.add_middleware(GZipMiddleware, minimum_size=512)
app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(RequestIDMiddleware)
app.add_middleware(CookiePolicyMiddleware)

# ────────────────────────── Static files ──────────────────────────

class _Uploads(StaticFiles):
    def is_not_modified(self, scope, request_headers, stat_result, etag=None):
        return super().is_not_modified(scope, request_headers, stat_result, etag)

app.mount("/uploads", _Uploads(directory=str(UPLOADS_DIR)), name="uploads")

# ────────────────────────── DB dependency ──────────────────────────

def get_db() -> Session:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# ────────────────────────── Exception handlers ──────────────────────────

@app.exception_handler(ClientDisconnect)
async def client_disconnect_handler(_: Request, __: ClientDisconnect):
    return Response(status_code=499)

@app.exception_handler(RequestValidationError)
async def validation_exception_handler(_: Request, exc: RequestValidationError):
    return JSONResponse(status_code=422, content={"detail": exc.errors()})

@app.exception_handler(HTTPException)
async def http_exception_handler(_: Request, exc: HTTPException):
    payload = exc.detail if isinstance(exc.detail, dict) else {"detail": exc.detail}
    return JSONResponse(status_code=exc.status_code, content=payload)

@app.exception_handler(Exception)
async def unhandled_exception_handler(_: Request, exc: Exception):
    if isinstance(exc, anyio.EndOfStream):
        return Response(status_code=499)
    logger.exception("unhandled")
    return JSONResponse(status_code=500, content={"detail": "Internal Server Error"})

# ────────────────────────── Token & cookie utils (lightweight) ──────────────────────────

SECRET = (os.getenv("AUTH_SECRET") or os.getenv("SECRET_KEY") or "dev-secret").encode("utf-8")
ACCESS_TTL = int(os.getenv("ACCESS_TOKEN_TTL_SECONDS", "3600"))
COOKIE_NAME = os.getenv("ACCESS_COOKIE", os.getenv("AUTH_COOKIE_NAME", "sb_access"))

def _sign(payload: str) -> str:
    sig = hmac.new(SECRET, payload.encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(sig).decode().rstrip("=")

def _mint_token(user_id: str, ttl: int = ACCESS_TTL) -> str:
    now = int(time.time())
    raw = f"{user_id}.{now}.{ttl}.{uuid.uuid4().hex}"
    return raw + "." + _sign(raw)

def _verify_token(token: str) -> Tuple[bool, Optional[str]]:
    try:
        p, sig = token.rsplit(".", 1)
        if not hmac.compare_digest(_sign(p), sig):
            return False, None
        user_id, ts, ttl, _rnd = p.split(".", 3)
        if int(time.time()) > int(ts) + int(ttl):
            return False, None
        return True, user_id
    except Exception:
        return False, None

def _set_access_cookie(resp: Response, token: str, ttl: int = ACCESS_TTL) -> None:
    resp.set_cookie(
        key=COOKIE_NAME,
        value=token,
        max_age=ttl,
        secure=True,
        httponly=True,
        samesite="none",
        path="/",
    )

def _clear_access_cookie(resp: Response) -> None:
    resp.delete_cookie(key=COOKIE_NAME, path="/")

# ────────────────────────── Password hashing helpers ──────────────────────────

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
    return stored == pw

# ────────────────────────── Minimal user table access (RAW SQL) ──────────────────────────

def _get_user_by_email(db: Session, email: str) -> Optional[dict]:
    sql = text(f'SELECT * FROM {USER_TABLE} WHERE LOWER(email) = LOWER(:email) LIMIT 1')
    row = db.execute(sql, {"email": email}).mappings().first()
    return dict(row) if row else None

def _get_user_by_username(db: Session, uname: str) -> Optional[dict]:
    cols = _users_columns()
    for cand in ("username", "user_name", "handle"):
        if cand in cols:
            sql = text(f'SELECT * FROM {USER_TABLE} WHERE LOWER("{cand}") = LOWER(:u) LIMIT 1')
            row = db.execute(sql, {"u": uname}).mappings().first()
            if row:
                return dict(row)
    return None

def _get_user_by_id(db: Session, user_id: str) -> Optional[dict]:
    sql = text(f'SELECT * FROM {USER_TABLE} WHERE id::text = :id LIMIT 1')
    row = db.execute(sql, {"id": user_id}).mappings().first()
    return dict(row) if row else None

def _create_user(db: Session, email: str, username: str, password: str) -> dict:
    hpw = _hash_password(password)
    cols = _users_columns()
    uname_col = next((c for c in ("username", "user_name", "handle") if c in cols), "username")
    sql = text(
        f'INSERT INTO {USER_TABLE} (email, "{uname_col}", {PW_COL}) '
        f'VALUES (:email, :username, :hpw) '
        f'RETURNING id, email, "{uname_col}" AS username'
    )
    try:
        row = db.execute(sql, {"email": email, "username": username, "hpw": hpw}).mappings().first()
        db.commit()
        return dict(row) if row else {"email": email, "username": username}
    except Exception as e:
        db.rollback()
        msg = str(e)
        if "unique" in msg.lower() or "duplicate" in msg.lower() or "23505" in msg:
            raise HTTPException(status_code=409, detail="Email already registered")
        logger.exception("create_user")
        raise HTTPException(status_code=500, detail="Failed to create user")

def _verify_login_identifier(db: Session, identifier: str, password: str) -> Optional[dict]:
    u = _get_user_by_email(db, identifier) if "@" in identifier else None
    if not u:
        u = _get_user_by_username(db, identifier) or _get_user_by_email(db, identifier)
    if not u:
        return None
    stored = u.get(PW_COL) or u.get("hashed_password") or u.get("password") or ""
    if not stored:
        return None
    if _verify_password(password, stored):
        return {
            "id": str(u.get("id")),
            "email": u.get("email"),
            "username": u.get("username") or u.get("user_name") or u.get("handle"),
        }
    return None

# ────────────────────────── Auth schemas ──────────────────────────

class SignupIn(BaseModel):
    email: EmailStr
    password: constr(min_length=6)
    username: constr(min_length=2)

class LoginIn(BaseModel):
    email: Optional[EmailStr] = None
    us
