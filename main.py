# backend/main.py
from __future__ import annotations

import os, sys, re, json, time, uuid, logging, secrets, hashlib
from pathlib import Path
from contextlib import asynccontextmanager, suppress
from typing import Callable, Iterable, Optional, List, Dict, Any

# ───────────────────────── Paths ─────────────────────────
THIS_FILE = Path(__file__).resolve()
BACKEND_DIR = THIS_FILE.parent
ROOT_DIR = BACKEND_DIR.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.append(str(ROOT_DIR))

BACKEND_PUBLIC_URL = os.getenv(
    "BACKEND_PUBLIC_URL",
    "https://smartbiz-backend-p45m.onrender.com"
).rstrip("/")

# ─────────────────────── Framework ───────────────────────
import anyio
from fastapi import FastAPI, HTTPException, Request, Depends, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.openapi.docs import get_swagger_ui_html, get_swagger_ui_oauth2_redirect_html
from pydantic import BaseModel, EmailStr, constr
from sqlalchemy import text
from sqlalchemy.orm import Session
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import ClientDisconnect
from starlette.responses import JSONResponse, Response, RedirectResponse

# ──────────────────────── DB imports ─────────────────────
try:
    from backend.db import Base, SessionLocal, engine
except Exception:
    from db import Base, SessionLocal, engine  # type: ignore

# ───────────────────── Env helpers ───────────────────────
def env_bool(key: str, default: bool = False) -> bool:
    v = os.getenv(key)
    return default if v is None else v.strip().lower() in {"1","true","yes","y","on"}

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
    return "" if not url else re.sub(r"://([^:@/]+):([^@/]+)@", r"://\1:****@", url)

ENVIRONMENT = (os.getenv("ENVIRONMENT") or os.getenv("ENV") or "production").lower()

# ─────────────────────── Logging ─────────────────────────
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
LOG_JSON = env_bool("LOG_JSON", False)

class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "lvl": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)

_handler = logging.StreamHandler()
_handler.setFormatter(_JsonFormatter() if LOG_JSON else logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s"))
root_logger = logging.getLogger()
root_logger.handlers = [_handler]
root_logger.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))
logger = logging.getLogger("smartbiz.main")

# ─────────────── User table introspection ────────────────
USER_TABLE = os.getenv("USER_TABLE", "users")
PW_COL = os.getenv("SMARTBIZ_PWHASH_COL", "password_hash")
_USERS_COLS: Optional[set[str]] = None

with suppress(Exception):
    from sqlalchemy import inspect as _sa_inspect
    _insp = _sa_inspect(engine)
    _USERS_COLS = {c["name"] for c in _insp.get_columns(USER_TABLE)}
    chosen = "hashed_password" if "hashed_password" in _USERS_COLS else ("password_hash" if "password_hash" in _USERS_COLS else PW_COL)
    os.environ.setdefault("SMARTBIZ_PWHASH_COL", chosen)
    PW_COL = chosen
    logger.info("Users cols: %s | PW col: %s", sorted(_USERS_COLS or []), PW_COL)

def _users_columns() -> set[str]:
    global _USERS_COLS
    if _USERS_COLS is not None: return _USERS_COLS
    with suppress(Exception):
        from sqlalchemy import inspect as _sa_inspect
        _USERS_COLS = {c["name"] for c in _sa_inspect(engine).get_columns(USER_TABLE)}
    return _USERS_COLS or set()

# ───────────────────── Middlewares ───────────────────────
class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        try:
            response: Response = await call_next(request)
        except (ClientDisconnect, anyio.EndOfStream):
            return Response(status_code=499)
        except Exception:
            logger.exception("security-mw")
            return JSONResponse(status_code=500, content={"detail": "Internal server error"})
        response.headers.setdefault("X-Content-Type-Options","nosniff")
        response.headers.setdefault("X-Frame-Options","DENY")
        response.headers.setdefault("Referrer-Policy","strict-origin-when-cross-origin")
        response.headers.setdefault("Permissions-Policy","geolocation=(), microphone=(), camera=()")
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
            response = JSONResponse(status_code=500, content={"detail":"Internal Server Error"})
        response.headers["x-request-id"] = rid
        response.headers["x-process-time-ms"] = str(int((time.perf_counter() - start) * 1000))
        return response

# ─────────────────────── DB helpers ──────────────────────
def _db_ping() -> bool:
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False

def get_db() -> Session:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# ─────────────────────── Lifespan ────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting SmartBiz (env=%s, db=%s)", ENVIRONMENT, _sanitize_db_url(os.getenv("DATABASE_URL","")))
    if not _db_ping():
        logger.error("Database connection failed at startup")
    else:
        logger.info("Database OK")
    if env_bool("AUTO_CREATE_TABLES", ENVIRONMENT != "production"):
        with suppress(Exception):
            Base.metadata.create_all(bind=engine, checkfirst=True)
            logger.info("Tables verified/created")
    try:
        yield
    finally:
        logger.info("Shutting down SmartBiz")

# ───────────────────────── App ───────────────────────────
_docs_enabled = env_bool("ENABLE_DOCS", ENVIRONMENT != "production")
app = FastAPI(
    title=os.getenv("APP_NAME","SmartBiz Assistance API"),
    description="SmartBiz Assistance Backend (Render + Netlify)",
    version=os.getenv("APP_VERSION","1.0.0"),
    docs_url=None,  # we provide /docs manually if enabled
    redoc_url="/redoc" if _docs_enabled else None,
    openapi_url="/openapi.json" if _docs_enabled else None,
    lifespan=lifespan,
)

# ───────────────────────── CORS ──────────────────────────
def _resolve_cors_origins() -> List[str]:
    hardcoded = [
        "https://smartbizsite.netlify.app",
        "https://smartbiz.site", "https://www.smartbiz.site",
        "http://localhost:5173", "http://127.0.0.1:5173",
        "http://localhost:4173", "http://127.0.0.1:4173",
    ]
    return _uniq(hardcoded + env_list("CORS_ORIGINS") + env_list("ALLOWED_ORIGINS"))

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
    logger.warning("CORS_ALLOW_ALL=1 (dev mode)")
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

app.add_middleware(GZipMiddleware, minimum_size=1024)
app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(RequestIDMiddleware)

# ───────────────────── Include Routers ───────────────────
# ⚠️ Usirudie prefix hapa – router tayari ina prefix="/auth"
def _import_auth_router():
    try:
        from routes.auth_routes import router as auth_router  # when running from project root
        return auth_router, "routes.auth_routes"
    except Exception as e1:
        try:
            from backend.routes.auth_routes import router as auth_router  # when package is 'backend'
            return auth_router, "backend.routes.auth_routes"
        except Exception as e2:
            logger.error("Failed to import auth router: %s | %s", e1, e2)
            raise

_auth_router, _auth_src = _import_auth_router()
app.include_router(_auth_router)
logger.info("Auth router registered from %s (prefix already set in router)", _auth_src)

# ───────────────── Global error handlers ─────────────────
@app.exception_handler(HTTPException)
async def http_exc_handler(_: Request, exc: HTTPException):
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

@app.exception_handler(RequestValidationError)
async def validation_exc_handler(_: Request, exc: RequestValidationError):
    return JSONResponse(status_code=422, content={"detail": "validation_error", "errors": exc.errors()})

# ───────────────────────── Routes ─────────────────────────
@app.get("/")
def root_redirect():
    # Prefer health; if docs enabled, redirect to docs for convenience
    return RedirectResponse("/docs" if _docs_enabled else "/health", status_code=302)

@app.get("/health")
@app.get("/api/health")
def health():
    return {"status":"healthy", "db":_db_ping(), "ts": time.time(), "base_url": BACKEND_PUBLIC_URL}

if _docs_enabled:
    @app.get("/docs", include_in_schema=False)
    async def custom_swagger_ui_html():
        return get_swagger_ui_html(openapi_url=app.openapi_url, title="SmartBiz API Docs")

    @app.get("/docs/oauth2-redirect", include_in_schema=False)
    async def swagger_ui_redirect():
        return get_swagger_ui_oauth2_redirect_html()

# CORS diagnostics (optional)
@app.get("/auth/_diag_cors")
async def cors_diag(request: Request):
    return {
        "origin_header": request.headers.get("origin"),
        "allowed_origins": ALLOW_ORIGINS if not CORS_ALLOW_ALL else ["* (regex)"],
        "auth_header_present": bool(request.headers.get("authorization") or request.headers.get("Authorization")),
    }
