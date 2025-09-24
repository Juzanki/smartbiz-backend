# backend/main.py
from __future__ import annotations

import os, sys, re, json, time, uuid, logging, importlib, importlib.util, pkgutil, types
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
try:
    from starlette.middleware.proxy_headers import ProxyHeadersMiddleware as _ProxyHeadersMiddleware  # type: ignore
except Exception:
    _ProxyHeadersMiddleware = None
from starlette.requests import ClientDisconnect
from starlette.responses import JSONResponse, Response, RedirectResponse

try:
    import starlette as _st
    _STARLETTE_VER = getattr(_st, "__version__", "?")
except Exception:
    _STARLETTE_VER = "?"

# ─────────────────────── Logging ─────────────────────────
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
LOG_JSON  = (os.getenv("LOG_JSON","0").strip().lower() in {"1","true","yes","on"})

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

ENVIRONMENT = (os.getenv("ENVIRONMENT") or os.getenv("ENV") or "production").lower()

# ─────────────────────── Canonical imports ───────────────────────
def _find(spec: str) -> bool:
    with suppress(Exception):
        return importlib.util.find_spec(spec) is not None
    return False

def _hard_alias(name: str, module_obj) -> None:
    sys.modules[name] = module_obj

def ensure_canonical_packages() -> dict:
    """
    Lazimisha njia moja ya packages:
      - models: 'backend.models' ikipatikana, sivyo 'models'
      - routes: 'backend.routes' ikipatikana, sivyo 'routes'
    Na unda/alias moduli zingine zirejee kwa hiyo hiyo object (hakuna double import).
    """
    out: Dict[str, str] = {}

    if "backend" not in sys.modules:
        backend_pkg = types.ModuleType("backend")
        backend_pkg.__path__ = [str(BACKEND_DIR)]
        sys.modules["backend"] = backend_pkg

    # MODELS
    models_canon = "backend.models" if _find("backend.models") else "models"
    models_mod   = importlib.import_module(models_canon)
    _hard_alias("models", models_mod)
    _hard_alias("backend.models", models_mod)
    out["models"] = models_canon

    # ROUTES
    routes_canon = "backend.routes" if _find("backend.routes") else "routes"
    try:
        routes_mod = importlib.import_module(routes_canon)
    except Exception:
        routes_mod = types.ModuleType(routes_canon)
        routes_mod.__path__ = [str(BACKEND_DIR / "routes")]
    _hard_alias("routes", routes_mod)
    _hard_alias("backend.routes", routes_mod)
    out["routes"] = routes_canon

    logger.info("Canonical packages => models=%s routes=%s", out["models"], out["routes"])
    return out

# ====== Dedup policy: prefer “modern” filenames, alias legacy to modern =======
PREFER_OVER_LEGACY: Dict[str, str] = {
    # legacy  : modern
    "viewer": "live_viewer",
    "livestream": "live_stream",
    "cohost": "co_host",
}
LEGACY_SKIP: set[str] = set(PREFER_OVER_LEGACY.keys())

def import_all_models_once(models_pkg_name: str) -> list[str]:
    """
    Import modules za models MARA MOJA via canonical package.
    - Ikiwa zipo jozi za legacy/modern (k.m. viewer vs live_viewer), tunapendelea modern
      na KUTO-import legacy kabisa.
    - Kisha tunaweka ALIASES: 'backend.models.viewer' -> module ya 'live_viewer'
      ili imports za zamani ziende module ileile (hakuna mapper mpya).
    """
    imported: list[str] = []
    pkg = importlib.import_module(models_pkg_name)

    # 1) Skani files ili kujua zipo modern/legacy zipi
    stems: set[str] = set()
    for mod in pkgutil.walk_packages(pkg.__path__, prefix=pkg.__name__ + "."):
        stem = mod.name.rsplit(".", 1)[-1]
        stems.add(stem)

    # 2) Tengeneza set ya SKIP kwa legacy zinazopatikana pamoja na modern zake
    to_skip: set[str] = set()
    for legacy, modern in PREFER_OVER_LEGACY.items():
        if modern in stems and legacy in stems:
            to_skip.add(legacy)

    # 3) Import kwa utaratibu: ruka legacy zilizogongana
    for mod in pkgutil.walk_packages(pkg.__path__, prefix=pkg.__name__ + "."):
        name = mod.name
        stem = name.rsplit(".", 1)[-1]
        if stem.startswith("_"):
            continue
        if stem in to_skip:
            logger.warning("Skipping legacy model module %s in favor of %s", stem, PREFER_OVER_LEGACY[stem])
            continue
        if name in sys.modules:
            imported.append(name)
            continue
        try:
            importlib.import_module(name)
            imported.append(name)
        except Exception as e:
            logger.error("Model import failed: %s -> %s", name, e)

    # 4) Weka ALIASES za legacy → modern (hazita-load faili la legacy)
    for legacy, modern in PREFER_OVER_LEGACY.items():
        modern_path = f"{models_pkg_name}.{modern}"
        legacy_path = f"{models_pkg_name}.{legacy}"
        if modern_path in sys.modules:
            _hard_alias(legacy_path, sys.modules[modern_path])

    # 5) Alias pia bila-prefix kwa convenience (kama sehemu nyingine zitajaribu)
    #    Mf: sys.modules['backend.models.viewer'] tayari imewekwa juu; hapa tunahakikisha
    return imported

def _log_mapper_duplicates(Base_obj) -> Dict[str, List[str]]:
    dups: Dict[str, List[str]] = {}
    try:
        name_map: Dict[str, set[str]] = {}
        for m in Base_obj.registry.mappers:
            c = m.class_
            name_map.setdefault(c.__name__, set()).add(f"{c.__module__}.{c.__name__}")
        for k, v in name_map.items():
            if len(v) > 1:
                dups[k] = sorted(v)
        if dups:
            logger.error("SQLAlchemy duplicate mappers detected: %s", dups)
        else:
            logger.info("SQLAlchemy mappers OK (no duplicates)")
    except Exception as e:
        logger.warning("duplicate-check failed: %s", e)
    return dups

# ─────────────────────── DB imports (after canonical) ───────────────────────
PKGS = ensure_canonical_packages()
try:
    from backend.db import Base, SessionLocal, engine  # type: ignore
except Exception:
    from db import Base, SessionLocal, engine  # type: ignore

# ───────────────────── Env helpers ───────────────────────
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

# ─────────────── User table introspection (optional) ───────────────
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
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault("Permissions-Policy", "geolocation=(), microphone=(), camera=()")
        response.headers["Server"] = "SmartBiz"
        if self.enable_hsts and (request.url.scheme == "https"):
            response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains; preload")
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
            response = JSONResponse(status_code=500, content={"detail": "Internal Server Error"})
        dur_ms = (time.perf_counter() - t0) * 1000.0
        response.headers["x-request-id"] = rid
        response.headers["x-process-time-ms"] = f"{int(dur_ms)}"
        response.headers["Server-Timing"] = f"app;dur={dur_ms:.2f}"
        return response

class BodySizeLimitMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: FastAPI, max_bytes: int):
        super().__init__(app); self.max_bytes = max(0, int(max_bytes))
    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        if self.max_bytes > 0:
            cl = request.headers.get("content-length")
            with suppress(Exception):
                if cl and int(cl) > self.max_bytes:
                    return JSONResponse(status_code=413, content={"detail": "payload_too_large"})
        return await call_next(request)

# ─────────────────────── Lifespan helpers ───────────────────────
def _fail_on_duplicate_mappers_if_configured() -> None:
    dups = _log_mapper_duplicates(Base)
    if dups and (os.getenv("FAIL_ON_DUP_MAPPERS","1").strip().lower() in {"1","true","yes","on"}):
        raise RuntimeError(f"Duplicate ORM mappers detected: {dups}")

@asynccontextmanager
async def lifespan(app: FastAPI):
    # 0) Canonicalize packages and import models once
    models_pkg = ensure_canonical_packages()["models"]  # refresh just in case
    imported_models = import_all_models_once(models_pkg)
    logger.info("Models imported canonically: %s", imported_models)

    # 1) DB test
    db_ok, db_ms, db_err = _db_ping()
    logger.info(
        "Starting SmartBiz (env=%s, starlette=%s, db=%s, db_ok=%s, db_ms=%.1fms)",
        ENVIRONMENT, _STARLETTE_VER, _sanitize_db_url(os.getenv("DATABASE_URL", "")), db_ok, db_ms
    )
    if not db_ok:
        logger.error("Database connection failed at startup (%s)", db_err)

    # 2) Create tables if allowed
    if (os.getenv("AUTO_CREATE_TABLES","0").strip().lower() in {"1","true","yes","on"}) or (ENVIRONMENT != "production" and os.getenv("AUTO_CREATE_TABLES") is None):
        with suppress(Exception):
            Base.metadata.create_all(bind=engine, checkfirst=True)
            logger.info("Tables verified/created")

    # 3) Check duplicate mappers
    _fail_on_duplicate_mappers_if_configured()

    try:
        yield
    finally:
        logger.info("Shutting down SmartBiz")

# ───────────────────────── App ───────────────────────────
def env_bool(key: str, default: bool = False) -> bool:
    v = os.getenv(key); return default if v is None else v.strip().lower() in {"1","true","yes","y","on"}

def env_list(key: str) -> List[str]:
    raw = os.getenv(key, ""); return [x.strip() for x in raw.split(",") if x and x.strip()]

def _uniq(items: Iterable[Optional[str]]) -> List[str]:
    out, seen = [], set()
    for x in items:
        if x and x not in seen: out.append(x); seen.add(x)
    return out

def _sanitize_db_url(url: str) -> str:
    if not url: return ""
    return re.sub(r"://([^:@/]+):([^@/]+)@", r"://\1:****@", url)

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

# ───────────────────── Include Routers (canonical) ───────────────────
def _import_router(module_path: str):
    mod = __import__(module_path, fromlist=["router"])
    return getattr(mod, "router", None)

def _auto_include_routes():
    included = []
    routes_pkg = ensure_canonical_packages()["routes"]
    try:
        pkg = importlib.import_module(routes_pkg)
        for mod in pkgutil.walk_packages(pkg.__path__, prefix=pkg.__name__ + "."):
            name = mod.name.rsplit(".", 1)[-1]
            if not name.endswith("_routes"):
                continue
            try:
                r = _import_router(mod.name)
                if r is not None:
                    app.include_router(r)
                    included.append(mod.name)
            except Exception as e:
                logger.error("Failed to include router %s: %s", mod.name, e)
    except Exception as e:
        logger.error("Routes scan failed: %s", e)

    # Whitelist ya ziada
    names = [x.strip() for x in os.getenv("ENABLED_ROUTERS", "auth_routes").split(",") if x.strip()]
    for name in names:
        for cand in (f"{routes_pkg}.{name}", f"{routes_pkg}.{name.removesuffix('.py')}"):
            try:
                r = _import_router(cand)
                if r is not None and cand not in included:
                    app.include_router(r); included.append(cand)
                    break
            except Exception:
                continue

    logger.info("Routers included: %s", included)

_auto_include_routes()

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
    return RedirectResponse("/docs" if (os.getenv("ENABLE_DOCS","0").strip().lower() in {"1","true","yes","on"} or ENVIRONMENT!="production") else "/health", status_code=302)

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

@app.get("/auth/_diag_cors")
async def cors_diag(request: Request):
    return {
        "origin_header": request.headers.get("origin"),
        "allowed_origins": _resolve_cors_origins() if not CORS_ALLOW_ALL else ["* (regex)"],
        "auth_header_present": bool(request.headers.get("authorization") or request.headers.get("Authorization")),
    }

@app.get("/_diag/orm_registry")
def diag_orm_registry():
    try:
        info: Dict[str, set[str]] = {}
        for m in Base.registry.mappers:
            c = m.class_
            info.setdefault(c.__name__, set()).add(f"{c.__module__}.{c.__name__}")
        duplicates = {k: sorted(v) for k, v in info.items() if len(v) > 1}
        return {"ok": True, "duplicates": duplicates, "total_mapped": sum(len(v) for v in info.values())}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
