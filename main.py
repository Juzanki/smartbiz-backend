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
from typing import Callable, Iterable, Optional

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

# ──────────────────────────────────────────────────────────────────────────────
# Env helpers
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
    if not url:
        return ""
    return re.sub(r"://([^:@/]+):([^@/]+)@", r"://\1:****@", url)

# ──────────────────────────────────────────────────────────────────────────────
# Paths & base env
# ──────────────────────────────────────────────────────────────────────────────

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

# ──────────────────────────────────────────────────────────────────────────────
# Logging
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
# Optional: detect password column name once
# ──────────────────────────────────────────────────────────────────────────────
with suppress(Exception):
    from sqlalchemy import inspect as _sa_inspect
    insp = _sa_inspect(engine)
    cols = {c["name"] for c in insp.get_columns("users")}
    chosen = "hashed_password" if "hashed_password" in cols else "password_hash"
    os.environ.setdefault("SMARTBIZ_PWHASH_COL", chosen)
    logger.info("PW column = %s (users cols: %s)", chosen, sorted(cols))

# ──────────────────────────────────────────────────────────────────────────────
# Middlewares
# ──────────────────────────────────────────────────────────────────────────────

class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        try:
            response: Response = await call_next(request)
        except (ClientDisconnect, anyio.EndOfStream):
            return Response(status_code=499)
        except Exception as e:
            logger.exception("security-mw: %s", e)
            return JSONResponse(status_code=500, content={"detail": "Internal server error"})
        # Conservative, FE uses fetch; CSP left to CDN
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
        start = time.perf_counter()
        try:
            response: Response = await call_next(request)
        except (ClientDisconnect, anyio.EndOfStream):
            response = Response(status_code=499)
        except Exception as e:
            logger.exception("reqid-mw: %s", e)
            response = JSONResponse(status_code=500, content={"detail": "Internal Server Error"})
        # timing headers
        try:
            response.headers["x-request-id"] = rid
            response.headers["x-process-time-ms"] = str(int((time.perf_counter() - start) * 1000))
        except Exception:
            pass
        return response

# ──────────────────────────────────────────────────────────────────────────────
# DB ping
# ──────────────────────────────────────────────────────────────────────────────

def _db_ping() -> bool:
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False

# ──────────────────────────────────────────────────────────────────────────────
# Lifespan
# ──────────────────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Backend starting (env=%s, db=%s)", ENVIRONMENT, _sanitize_db_url(os.getenv("DATABASE_URL", "")))
    if not _db_ping():
        logger.warning("Database ping failed at startup.")
    try:
        yield
    finally:
        pass  # add graceful cleanup if you spawn tasks

# ──────────────────────────────────────────────────────────────────────────────
# FastAPI app
# ──────────────────────────────────────────────────────────────────────────────

_docs_enabled = env_bool("ENABLE_DOCS", ENVIRONMENT != "production")
app = FastAPI(
    title=os.getenv("APP_NAME", "SmartBiz Assistance API"),
    description="SmartBiz Assistance backend",
    version=os.getenv("APP_VERSION", "1.0.0"),
    docs_url="/docs" if _docs_enabled else None,
    redoc_url="/redoc" if _docs_enabled else None,
    openapi_url="/openapi.json" if _docs_enabled else None,
    lifespan=lifespan,
)

# Honor X-Forwarded-* from Render proxy
with suppress(Exception):
    from starlette.middleware.proxy_headers import ProxyHeadersMiddleware
    app.add_middleware(ProxyHeadersMiddleware, trusted_hosts="*")

# Trusted hosts
trusted = env_list("TRUSTED_HOSTS")
if trusted:
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=trusted)

# Compression + security + request-id
app.add_middleware(GZipMiddleware, minimum_size=512)
app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(RequestIDMiddleware)

# ──────────────────────────────────────────────────────────────────────────────
# CORS (Netlify + custom domain + localhost + deploy previews)
# ──────────────────────────────────────────────────────────────────────────────

def _resolve_cors_origins() -> list[str]:
    defaults = [
        "https://smartbiz.site",
        "https://www.smartbiz.site",
        "https://smartbizsite.netlify.app",
        "http://localhost:5173", "http://127.0.0.1:5173",
        "http://localhost:4173", "http://127.0.0.1:4173",
    ]
    env_origins = env_list("ALLOWED_ORIGINS") + env_list("CORS_ORIGINS")
    env_urls = [os.getenv(k) for k in ("FRONTEND_URL", "WEB_URL", "VITE_API_BASE_URL", "NETLIFY_PUBLIC_URL")]
    return _uniq([*env_origins, *env_urls, *defaults])

ALLOW_ORIGINS = _resolve_cors_origins()
DEFAULT_REGEX = (
    r"^https:\/\/([0-9a-z\-]+--)?smartbizsite\.netlify\.app$"  # Netlify deploy previews
    r"|^https?:\/\/(localhost|127\.0\.0\.1)(:\d+)?$"
)
ALLOW_ORIGIN_REGEX = os.getenv("CORS_ORIGIN_REGEX", DEFAULT_REGEX)

CORS_MODE = os.getenv("CORS_MODE", "strict").strip().lower()  # strict|allow-any|dev-safe
if CORS_MODE == "allow-any":
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[],
        allow_origin_regex=r".*",
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["*"],
        max_age=86400,
    )
else:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=ALLOW_ORIGINS,
        allow_origin_regex=ALLOW_ORIGIN_REGEX if CORS_MODE != "strict" else None,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["*"],
        expose_headers=["*"],
        max_age=86400,
    )

# OPTIONS catch-all (preflight must succeed even if auth guards exist)
@app.options("/{rest_of_path:path}", include_in_schema=False)
async def any_options(_: Request, rest_of_path: str):
    return Response(status_code=204)

# ──────────────────────────────────────────────────────────────────────────────
# Static /uploads
# ──────────────────────────────────────────────────────────────────────────────

class _Uploads(StaticFiles):
    def is_not_modified(self, scope, request_headers, stat_result, etag=None):
        return super().is_not_modified(scope, request_headers, stat_result, etag)

app.mount("/uploads", _Uploads(directory=str(UPLOADS_DIR)), name="uploads")

# ──────────────────────────────────────────────────────────────────────────────
# DB init (dev only)
# ──────────────────────────────────────────────────────────────────────────────

if env_bool("AUTO_CREATE_TABLES", ENVIRONMENT != "production"):
    with suppress(Exception):
        Base.metadata.create_all(bind=engine, checkfirst=True)

def get_db() -> Session:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# ──────────────────────────────────────────────────────────────────────────────
# Exceptions
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
    logger.exception("unhandled: %s", exc)
    return JSONResponse(status_code=500, content={"detail": "Internal Server Error"})

# ──────────────────────────────────────────────────────────────────────────────
# Include routers (both "" and "/api" if configured)
# ──────────────────────────────────────────────────────────────────────────────

def _safe_include(module_path: str, *, attr: str = "router", prefixes: list[str] = [""]) -> None:
    with suppress(Exception):
        mod = importlib.import_module(module_path)
        r = getattr(mod, attr, None)
        if r is None:
            logger.warning("Router attr '%s' missing in %s", attr, module_path)
            return
        for p in prefixes:
            app.include_router(r, prefix=p)

prefixes = _uniq(["", *([p.strip() for p in os.getenv("API_PREFIXES", "/api").split(",") if p.strip()])])

# Known routers (optional presence)
for mod in [
    "routes.auth_routes",
    "routes.invoice",
    "routes.owner_routes",
    "routes.order_notification",
    "routes.ai_responder",
    "backend.routes.auth_routes",
    "backend.routes.invoice",
    "backend.routes.owner_routes",
    "backend.routes.order_notification",
    "backend.routes.ai_responder",
    "api.health",
    "backend.api.health",
    "websocket.ws_routes",
    "backend.websocket.ws_routes",
]:
    _safe_include(mod, prefixes=prefixes if not mod.endswith(("ws_routes",)) else [""])

# ──────────────────────────────────────────────────────────────────────────────
# Auth aliases (/signup, /register, /login) with and without /api
# ──────────────────────────────────────────────────────────────────────────────

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

# ──────────────────────────────────────────────────────────────────────────────
# Diagnostics & health
# ──────────────────────────────────────────────────────────────────────────────

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
        "env": ENVIRONMENT,
        "api_prefixes": prefixes,
    }

@app.get("/_info", tags=["Debug"])
async def info():
    return {
        "env": ENVIRONMENT,
        "db": _sanitize_db_url(os.getenv("DATABASE_URL", "")),
        "cors_mode": os.getenv("CORS_MODE", "strict"),
        "allow_origins": ALLOW_ORIGINS,
        "allow_origin_regex": os.getenv("CORS_ORIGIN_REGEX", DEFAULT_REGEX),
        "api_prefixes": prefixes,
    }

# Super-light health (no DB)
@app.get("/health", include_in_schema=False)
async def health():
    return {"ok": True}

@app.get("/healthz", tags=["Health"])
async def healthz():
    return {"ok": True}

@app.get("/readyz", tags=["Health"])
async def readyz():
    return {"ready": True, "db_ok": _db_ping()}

@app.get("/dbz", tags=["Health"])
async def dbz():
    return {"db_ok": _db_ping()}

# Public POST echo for CORS test (no JWT)
@app.post("/echo", include_in_schema=False)
async def echo(payload: dict):
    return {"ok": True, "you_sent": payload}

if env_bool("DEBUG", False):
    @app.get("/_routes", tags=["Debug"])
    async def list_routes():
        return [
            {"methods": sorted(list(getattr(r, "methods", []) or [])), "path": getattr(r, "path", None)}
            for r in app.routes
        ]

# ──────────────────────────────────────────────────────────────────────────────
# Entrypoint (local dev)
# ──────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":  # pragma: no cover
    import uvicorn
    uvicorn.run(
        "main:app",
        host=os.getenv("HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", "8000")),
        reload=env_bool("RELOAD", ENVIRONMENT != "production"),
        proxy_headers=True,
        forwarded_allow_ips="*",
    )
