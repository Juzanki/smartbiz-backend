# backend/main.py
from __future__ import annotations

import importlib
import json
import logging
import os
import re
import time
import uuid
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Callable, Iterable, Optional, List

import anyio
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.requests import ClientDisconnect
from starlette.responses import JSONResponse, Response, RedirectResponse

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
    logger.info("SmartBiz starting (env=%s, db=%s)", ENVIRONMENT, _sanitize_db_url(os.getenv("DATABASE_URL", "")))
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

# ───────────── CORS (must be BEFORE other middleware) ─────────────

def _resolve_cors_origins() -> List[str]:
    # Hardcoded + env union
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

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOW_ORIGINS,      # explicit allow-list
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["set-cookie"],
    max_age=600,
)

# Preflight catch-all, just in case
@app.options("/{rest_of_path:path}", include_in_schema=False)
async def any_options(_: Request, rest_of_path: str):
    return Response(status_code=204)

# ───────────── Other middlewares AFTER CORS ─────────────

with suppress(Exception):
    from starlette.middleware.proxy_headers import ProxyHeadersMiddleware
    app.add_middleware(ProxyHeadersMiddleware, trusted_hosts="*")

trusted_hosts = env_list("TRUSTED_HOSTS") or ["*", ".onrender.com", ".smartbiz.site", "localhost", "127.0.0.1"]
app.add_middleware(TrustedHostMiddleware, allowed_hosts=trusted_hosts)

app.add_middleware(GZipMiddleware, minimum_size=512)
app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(RequestIDMiddleware)

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

# ────────────────────────── Routers ──────────────────────────

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
        logger.debug("Module %s not available", module_path)
    except Exception as e:
        logger.error("Failed to include router %s: %s", module_path, e)

prefixes = _uniq([p.strip() for p in os.getenv("API_PREFIXES", "/api").split(",") if p.strip()])

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

# ────────────────────────── Auth: NO-REDIRECT signup shim ──────────────────────────
# Hii huepuka 307 redirect (ambayo mara nyingine huvuruga CORS) kwa
# kupokea /api/auth/signup na kuipeleka ndani ya ASGI kwenda /api/auth/register.

try:
    import httpx
    _HAVE_HTTPX = True
except Exception:
    _HAVE_HTTPX = False

@app.post("/api/auth/signup", tags=["Auth"], include_in_schema=False)
async def _compat_signup(request: Request):
    """
    Accepts legacy /api/auth/signup and forwards internally to /api/auth/register
    without external redirect (fixes CORS/preflight on some clients).
    """
    data = None
    ctype = request.headers.get("content-type", "")
    try:
        if "application/json" in ctype:
            data = await request.json()
        elif "application/x-www-form-urlencoded" in ctype or "multipart/form-data" in ctype:
            form = await request.form()
            data = dict(form)
    except Exception:
        data = None

    if not _HAVE_HTTPX:
        # fallback (still works, but less ideal)
        return RedirectResponse(url="/api/auth/register", status_code=307)

    async with httpx.AsyncClient(app=app, base_url="http://internal") as ac:
        if data is None:
            raw = await request.body()
            resp = await ac.post("/api/auth/register", content=raw, headers={"content-type": ctype or "application/json"})
        else:
            resp = await ac.post("/api/auth/register", json=data)

    mt = resp.headers.get("content-type", "application/json")
    if "application/json" in mt:
        try:
            return JSONResponse(resp.json(), status_code=resp.status_code)
        except Exception:
            return Response(resp.content, status_code=resp.status_code, media_type=mt)
    return Response(resp.content, status_code=resp.status_code, media_type=mt)

# ────────────────────────── Legacy convenient aliases (still kept) ──────────────────────────

def _register_auth_aliases(pref: str):
    p = pref

    @app.post(f"{p}/signup", include_in_schema=False, tags=["Auth"])
    async def _signup_alias():
        return RedirectResponse(url=f"{p}/auth/register", status_code=307)

    @app.post(f"{p}/register", include_in_schema=False, tags=["Auth"])
    async def _register_alias():
        return RedirectResponse(url=f"{p}/auth/register", status_code=307)

    @app.post(f"{p}/login", include_in_schema=False, tags=["Auth"])
    async def _login_alias():
        return RedirectResponse(url=f"{p}/auth/login", status_code=307)

for pref in prefixes:
    _register_auth_aliases(pref)

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
async def echo(payload: dict):
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
