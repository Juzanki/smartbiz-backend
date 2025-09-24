# backend/main.py
from __future__ import annotations

import os, sys, re, json, time, uuid, logging
from pathlib import Path
from contextlib import asynccontextmanager, suppress
from typing import Callable, Iterable, Optional, List, Dict, Any, Tuple

# ───────────────────────── Paths ─────────────────────────
THIS_FILE = Path(__file__).resolve()
BACKEND_DIR = THIS_FILE.parent
ROOT_DIR = BACKEND_DIR.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.append(str(ROOT_DIR))

BACKEND_PUBLIC_URL = (os.getenv("BACKEND_PUBLIC_URL", "https://smartbiz-backend-p45m.onrender.com") or "").rstrip("/")

# ─────────────────────── Framework ───────────────────────
import anyio
from fastapi import FastAPI, HTTPException, Request, Depends
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.openapi.docs import get_swagger_ui_html, get_swagger_ui_oauth2_redirect_html
from sqlalchemy import text
from sqlalchemy.orm import Session
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
# Proxy headers: ipo kwenye Starlette mpya tu
try:
    from starlette.middleware.proxy_headers import ProxyHeadersMiddleware as _ProxyHeadersMiddleware  # type: ignore
except Exception:
    _ProxyHeadersMiddleware = None
from starlette.requests import ClientDisconnect
from starlette.responses import JSONResponse, Response, RedirectResponse

# (hiari) version logging
try:
    import starlette as _st
    _STARLETTE_VER = getattr(_st, "__version__", "?")
except Exception:
    _STARLETTE_VER = "?"

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
    if not url:
        return ""
    return re.sub(r"://([^:@/]+):([^@/]+)@", r"://\1:****@", url)

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
    if _USERS_COLS is not None:
        return _USERS_COLS
    with suppress(Exception):
        from sqlalchemy import inspect as _sa_inspect
        _USERS_COLS = {c["name"] for c in _sa_inspect(engine).get_columns(USER_TABLE)}
    return _USERS_COLS or set()

# ───────────────────── DB helpers ──────────────────────
def _db_ping() -> Tuple[bool, float, str]:
    t0 = time.perf_counter()
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True, (time.perf_counter() - t0) * 1000.0, ""
    except Exception as e:
        return False, (time.perf_counter() - t0) * 1000.0, f"{type(e).__name__}"

def get_db() -> Session:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# ───────────────────── Middlewares ───────────────────────
class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: FastAPI, *, enable_hsts: bool = False):
        super().__init__(app)
        self.enable_hsts = enable_hsts

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        rid = request.headers.get("x-request-id") or "-"
        try:
            response: Response = await call_next(request)
        except (ClientDisconnect, anyio.EndOfStream):
            return Response(status_code=499)
        except Exception:
            logger.exception("security-mw xrid=%s", rid)
            return JSONResponse(status_code=500, content={"detail": "Internal server error"})
        response.headers.setdefault("X-Content-Type-Options","nosniff")
        response.headers.setdefault("X-Frame-Options","DENY")
        response.headers.setdefault("Referrer-Policy","strict-origin-when-cross-origin")
        response.headers.setdefault("Permissions-Policy","geolocation=(), microphone=(), camera=()")
        response.headers["Server"] = "SmartBiz"
        if self.enable_hsts and (request.url.scheme == "https"):
            response.headers.setdefault("Strict-Transport-Security","max-age=31536000; includeSubDomains; preload")
        return response

class RequestIDAndTimingMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        rid = request.headers.get("x-request-id") or uuid.uuid4().hex
        request.state.request_id = rid
        t0 = time.perf_counter()
        try:
            response: Response = await call_next(request)
        except (ClientDisconnect, anyio.EndOfStream):
            response = Response(status_code=499)
        except Exception:
            logger.exception("unhandled xrid=%s", rid)
            response = JSONResponse(status_code=500, content={"detail":"Internal Server Error"})
        dur_ms = (time.perf_counter() - t0) * 1000.0
        response.headers["x-request-id"] = rid
        response.headers["x-process-time-ms"] = f"{int(dur_ms)}"
        response.headers["Server-Timing"] = f"app;dur={dur_ms:.2f}"
        return response

class BodySizeLimitMiddleware(BaseHTTPMiddleware):
    """Reject requests that declare a Content-Length larger than MAX_BODY_BYTES (optional)."""
    def __init__(self, app: FastAPI, max_bytes: int):
        super().__init__(app)
        self.max_bytes = max(0, int(max_bytes))

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        if self.max_bytes > 0:
            cl = request.headers.get("content-length")
            with suppress(Exception):
                if cl and int(cl) > self.max_bytes:
                    return JSONResponse(status_code=413, content={"detail": "payload_too_large"})
        return await call_next(request)

# --- juu ya file (imports zingine zipo tayari) ---
import importlib
import sys

# ================== EXTRA: Canonical module aliases (ROBUST) ==================
# Lengo: kuruhusu 'backend.routes.*' na 'backend.models.*' kufanya kazi hata bila folder 'backend/'
with suppress(Exception):
    import types
    # 1) Hakikisha 'routes' & 'models' zinaweza ku-importiwa
    if (BACKEND_DIR / "routes").exists():
        try:
            sys.modules.setdefault("routes", importlib.import_module("routes"))
        except Exception:
            pass
    if (BACKEND_DIR / "models").exists():
        try:
            sys.modules.setdefault("models", importlib.import_module("models"))
        except Exception:
            pass

    # 2) Tengeneza package ya bandia 'backend' ikiwa haipo
    if "backend" not in sys.modules:
        backend_pkg = types.ModuleType("backend")
        backend_pkg.__path__ = [str(BACKEND_DIR)]  # iwe package halali
        sys.modules["backend"] = backend_pkg

    # 3) Alias subpackages: backend.routes → routes, backend.models → models
    if "routes" in sys.modules:
        sys.modules.setdefault("backend.routes", sys.modules["routes"])
    if "models" in sys.modules:
        sys.modules.setdefault("backend.models", sys.modules["models"])
# ==============================================================================

# ================== EXTRA: weak alias (ok to keep; no harm) ===================
with suppress(Exception):
    # Ikiwa 'backend.models' tayari ime-load, alias 'models' → 'backend.models'
    if "backend.models" in sys.modules:
        sys.modules.setdefault("models", sys.modules["backend.models"])
with suppress(Exception):
    if "backend.routes" in sys.modules:
        sys.modules.setdefault("routes", sys.modules["backend.routes"])
# ==============================================================================

# ─────────────────────── Lifespan helpers ───────────────────────
def _import_all_models_canonically() -> list[str]:
    """
    Import all model modules ONLY via 'backend.models.<name>'.
    Also alias 'models' -> 'backend.models' to avoid double imports.
    Returns the list of imported module paths.
    """
    imported: list[str] = []

    models_pkg = BACKEND_DIR / "models"
    if models_pkg.exists():
        # Ensure package ipo na ime-load kama 'backend.models'
        importlib.import_module("backend.models")

        # Alias: kitu chochote kinacho-import 'models' kipate object ile ile
        sys.modules.setdefault("models", sys.modules["backend.models"])

        for f in sorted(models_pkg.glob("*.py")):
            name = f.stem
            if name.startswith("_"):
                continue
            mod_path = f"backend.models.{name}"
            try:
                importlib.import_module(mod_path)
                imported.append(mod_path)
            except Exception as e:
                logger.error("Failed to import model %s: %s", mod_path, e)
    else:
        logger.warning("No backend/models directory found")

    return imported

def _log_mapper_duplicates() -> None:
    """Ongeza ufuatiliaji: toa onyo kama kuna majina ya mappers yamejirudia."""
    try:
        name_map: Dict[str, set[str]] = {}
        for m in Base.registry.mappers:
            cls = m.class_
            name_map.setdefault(cls.__name__, set()).add(f"{cls.__module__}.{cls.__name__}")
        dups = {k: sorted(v) for k, v in name_map.items() if len(v) > 1}
        if dups:
            logger.error("SQLAlchemy duplicate mappers detected: %s", dups)
        else:
            logger.info("SQLAlchemy mappers OK (no duplicate names)")
    except Exception as e:
        logger.debug("mapper-duplicate-check skipped: %s", e)

# ─────────────────────── Lifespan ────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    db_ok, db_ms, db_err = _db_ping()
    logger.info(
        "Starting SmartBiz (env=%s, starlette=%s, db=%s, db_ok=%s, db_ms=%.1fms)",
        ENVIRONMENT, _STARLETTE_VER, _sanitize_db_url(os.getenv("DATABASE_URL", "")), db_ok, db_ms
    )
    if not db_ok:
        logger.error("Database connection failed at startup (%s)", db_err)

    # 1) Pakia models kwa njia moja tu (canonical) kabla ya create_all
    with suppress(Exception):
        imported_models = _import_all_models_canonically()
        logger.info("Models imported: %s", imported_models)

    # 2) Unda/ihakiki jedwali
    if env_bool("AUTO_CREATE_TABLES", ENVIRONMENT != "production"):
        with suppress(Exception):
            Base.metadata.create_all(bind=engine, checkfirst=True)
            logger.info("Tables verified/created")

    # 3) Angalia marudio ya mappers (helpful kwa 500 za 'Multiple classes found...')
    with suppress(Exception):
        _log_mapper_duplicates()

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
    docs_url=None,
    redoc_url="/redoc" if _docs_enabled else None,
    openapi_url="/openapi.json" if _docs_enabled else None,
    lifespan=lifespan,
)

# Proxy/Host safety
ENABLE_PROXY_HEADERS = env_bool("ENABLE_PROXY_HEADERS", True)
if ENABLE_PROXY_HEADERS and _ProxyHeadersMiddleware:
    app.add_middleware(_ProxyHeadersMiddleware, trusted_hosts="*")
elif ENABLE_PROXY_HEADERS and not _ProxyHeadersMiddleware:
    logger.warning("ProxyHeadersMiddleware missing (starlette=%s). Using Uvicorn --proxy-headers.", _STARLETTE_VER)

ALLOWED_HOSTS = env_list("ALLOWED_HOSTS") or ["*"]
if ALLOWED_HOSTS != ["*"]:
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=ALLOWED_HOSTS)

# CORS
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
        expose_headers=["set-cookie", "x-request-id"],
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
        expose_headers=["set-cookie", "x-request-id"],
        max_age=600,
    )

# Compression + Security + Request ID + Body-limit
app.add_middleware(GZipMiddleware, minimum_size=1024)
app.add_middleware(SecurityHeadersMiddleware, enable_hsts=env_bool("ENABLE_HSTS", True))
app.add_middleware(RequestIDAndTimingMiddleware)
max_body = int(os.getenv("MAX_BODY_BYTES", "0") or "0")
if max_body > 0:
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=max_body)

# ───────────────────── Include Routers ───────────────────
def _import_router(module_path: str):
    mod = __import__(module_path, fromlist=["router"])
    return getattr(mod, "router", None)

def _auto_include_routes():
    candidates: List[str] = []
    search_dirs = [BACKEND_DIR / "routes", ROOT_DIR / "routes"]
    for d in search_dirs:
        if not d.exists():
            continue
        for p in d.glob("*_routes.py"):
            module_path = f"backend.routes.{p.stem}" if d == BACKEND_DIR / "routes" else f"routes.{p.stem}"
            candidates.append(module_path)

    included = []
    for mod in candidates:
        try:
            r = _import_router(mod)
            if r is not None:
                app.include_router(r)
                included.append(mod)
        except Exception as e:
            logger.error("Failed to include router %s: %s", mod, e)

    if not included:
        with suppress(Exception):
            from backend.routes.auth_routes import router as _r1
            app.include_router(_r1)
            included.append("backend.routes.auth_routes")
        with suppress(Exception):
            from routes.auth_routes import router as _r2
            app.include_router(_r2)
            included.append("routes.auth_routes")

    logger.info("Routers included: %s", included or [])

# -------- EXTRA: Router bootstrap (phase 2 via whitelist) --------
def _import_router_variants(name: str):
    """
    Jaribu import routes.<name> kisha backend.routes.<name>.
    Inarudisha (router, origin_module) au (None, None)
    """
    for mod in (f"routes.{name}", f"backend.routes.{name}"):
        try:
            pkg = __import__(mod, fromlist=["router"])
            return getattr(pkg, "router", None), mod
        except Exception:
            continue
    return None, None

def _include_whitelisted_routes():
    """
    Whitelist ya ziada kupitia env ENABLED_ROUTERS (comma-separated).
    Mfano: ENABLED_ROUTERS=auth_routes,comment_routes
    """
    names = [x.strip() for x in os.getenv("ENABLED_ROUTERS", "auth_routes").split(",") if x.strip()]
    included2 = []
    for name in names:
        r, origin = _import_router_variants(name)
        if r is None:
            logger.error("Whitelist include failed for %s (tried routes.%s / backend.routes.%s)", name, name, name)
            continue
        try:
            app.include_router(r)
            included2.append(origin or name)
        except Exception as e:
            logger.error("Whitelist include crashed for %s from %s: %s", name, origin, e)
    if included2:
        logger.info("Whitelist routers included (phase2): %s", included2)
# ------------------------------------------------------------------

_auto_include_routes()
_include_whitelisted_routes()  # ← inaongeza juu ya ya awali bila kubadilisha chochote

# ───────────────── Global error handlers ─────────────────
@app.exception_handler(HTTPException)
async def http_exc_handler(_: Request, exc: HTTPException):
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

@app.exception_handler(RequestValidationError)
async def validation_exc_handler(_: Request, exc: RequestValidationError):
    return JSONResponse(status_code=422, content={"detail": "validation_error", "errors": exc.errors()})

@app.exception_handler(Exception)
async def unhandled_exc_handler(request: Request, exc: Exception):
    rid = getattr(getattr(request, "state", None), "request_id", "-")
    logger.exception("unhandled-exception xrid=%s", rid)
    return JSONResponse(status_code=500, content={"detail": "internal_error", "xrid": rid})

# ───────────────────────── Routes ─────────────────────────
@app.get("/")
def root_redirect():
    return RedirectResponse("/docs" if _docs_enabled else "/health", status_code=302)

@app.get("/health")
@app.head("/health")
@app.get("/api/health")
def health():
    db_ok, db_ms, db_err = _db_ping()
    try:
        import passlib  # type: ignore
        _passlib = True
    except Exception:
        _passlib = False
    try:
        import bcrypt  # type: ignore
        _bcrypt = True
    except Exception:
        _bcrypt = False

    git_sha = os.getenv("RENDER_GIT_COMMIT") or os.getenv("GIT_SHA") or ""
    return {
        "status": "healthy",
        "db": {"ok": db_ok, "latency_ms": int(db_ms), "err": db_err or None},
        "deps": {"passlib": _passlib, "bcrypt": _bcrypt},
        "ts": time.time(),
        "env": {
            "environment": ENVIRONMENT,
            "starlette": _STARLETTE_VER,
            "app_version": os.getenv("APP_VERSION", "1.0.0"),
            "log_json": LOG_JSON,
            "allowed_hosts": ALLOWED_HOSTS,
            "cors_allow_all": CORS_ALLOW_ALL,
            "allowed_origins": ALLOW_ORIGINS if not CORS_ALLOW_ALL else ["*"],
            "db_url": _sanitize_db_url(os.getenv("DATABASE_URL", "")),
            "git_sha": git_sha,
        },
        "base_url": BACKEND_PUBLIC_URL,
    }

@app.get("/health/db")
def health_db(db: Session = Depends(get_db)):
    t0 = time.perf_counter()
    try:
        db.execute(text("SELECT 1"))
        return {"ok": True, "latency_ms": int((time.perf_counter() - t0) * 1000)}
    except Exception as e:
        logger.exception("DB health failed")
        return JSONResponse({"ok": False, "error": type(e).__name__}, status_code=500)

# CORS diagnostics (optional)
@app.get("/auth/_diag_cors")
async def cors_diag(request: Request):
    return {
        "origin_header": request.headers.get("origin"),
        "allowed_origins": ALLOW_ORIGINS if not CORS_ALLOW_ALL else ["* (regex)"],
        "auth_header_present": bool(request.headers.get("authorization") or request.headers.get("Authorization")),
    }

# ================== EXTRA: Main diagnostics ==================
DIAG_MAIN_ENABLED = env_bool("DIAG_MAIN_ENABLED", False)

@app.get("/_diag/routers")
def diag_routers():
    if not DIAG_MAIN_ENABLED:
        raise HTTPException(status_code=404, detail="not_enabled")
    paths = sorted({getattr(r, "path", None) for r in app.routes if hasattr(r, "path") and getattr(r, "path")})
    return {
        "count": len(paths),
        "paths": paths[:300],
        "whitelist": [x.strip() for x in os.getenv("ENABLED_ROUTERS","auth_routes").split(",") if x.strip()],
    }

@app.get("/_diag/orm_registry")
def diag_orm_registry():
    if not DIAG_MAIN_ENABLED:
        raise HTTPException(status_code=404, detail="not_enabled")
    try:
        names: Dict[str, set[str]] = {}
        for m in Base.registry.mappers:
            cls = m.class_
            names.setdefault(cls.__name__, set()).add(f"{cls.__module__}.{cls.__name__}")
        duplicates = {k: sorted(v) for k, v in names.items() if len(v) > 1}
        return {
            "ok": True,
            "duplicates": duplicates,
            "total_mapped": sum(len(v) for v in names.values()),
        }
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
# =============================================================

# Docs (optional)
if _docs_enabled:
    @app.get("/docs", include_in_schema=False)
    async def custom_swagger_ui_html():
        return get_swagger_ui_html(openapi_url=app.openapi_url, title="SmartBiz API Docs")

    @app.get("/docs/oauth2-redirect", include_in_schema=False)
    async def swagger_ui_redirect():
        return get_swagger_ui_oauth2_redirect_html()
