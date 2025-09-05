# backend/main.py
from __future__ import annotations

import asyncio
import importlib
import inspect as _pyinspect
import json
import logging
import os
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
from sqlalchemy import text, inspect as _sa_inspect
from sqlalchemy.orm import Session
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.requests import ClientDisconnect
from starlette.responses import JSONResponse, Response

# ──────────────────────────────────────────────────────────────────────────────
# 1) Load env files early
# ──────────────────────────────────────────────────────────────────────────────
def _load_env() -> None:
    with suppress(Exception):
        from dotenv import load_dotenv
        root = Path(__file__).resolve().parents[1]
        # Order: .env → .env.local → env by ENVIRONMENT
        load_dotenv(root / ".env", override=False)
        load_dotenv(root / ".env.local", override=False)
        env = (os.getenv("ENVIRONMENT") or os.getenv("ENV") or "development").strip().lower()
        fname = ".env.production" if env == "production" else ".env.development"
        load_dotenv(root / fname, override=False)

_load_env()

# ──────────────────────────────────────────────────────────────────────────────
# 2) Helpers
# ──────────────────────────────────────────────────────────────────────────────
def env_bool(key: str, default: bool = False) -> bool:
    v = os.getenv(key)
    return default if v is None else v.strip().lower() in {"1", "true", "yes", "y", "on"}

def env_list(key: str) -> list[str]:
    raw = os.getenv(key, "")
    return [x.strip() for x in raw.split(",") if x and x.strip()]

def _uniq(items: Iterable[Optional[str]]) -> list[str]:
    out, seen = [], set()
    for x in items:
        if x and x not in seen:
            out.append(x); seen.add(x)
    return out

def _sanitize_db_url(url: str) -> str:
    try:
        from urllib.parse import urlparse
        p = urlparse(url)
        host = (p.hostname or "localhost").replace("localhost", "127.0.0.1")
        port = f":{p.port}" if p.port else ""
        dbn = (p.path or "").lstrip("/")
        return f"{p.scheme or 'db'}://****@{host}{port}/{dbn}"
    except Exception:
        return "hidden://****@db"

# ──────────────────────────────────────────────────────────────────────────────
# 3) Paths, environment, database URL
# ──────────────────────────────────────────────────────────────────────────────
BACKEND_DIR = Path(__file__).resolve().parent
ROOT_DIR = BACKEND_DIR.parent
UPLOADS_DIR = Path(os.getenv("UPLOADS_DIR", BACKEND_DIR / "uploads")).resolve()
UPLOADS_DIR.mkdir(parents=True, exist_ok=True)

ENVIRONMENT = (os.getenv("ENVIRONMENT") or os.getenv("ENV") or "development").lower()
DATABASE_URL = (
    os.getenv("DATABASE_URL")
    or os.getenv("RAILWAY_DATABASE_URL")
    or os.getenv("LOCAL_DATABASE_URL")
)
if not DATABASE_URL:
    if ENVIRONMENT in {"dev", "development", "local"}:
        DATABASE_URL = "sqlite:///./smartbiz_dev.db"
    else:
        raise RuntimeError("DATABASE_URL missing. Set DATABASE_URL/RAILWAY_DATABASE_URL/LOCAL_DATABASE_URL.")

# ──────────────────────────────────────────────────────────────────────────────
# 4) Logging
# ──────────────────────────────────────────────────────────────────────────────
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

# ──────────────────────────────────────────────────────────────────────────────
# 5) DB engine/session – detect password column BEFORE importing models
# ──────────────────────────────────────────────────────────────────────────────
from backend.db import Base, SessionLocal, engine  # type: ignore

def _detect_pw_column_and_set_env() -> str:
    chosen = os.getenv("SMARTBIZ_PWHASH_COL", "").strip()
    if chosen:
        logger.info("Using PW col from env: %s", chosen)
        return chosen
    try:
        insp = _sa_inspect(engine)
        cols = {c["name"] for c in insp.get_columns("users")}
        if "hashed_password" in cols and "password_hash" not in cols:
            chosen = "hashed_password"
        elif "password_hash" in cols and "hashed_password" not in cols:
            chosen = "password_hash"
        else:
            chosen = "hashed_password" if "hashed_password" in cols else "password_hash"
        os.environ["SMARTBIZ_PWHASH_COL"] = chosen
        logger.info("Auto-detected password column: %s (users columns: %s)", chosen, sorted(cols))
    except Exception as e:
        chosen = "password_hash"
        os.environ["SMARTBIZ_PWHASH_COL"] = chosen
        logger.warning("PW column detection failed, defaulting to %s (%s)", chosen, e)
    return chosen

_detect_pw_column_and_set_env()
import backend.models  # noqa

# Optional feature toggles (loaded lazily)
_HAS_LANG = False
with suppress(Exception):
    from backend.middleware.language import language_middleware  # type: ignore
    _HAS_LANG = True

_HAS_BG = False
with suppress(Exception):
    from backend.background import start_background_tasks  # type: ignore
    _HAS_BG = True

_HAS_SCHED = False
with suppress(Exception):
    from backend.tasks.scheduler import start_schedulers  # type: ignore
    _HAS_SCHED = True

_HAS_WS = False
with suppress(Exception):
    from backend.websocket import ws_routes  # type: ignore
    _HAS_WS = True

from backend.routes.auth_routes import router as auth_router, legacy_router as auth_legacy_router  # type: ignore

# ──────────────────────────────────────────────────────────────────────────────
# 6) Middlewares: Security headers + Request ID (guarding ClientDisconnect)
# ──────────────────────────────────────────────────────────────────────────────
class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        try:
            response: Response = await call_next(request)
            if response is None:
                return JSONResponse(status_code=500, content={"detail": "No response returned"})
        except ClientDisconnect:
            return Response(status_code=499)
        except anyio.EndOfStream:
            return Response(status_code=499)
        except Exception as e:
            logger.exception("Security middleware error: %s", e)
            return JSONResponse(status_code=500, content={"detail": "Internal server error (security middleware)"})
        # conservative defaults (CSP inaweza kuongezwa kulingana na mahitaji)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer-when-downgrade")
        response.headers.setdefault("X-XSS-Protection", "1; mode=block")
        response.headers.setdefault("Permissions-Policy", "geolocation=(), microphone=(), camera=()")
        return response

class RequestIDMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        rid = request.headers.get("x-request-id") or uuid.uuid4().hex
        request.state.request_id = rid
        try:
            response: Response = await call_next(request)
            if response is None:
                return JSONResponse(status_code=500, content={"detail": "No response returned"})
        except ClientDisconnect:
            response = Response(status_code=499)
        except anyio.EndOfStream:
            response = Response(status_code=499)
        except Exception as e:
            logger.exception("RequestID middleware error: %s", e)
            response = JSONResponse(status_code=500, content={"detail": "Internal Server Error"})
        response.headers["x-request-id"] = rid
        return response

# ──────────────────────────────────────────────────────────────────────────────
# 7) DB health helper
# ──────────────────────────────────────────────────────────────────────────────
def _db_ping_fallback() -> bool:
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False

# ──────────────────────────────────────────────────────────────────────────────
# 8) Lifespan
# ──────────────────────────────────────────────────────────────────────────────
def _spawn_service(fn):
    result = fn()
    return asyncio.create_task(result) if _pyinspect.isawaitable(result) else None

async def _maybe_await(fn):
    res = fn()
    return await res if _pyinspect.isawaitable(res) else res

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Using DB: %s", _sanitize_db_url(DATABASE_URL))
    logger.info("UPLOADS_DIR: %s", UPLOADS_DIR)
    if not _db_ping_fallback():
        logger.warning("Database ping failed at startup.")

    app.state.services_started = True
    app.state.bg_tasks: list[asyncio.Task] = []
    mode = os.getenv("SERVICE_START_MODE", "task").lower()  # 'task' | 'await'

    if _HAS_BG:
        if mode == "await":
            await _maybe_await(start_background_tasks)
        else:
            if t := _spawn_service(start_background_tasks):
                app.state.bg_tasks.append(t)

    if _HAS_SCHED and env_bool("ENABLE_SCHEDULER", True):
        if mode == "await":
            await _maybe_await(start_schedulers)
        else:
            if t := _spawn_service(start_schedulers):
                app.state.bg_tasks.append(t)

    try:
        yield
    finally:
        for t in getattr(app.state, "bg_tasks", []):
            if t and not t.done():
                t.cancel()
        await asyncio.gather(*[t for t in getattr(app.state, "bg_tasks", []) if t], return_exceptions=True)

# ──────────────────────────────────────────────────────────────────────────────
# 9) FastAPI app
# ──────────────────────────────────────────────────────────────────────────────
_docs_enabled = env_bool("ENABLE_DOCS", ENVIRONMENT not in {"production"})
app = FastAPI(
    title=os.getenv("APP_NAME", "SmartBiz Assistance API"),
    description="Powerful SaaS backend for automating business operations",
    version=os.getenv("VITE_APP_VERSION", os.getenv("APP_VERSION", "1.0.0")),
    docs_url="/docs" if _docs_enabled else None,
    redoc_url="/redoc" if _docs_enabled else None,
    openapi_url="/openapi.json" if _docs_enabled else None,
    lifespan=lifespan,
)

# ──────────────────────────────────────────────────────────────────────────────
# 10) CORS (modes: strict | dev-safe | allow-any)
# ──────────────────────────────────────────────────────────────────────────────
def _resolve_cors_origins() -> list[str]:
    env_origins = env_list("CORS_ORIGINS")
    dev = [
        "http://localhost:5173", "http://127.0.0.1:5173",
        "http://localhost:4173", "http://127.0.0.1:4173",
        "http://localhost:3000", "http://127.0.0.1:3000",
    ]
    env_urls = [os.getenv(k) for k in (
        "FRONTEND_URL", "WEB_URL", "VITE_WEB_URL", "VITE_APP_URL",
        "RAILWAY_PUBLIC_URL", "RENDER_EXTERNAL_URL", "NETLIFY_PUBLIC_URL"
    )]
    return _uniq([*dev, *env_origins, *env_urls])

CORS_MODE = os.getenv("CORS_MODE", "dev-safe").strip().lower()  # strict | dev-safe | allow-any
ALLOW_ORIGINS = _resolve_cors_origins()
# regex ya localhost/ngrok nk (inasaidia ports zozote za dev tunneling)
DEFAULT_REGEX = r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$|^https?://([a-z0-9-]+\.)*(ngrok-free\.app|trycloudflare\.com)$"
ALLOW_ORIGIN_REGEX = os.getenv("CORS_ORIGIN_REGEX", DEFAULT_REGEX)

# Tumia CORSMiddleware rasmi (starlette) – na chaguo la “allow-any” lenye credentials (DEV tu).
if CORS_MODE == "allow-any":
    # Ruhusu origin yoyote yenye credentials (⚠︎ DEV TU!)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[],                 # lazima mtupu ukitumia regex
        allow_origin_regex=r".*",         # origin yoyote
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["*"],
        max_age=86400,
    )
else:
    # strict/dev-safe: orodha + regex ya localhost/tunnels
    app.add_middleware(
        CORSMiddleware,
        allow_origins=ALLOW_ORIGINS,
        allow_origin_regex=ALLOW_ORIGIN_REGEX if CORS_MODE != "strict" else None,
        allow_credentials=True,           # muhimu kama unatumia cookies/Authorization
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["*"],
        max_age=86400,
    )

# Fallback preflight handler (baadhi ya proxies hu-drop OPTIONS);
# CORSMiddleware hushughulikia tayari, lakini hii ni safety net.
@app.options("/{rest_of_path:path}", include_in_schema=False)
async def any_options(_: Request, rest_of_path: str):
    return Response(status_code=204)

# Proxy headers / Trusted hosts
with suppress(Exception):
    from starlette.middleware.proxy_headers import ProxyHeadersMiddleware  # type: ignore
    app.add_middleware(ProxyHeadersMiddleware, trusted_hosts="*")

trusted = env_list("TRUSTED_HOSTS")
if trusted:
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=trusted)

# Compression + security + request-id + language
app.add_middleware(GZipMiddleware, minimum_size=512)
app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(RequestIDMiddleware)
if '_HAS_LANG' in globals() and _HAS_LANG:
    with suppress(Exception):
        app.add_middleware(language_middleware)  # type: ignore

# Static uploads
class _Uploads(StaticFiles):
    def is_not_modified(self, scope, request_headers, stat_result, etag=None):
        return super().is_not_modified(scope, request_headers, stat_result, etag)

app.mount("/uploads", _Uploads(directory=str(UPLOADS_DIR)), name="uploads")

# ──────────────────────────────────────────────────────────────────────────────
# 11) DB bootstrap
# ──────────────────────────────────────────────────────────────────────────────
if env_bool("AUTO_CREATE_TABLES", ENVIRONMENT in {"dev", "development", "local"}):
    Base.metadata.create_all(bind=engine, checkfirst=True)

def get_db() -> Session:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# ──────────────────────────────────────────────────────────────────────────────
# 12) Exception handlers (special 499 for ClientDisconnect/EndOfStream)
# ──────────────────────────────────────────────────────────────────────────────
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
    logger.exception("Unhandled error: %s", exc)
    return JSONResponse(status_code=500, content={"detail": "Internal Server Error"})

# ──────────────────────────────────────────────────────────────────────────────
# 13) Routers
# ──────────────────────────────────────────────────────────────────────────────
def _safe_include(module_path: str, *, attr: str = "router", prefix: str | None = None, tags: list[str] | None = None):
    try:
        mod = importlib.import_module(module_path)
        r = getattr(mod, attr, None)
        if r is not None:
            app.include_router(r, prefix=prefix or "", tags=tags or [])
            logger.debug("Included router: %s", module_path)
        else:
            logger.warning("Router attr '%s' not found in %s", attr, module_path)
    except Exception as e:
        logger.warning("Skipping router %s (%s)", module_path, e)

app.include_router(auth_router)
app.include_router(auth_legacy_router)

for mod, pref, tag in [
    ("backend.routes.invoice", "/invoice", ["Invoice"]),
    ("backend.routes.owner_routes", "/owner", ["Owner"]),
    ("backend.routes.order_notification", "/order-notify", ["Order Notifications"]),
    ("backend.routes.ai_responder", "/ai", ["AI"]),
]:
    _safe_include(mod, prefix=pref, tags=tag)

if _HAS_WS:
    _safe_include("backend.websocket.ws_routes", prefix="", tags=["WebSocket"])

# ──────────────────────────────────────────────────────────────────────────────
# 14) Health / Debug endpoints
# ──────────────────────────────────────────────────────────────────────────────
class WhatsAppMessage(BaseModel):
    message: str

@app.post("/send-whatsapp/", tags=["WhatsApp"])
async def send_whatsapp_message(payload: WhatsAppMessage):
    return {"status": "queued", "to": os.getenv("RECIPIENT_PHONE", "N/A"), "message": payload.message}

@app.get("/", tags=["Root"])
async def root():
    return {
        "message": "Welcome to SmartBiz Assistance API",
        "docs": app.docs_url,
        "status": "running",
        "version": app.version,
        "env": ENVIRONMENT,
    }

@app.get("/healthz", tags=["Health"])
async def healthz():
    return {"ok": True}

@app.get("/readyz", tags=["Health"])
async def readyz():
    return {"ready": bool(getattr(app.state, "services_started", False))}

@app.get("/dbz", tags=["Health"])
async def dbz():
    return {"db_ok": bool(_db_ping_fallback())}

@app.get("/_cors", tags=["Debug"])
async def cors_debug(request: Request):
    return {
        "origin_header": request.headers.get("origin"),
        "allow_origins": ALLOW_ORIGINS,
        "allow_origin_regex": ALLOW_ORIGIN_REGEX,
        "cors_mode": CORS_MODE,
        "SMARTBIZ_PWHASH_COL": os.getenv("SMARTBIZ_PWHASH_COL"),
    }

if env_bool("DEBUG", False):
    @app.get("/_routes", tags=["Debug"])
    async def list_routes():
        return [
            {"methods": sorted(list(getattr(r, "methods", []) or [])), "path": getattr(r, "path", None)}
            for r in app.routes
        ]

# ──────────────────────────────────────────────────────────────────────────────
# 15) Entrypoint
# ──────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":  # pragma: no cover
    import uvicorn
    uvicorn.run(
        "backend.main:app",
        host=os.getenv("HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", "8000")),
        reload=env_bool("RELOAD", ENVIRONMENT in {"dev", "development", "local"}),
        proxy_headers=True,
        forwarded_allow_ips="*",
    )
