from _future_ import annotations

# ── Path fallback (Render + local) ────────────────────────────────────────────
import os, sys, re, json, time, uuid, base64, hmac, hashlib, logging
from pathlib import Path
from contextlib import asynccontextmanager, suppress
from typing import Callable, Iterable, Optional, List, Tuple, Dict, Any

THIS_FILE = Path(_file_).resolve()
BACKEND_DIR = THIS_FILE.parent
ROOT_DIR = BACKEND_DIR.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.append(str(ROOT_DIR))

BACKEND_PUBLIC_URL = os.getenv(
    "BACKEND_PUBLIC_URL",
    "https://smartbiz-backend-p45m.onrender.com",
).rstrip("/")

import anyio
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.openapi.docs import get_swagger_ui_html, get_swagger_ui_oauth2_redirect_html
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, EmailStr, constr
from sqlalchemy import text
from sqlalchemy.orm import Session
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.requests import ClientDisconnect
from starlette.responses import JSONResponse, Response, RedirectResponse
from starlette.datastructures import MutableHeaders

# ── DB imports (package-first, then relative) ─────────────────────────────────
try:
    from backend.db import Base, SessionLocal, engine
except Exception:  # pragma: no cover
    from db import Base, SessionLocal, engine  # type: ignore

# ────────────────────────── Env helpers ───────────────────────────────────────
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
    return "" if not url else re.sub(r"://([^:@/]+):([^@/]+)@", r"://\1:@", url)

ENVIRONMENT = (os.getenv("ENVIRONMENT") or os.getenv("ENV") or "production").lower()
UPLOADS_DIR = Path(os.getenv("UPLOADS_DIR", BACKEND_DIR / "uploads")).resolve()
UPLOADS_DIR.mkdir(parents=True, exist_ok=True)

# ────────────────────────── Logging ───────────────────────────────────────────
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

# ────────────────────────── User table introspection (optional) ──────────────
USER_TABLE = os.getenv("USER_TABLE", "users")
PW_COL = os.getenv("SMARTBIZ_PWHASH_COL", "password_hash")
_USERS_COLS: Optional[set[str]] = None

with suppress(Exception):
    from sqlalchemy import inspect as _sa_inspect
    insp = _sa_inspect(engine)
    _USERS_COLS = {c["name"] for c in insp.get_columns(USER_TABLE)}
    chosen = "hashed_password" if "hashed_password" in _USERS_COLS else ("password_hash" if "password_hash" in _USERS_COLS else PW_COL)
    os.environ.setdefault("SMARTBIZ_PWHASH_COL", chosen)
    PW_COL = chosen
    logger.info("Users cols: %s | PW col: %s", sorted(_USERS_COLS or []), PW_COL)

def _users_columns() -> set[str]:
    global _USERS_COLS
    if _USERS_COLS is not None:
        return _USERS_COLS
    with suppress(Exception):
        from sqlalchemy import inspect as _sa_inspect
        _USERS_COLS = {c["name"] for c in _sa_inspect(engine).get_columns(USER_TABLE)}
    return _USERS_COLS or set()

# ────────────────────────── Middlewares ───────────────────────────────────────
class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        try:
            response: Response = await call_next(request)
        except (ClientDisconnect, anyio.EndOfStream):
            return Response(status_code=499)
        except Exception:
            logger.exception("security-mw")
            return JSONResponse(status_code=500, content={"detail": "Internal server error"})
        # basic hardening
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault("Permissions-Policy", "geolocation=(), microphone=(), camera=()")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data: https:; style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net;"
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
        response.headers["x-request-id"] = rid
        response.headers["x-process-time-ms"] = str(int((time.perf_counter() - start) * 1000))
        return response

FORCE_SAMESITE_NONE = env_bool("COOKIE_SAMESITE_NONE", True)
FORCE_HTTPONLY = env_bool("COOKIE_HTTPONLY_DEFAULT", True)

class CookiePolicyMiddleware(BaseHTTPMiddleware):
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
                    s = re.sub(r"(?i)SameSite=\w+", "SameSite=None", s) if "samesite=" in s.lower() else s + "; SameSite=None"
                    if "secure" not in s.lower():
                        s += "; Secure"
                if FORCE_HTTPONLY and "httponly" not in s.lower():
                    s += "; HttpOnly"
                cookie_values.append(s); mutated = True
        if mutated:
            headers = MutableHeaders(response.headers)
            headers.pop("set-cookie", None)
            for cv in cookie_values:
                headers.append("set-cookie", cv)
        return response

# ────────────────────────── DB helpers ────────────────────────────────────────
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

# ────────────────────────── Lifespan ──────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting SmartBiz (env=%s, db=%s)", ENVIRONMENT, _sanitize_db_url(os.getenv("DATABASE_URL", "")))
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

# ────────────────────────── App ───────────────────────────────────────────────
_docs_enabled = env_bool("ENABLE_DOCS", ENVIRONMENT != "production")
app = FastAPI(
    title=os.getenv("APP_NAME", "SmartBiz Assistance API"),
    description="SmartBiz Assistance Backend (Render + Netlify)",
    version=os.getenv("APP_VERSION", "1.0.0"),
    docs_url=None,
    redoc_url="/redoc" if _docs_enabled else None,
    openapi_url="/openapi.json" if _docs_enabled else None,
    lifespan=lifespan,
)

# ────────────────────────── Static ────────────────────────────────────────────
class _Uploads(StaticFiles):
    def is_not_modified(self, scope, request_headers, stat_result, etag=None):  # noqa: ANN001
        return super().is_not_modified(scope, request_headers, stat_result, etag)

app.mount("/uploads", _Uploads(directory=str(UPLOADS_DIR)), name="uploads")

# ────────────────────────── Exceptions ────────────────────────────────────────
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
