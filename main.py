# backend/main.py
from __future__ import annotations

import base64
import hmac
import hashlib
import importlib
import json
import logging
import os
import re
import time
import uuid
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Callable, Iterable, Optional, List, Tuple

import anyio
import httpx
from fastapi import FastAPI, HTTPException, Request, Header, Depends, status, Body
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, EmailStr, constr
from sqlalchemy import text
from sqlalchemy.orm import Session
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.requests import ClientDisconnect
from starlette.responses import JSONResponse, Response, RedirectResponse
from starlette.datastructures import MutableHeaders

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
            out.append(x); seen.add(x)
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
with suppress(Exception):
    from sqlalchemy import inspect as _sa_inspect
    insp = _sa_inspect(engine)
    cols = {c["name"] for c in insp.get_columns("users")}
    chosen = "hashed_password" if "hashed_password" in cols else "password_hash"
    os.environ.setdefault("SMARTBIZ_PWHASH_COL", chosen)
    logger.info("PW column = %s (users cols: %s)", chosen, sorted(cols))

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
        # Basic hardening
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault("Permissions-Policy", "geolocation=(), microphone=(), camera=()")
        # CSP relaxed enough for SPA assets; adjust if needed
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data: https:; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline';"
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
    """
    Ensures Set-Cookie headers are cross-site compatible for SPA on Netlify (frontend) + Render (backend).
    Adds/normalizes: SameSite=None; Secure; HttpOnly (configurable by env).
    """
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
    docs_url="/docs" if _docs_enabled else None,
    redoc_url="/redoc" if _docs_enabled else None,
    openapi_url="/openapi.json" if _docs_enabled else None,
    lifespan=lifespan,
)

# ───────────── CORS (must be BEFORE other middleware & routers) ─────────────

def _resolve_cors_origins() -> List[str]:
    hardcoded = [
        "https://smartbizsite.netlify.app",
        "https://smartbiz.site",
        "https://www.smartbiz.site",
        "http://localhost:5173", "http://127.0.0.1:5173",
        "http://localhost:4173", "http://127.0.0.1:4173",
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

# ────────────────────────── Light-weight token utils ──────────────────────────
# NOT a full JWT; enough for frontend flow (refresh, cookie). Replace with JWT in prod.

SECRET = (os.getenv("AUTH_SECRET") or os.getenv("SECRET_KEY") or "dev-secret").encode("utf-8")
ACCESS_TTL = int(os.getenv("ACCESS_TOKEN_TTL_SECONDS", "3600"))
REFRESH_TTL = int(os.getenv("REFRESH_TOKEN_TTL_SECONDS", "1209600"))  # 14 days
COOKIE_NAME = os.getenv("ACCESS_COOKIE", "sb_access")

def _sign(payload: str) -> str:
    sig = hmac.new(SECRET, payload.encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(sig).decode().rstrip("=")

def _mint_token(user_id: str, ttl: int) -> str:
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

# ────────────────────────── Password hashing helpers ──────────────────────────
# Tries bcrypt if available; otherwise PBKDF2-HMAC-SHA256.

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
    # raw fallback (not recommended)
    return stored == pw

# ────────────────────────── Minimal user table access ──────────────────────────

USER_TABLE = os.getenv("USER_TABLE", "users")
PW_COL = os.getenv("SMARTBIZ_PWHASH_COL", "password_hash")

def _get_user_by_email(db: Session, email: str) -> Optional[dict]:
    sql = text(f"SELECT * FROM {USER_TABLE} WHERE email = :email LIMIT 1")
    row = db.execute(sql, {"email": email}).mappings().first()
    return dict(row) if row else None

def _create_user(db: Session, email: str, username: str, password: str) -> dict:
    # Try common columns: id, email, username, <pw column>, created_at
    hpw = _hash_password(password)
    sql = text(f"""
        INSERT INTO {USER_TABLE} (email, username, {PW_COL})
        VALUES (:email, :username, :hpw)
        RETURNING id, email, username
    """)
    try:
        row = db.execute(sql, {"email": email, "username": username, "hpw": hpw}).mappings().first()
        db.commit()
        return dict(row) if row else {"email": email, "username": username}
    except Exception as e:
        db.rollback()
        # unique violation → 23505 (PG), else generic
        msg = str(e)
        if "unique" in msg.lower() or "duplicate" in msg.lower() or "23505" in msg:
            raise HTTPException(status_code=409, detail="Email already registered")
        logger.exception("create_user")
        raise HTTPException(status_code=500, detail="Failed to create user")

def _verify_login(db: Session, email: str, password: str) -> Optional[dict]:
    u = _get_user_by_email(db, email)
    if not u:
        return None
    stored = u.get(PW_COL) or u.get("hashed_password") or u.get("password") or ""
    if not stored:
        return None
    if _verify_password(password, stored):
        return {"id": str(u.get("id")), "email": u.get("email"), "username": u.get("username")}
    return None

# ────────────────────────── Auth models ──────────────────────────

class SignupIn(BaseModel):
    email: EmailStr
    password: constr(min_length=6)
    username: constr(min_length=2)

class LoginIn(BaseModel):
    email: EmailStr
    password: constr(min_length=1)

class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: Optional[dict] = None

# ────────────────────────── Auth endpoints (match your axios) ──────────────────────────

@app.post("/api/auth/register", response_model=TokenOut, tags=["Auth"])
def register(payload: SignupIn, db: Session = Depends(get_db)):
    if _get_user_by_email(db, payload.email):
        raise HTTPException(status_code=409, detail="Email already registered")
    user = _create_user(db, payload.email, payload.username, payload.password)
    access = _mint_token(user_id=str(user.get("id") or user["email"]), ttl=ACCESS_TTL)
    resp = JSONResponse({"access_token": access, "token_type": "bearer", "user": user}, status_code=status.HTTP_201_CREATED)
    _set_access_cookie(resp, access, ACCESS_TTL)
    return resp

@app.post("/api/auth/login", response_model=TokenOut, tags=["Auth"])
def login(payload: LoginIn, db: Session = Depends(get_db)):
    user = _verify_login(db, payload.email, payload.password)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid credentials")
    access = _mint_token(user_id=str(user.get("id") or user["email"]), ttl=ACCESS_TTL)
    resp = JSONResponse({"access_token": access, "token_type": "bearer", "user": user})
    _set_access_cookie(resp, access, ACCESS_TTL)
    return resp

@app.post("/api/auth/token/refresh", response_model=TokenOut, tags=["Auth"])
def refresh_token(request: Request, db: Session = Depends(get_db)):
    # prefer cookie; fallback to Authorization: Bearer
    raw = request.cookies.get(COOKIE_NAME)
    if not raw:
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            raw = auth.split(" ", 1)[1].strip()
    if not raw:
        raise HTTPException(status_code=401, detail="Missing token")

    ok, user_id = _verify_token(raw)
    if not ok or not user_id:
        raise HTTPException(status_code=401, detail="Invalid token")

    # try to fetch user email/username
    user = None
    with suppress(Exception):
        # if user_id was email, this still works
        u = _get_user_by_email(db, user_id)
        if not u:
            # maybe user_id is numeric id
            sql = text(f"SELECT id,email,username FROM {USER_TABLE} WHERE id::text = :uid LIMIT 1")
            row = db.execute(sql, {"uid": user_id}).mappings().first()
            user = dict(row) if row else None
        else:
            user = u

    access = _mint_token(user_id=user_id, ttl=ACCESS_TTL)
    resp = JSONResponse({"access_token": access, "token_type": "bearer", "user": user})
    _set_access_cookie(resp, access, ACCESS_TTL)
    return resp

# Compatibility: accept legacy /api/auth/signup → forward internally to register (no redirect)
@app.post("/api/auth/signup", tags=["Auth"], include_in_schema=False)
async def _compat_signup(request: Request):
    ctype = request.headers.get("content-type", "")
    try:
        if "application/json" in ctype:
            payload = await request.json()
            send_kwargs = dict(json=payload)
            headers = {}
        elif "application/x-www-form-urlencoded" in ctype or "multipart/form-data" in ctype:
            form = await request.form()
            payload = dict(form)
            send_kwargs = dict(json=payload)
            headers = {}
        else:
            raw = await request.body()
            send_kwargs = dict(content=raw)
            headers = {"content-type": ctype or "application/json"}
    except Exception:
        raw = await request.body()
        send_kwargs = dict(content=raw)
        headers = {"content-type": ctype or "application/json"}

    async with httpx.AsyncClient(app=app, base_url="http://internal") as ac:
        resp = await ac.post("/api/auth/register", headers=headers, **send_kwargs)

    mt = resp.headers.get("content-type", "application/json")
    if "application/json" in mt:
        try:
            return JSONResponse(resp.json(), status_code=resp.status_code)
        except Exception:
            return Response(resp.content, status_code=resp.status_code, media_type=mt)
    return Response(resp.content, status_code=resp.status_code, media_type=mt)

# Optional aliases (/api/register, /api/login)
def _register_auth_aliases(pref: str):
    p = pref
    @app.post(f"{p}/register", include_in_schema=False, tags=["Auth"])
    async def _register_alias(request: Request):
        data = await request.body()
        headers = {"content-type": request.headers.get("content-type", "application/json")}
        async with httpx.AsyncClient(app=app, base_url="http://internal") as ac:
            r = await ac.post(f"{p}/auth/register", headers=headers, content=data)
        return Response(content=r.content, status_code=r.status_code, media_type=r.headers.get("content-type"))
    @app.post(f"{p}/login", include_in_schema=False, tags=["Auth"])
    async def _login_alias(request: Request):
        data = await request.body()
        headers = {"content-type": request.headers.get("content-type", "application/json")}
        async with httpx.AsyncClient(app=app, base_url="http://internal") as ac:
            r = await ac.post(f"{p}/auth/login", headers=headers, content=data)
        return Response(content=r.content, status_code=r.status_code, media_type=r.headers.get("content-type"))

prefixes = _uniq([p.strip() for p in os.getenv("API_PREFIXES", "/api").split(",") if p.strip()])
for pref in prefixes:
    _register_auth_aliases(pref)

# ────────────────────────── Routers (best-effort include) ──────────────────────────

def _safe_include(module_path: str, *, attr: str = "router", prefixes: List[str] = [""]) -> None:
    try:
        mod = importlib.import_module(module_path)
        r = getattr(mod, attr, None)
        if r is None:
            logger.warning("Router attribute '%s' missing in %s", attr, module_path)
            return
        for p in prefixes:
            app.include_router(r, prefix=p)
            logger.debug("Included router %s with prefix %s", module_path, p)
    except ImportError:
        logger.debug("Module %s not available")
    except Exception as e:
        logger.error("Failed to include router %s: %s", module_path, e)

routers_to_try = [
    # flat
    "routes.auth", "routes.auth_routes", "routes.invoice", "routes.owner",
    "routes.owner_routes", "routes.order", "routes.order_notification",
    "routes.ai", "routes.ai_responder", "routes.chat", "routes.payments",
    "routes.wallet", "api.health", "api.status", "websocket.ws", "websocket.ws_routes",
    # package
    "backend.routes.auth", "backend.routes.auth_routes", "backend.routes.invoice",
    "backend.routes.owner", "backend.routes.owner_routes", "backend.routes.order",
    "backend.routes.order_notification", "backend.routes.ai", "backend.routes.ai_responder",
    "backend.routes.chat", "backend.routes.payments", "backend.routes.wallet",
    "backend.api.health", "backend.api.status", "backend.websocket.ws", "backend.websocket.ws_routes",
]
for router_module in routers_to_try:
    _safe_include(router_module, prefixes=prefixes)

# ────────────────────────── Debug utilities ──────────────────────────

@app.get("/debug/cors-ping", tags=["Debug"])
async def cors_ping():
    return {"ok": True, "ts": time.time()}

@app.get("/debug/echo-headers", tags=["Debug"])
async def echo_headers(request: Request, origin: str | None = Header(default=None)):
    return {
        "method": request.method,
        "path": request.url.path,
        "origin_header": origin,
        "host_header": request.headers.get("host"),
        "referer": request.headers.get("referer"),
        "allowed_cors_origins": ALLOW_ORIGINS,
        "ts": time.time(),
    }

@app.get("/debug/set-cookie", tags=["Debug"], include_in_schema=False)
async def debug_set_cookie():
    r = JSONResponse({"ok": True, "note": "cookie issued"})
    _set_access_cookie(r, _mint_token("debug", ACCESS_TTL), ACCESS_TTL)
    return r

# ────────────────────────── Core endpoints ──────────────────────────

class WhatsAppMessage(BaseModel):
    message: str

@app.post("/send-whatsapp/", tags=["WhatsApp"])
async def send_whatsapp_message(payload: WhatsAppMessage):
    return {"status": "queued", "to": os.getenv("RECIPIENT_PHONE", "N/A"), "message": payload.message}

@app.get("/", tags=["Root"])
async def root():
    return {
        "message": "SmartBiz Assistance API",
        "status": "running",
        "version": app.version,
        "environment": ENVIRONMENT,
        "database": "connected" if _db_ping() else "disconnected",
        "documentation": "/docs" if _docs_enabled else "disabled",
    }

@app.get("/_info", tags=["Debug"], include_in_schema=_docs_enabled)
async def info():
    return {
        "environment": ENVIRONMENT,
        "database": _sanitize_db_url(os.getenv("DATABASE_URL", "")),
        "cors_origins": ALLOW_ORIGINS,
        "api_prefixes": prefixes,
        "render_url": os.getenv("RENDER_PUBLIC_URL", "Not set"),
        "netlify_url": os.getenv("NETLIFY_PUBLIC_URL", "Not set"),
    }

@app.get("/health", include_in_schema=False)
async def health():
    return {"ok": True, "ts": time.time()}

@app.get("/healthz", tags=["Health"])
async def healthz():
    db_ok = _db_ping()
    return {"status": "healthy" if db_ok else "degraded", "database": db_ok, "ts": time.time()}

@app.get("/readyz", tags=["Health"])
async def readyz():
    db_ok = _db_ping()
    return {"ready": db_ok, "ts": time.time()}

@app.post("/echo", include_in_schema=False)
async def echo(payload: dict = Body(default_factory=dict)):
    return {"ok": True, "echo": payload, "ts": time.time()}

# ────────────────────────── Entrypoint (local dev) ──────────────────────────

if __name__ == "__main__":  # pragma: no cover
    import uvicorn
    uvicorn.run(
        "backend.main:app",
        host=os.getenv("HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", "8000")),
        reload=env_bool("RELOAD", ENVIRONMENT != "production"),
        proxy_headers=True,
        forwarded_allow_ips="*",
    )
