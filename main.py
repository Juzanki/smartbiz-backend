# -*- coding: utf-8 -*-
"""
SmartBiz Assistance — FastAPI entrypoint (robust & production-friendly).

- Hushughulikia mazingira mawili: ku-run kama `backend.main` AU `main` (kupitia shim ya root)
- Hutengeneza namespace packages "backend", "backend.models", "backend.routes" bila kugusa __init__.py
- Hupakia models kwa utaratibu salama (bila double-imports) kisha hu-verify SQLAlchemy mappers
- CORS inasoma var moja tu: ALLOW_ORIGINS (na ALLOW_CREDENTIALS)
- Health/info endpoints na middleware za usalama, request id, timing & gzip
"""
from __future__ import annotations

import os, sys, re, json, time, uuid, logging, importlib, pkgutil, types
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Callable, Iterable, Optional, List, Dict, Any, Tuple

# ───────────────────────── Paths ─────────────────────────
_THIS = Path(__file__).resolve()
_BACKEND_DIR = _THIS.parent
_PROJECT_ROOT = _BACKEND_DIR.parent

# Hakikisha project root ipo kwenye sys.path (hasa kwa local run)
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.append(str(_PROJECT_ROOT))

# ─────────────────────── Logging ─────────────────────────
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
LOG_JSON  = os.getenv("LOG_JSON", "0").strip().lower() in {"1","true","yes","on"}

class _JsonFmt(logging.Formatter):
    def format(self, rec: logging.LogRecord) -> str:
        out = {
            "ts": self.formatTime(rec, "%Y-%m-%dT%H:%M:%S"),
            "lvl": rec.levelname,
            "logger": rec.name,
            "msg": rec.getMessage(),
        }
        if rec.exc_info:
            out["exc"] = self.formatException(rec.exc_info)
        return json.dumps(out, ensure_ascii=False)

_handler = logging.StreamHandler()
_handler.setFormatter(_JsonFmt() if LOG_JSON else logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s"))
root_logger = logging.getLogger()
root_logger.handlers = [_handler]
root_logger.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))
log = logging.getLogger("smartbiz.main")

ENVIRONMENT = (os.getenv("ENVIRONMENT") or os.getenv("ENV") or "production").strip().lower()

# ───────────────────── Canonical namespaces ─────────────────────
# Tunaunda 'backend' kama namespace package ili imports kama `backend.models.*` zifanye kazi
def _ns_package(name: str, path: Path) -> types.ModuleType:
    mod = sys.modules.get(name)
    if mod is None:
        mod = types.ModuleType(name)
        mod.__path__ = [str(path)]  # type: ignore[attr-defined]
        sys.modules[name] = mod
    return mod

def _bootstrap_namespaces() -> None:
    # backend/
    _ns_package("backend", _BACKEND_DIR)
    # backend/models/, backend/routes/
    _ns_package("backend.models", _BACKEND_DIR / "models")
    _ns_package("backend.routes", _BACKEND_DIR / "routes")
    # Pia alias zisizovunja code za kale (optional)
    sys.modules.setdefault("models", sys.modules["backend.models"])
    sys.modules.setdefault("routes", sys.modules["backend.routes"])
    log.info("Canonical packages set: backend.models, backend.routes")

_bootstrap_namespaces()

# ───────────────────── DB imports (resilient) ─────────────────────
try:
    from backend.db import Base, SessionLocal, engine  # type: ignore
except Exception:  # local run fallback
    from db import Base, SessionLocal, engine  # type: ignore

# ───────────────────── Framework ─────────────────────
import anyio
from sqlalchemy import text
from sqlalchemy.orm import Session
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
try:
    from starlette.middleware.proxy_headers import ProxyHeadersMiddleware as _Proxy  # 0.37.x
except Exception:
    _Proxy = None
from starlette.requests import ClientDisconnect
from starlette.responses import JSONResponse, Response, RedirectResponse

try:
    import starlette as _st
    _STARLETTE_VER = getattr(_st, "__version__", "?")
except Exception:
    _STARLETTE_VER = "?"

# ───────────────────── Env helpers ─────────────────────
def env_bool(key: str, default: bool = False) -> bool:
    v = os.getenv(key)
    return default if v is None else v.strip().lower() in {"1","true","yes","on","y"}

def env_list(key: str) -> List[str]:
    raw = os.getenv(key, "")
    return [x.strip() for x in raw.split(",") if x.strip()]

def _sanitize_db_url(url: str) -> str:
    if not url:
        return ""
    return re.sub(r"://([^:@/]+):([^@/]+)@", r"://\1:****@", url)

# ───────────────────── DB helpers ─────────────────────
def _db_ping() -> Tuple[bool, float, str]:
    t0 = time.perf_counter()
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True, (time.perf_counter() - t0) * 1000.0, ""
    except Exception as e:
        return False, (time.perf_counter() - t0) * 1000.0, type(e).__name__

def get_db() -> Session:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# ───────────────────── Middleware ─────────────────────
class SecurityHeaders(BaseHTTPMiddleware):
    def __init__(self, app: FastAPI, *, enable_hsts: bool = False):
        super().__init__(app); self.enable_hsts = enable_hsts
    async def dispatch(self, request: Request, call_next):
        rid = request.headers.get("x-request-id") or "-"
        try:
            resp: Response = await call_next(request)
        except (ClientDisconnect, anyio.EndOfStream):
            return Response(status_code=499)
        except Exception:
            log.exception("security-mw xrid=%s", rid)
            return JSONResponse(status_code=500, content={"detail": "Internal server error"})
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        resp.headers.setdefault("Permissions-Policy", "geolocation=(), microphone=(), camera=()")
        resp.headers["Server"] = "SmartBiz"
        if self.enable_hsts and (request.url.scheme == "https"):
            resp.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains; preload")
        return resp

class RequestIDAndTiming(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        rid = request.headers.get("x-request-id") or uuid.uuid4().hex
        request.state.request_id = rid
        t0 = time.perf_counter()
        try:
            resp: Response = await call_next(request)
        except (ClientDisconnect, anyio.EndOfStream):
            resp = Response(status_code=499)
        except Exception:
            log.exception("unhandled xrid=%s", rid)
            resp = JSONResponse(status_code=500, content={"detail": "Internal Server Error"})
        dur_ms = (time.perf_counter() - t0) * 1000.0
        resp.headers["x-request-id"] = rid
        resp.headers["x-process-time-ms"] = f"{int(dur_ms)}"
        resp.headers["Server-Timing"] = f"app;dur={dur_ms:.2f}"
        return resp

# ───────────────────── Models loading (no double-imports) ─────────────────────
def _iter_modules(pkg_name: str) -> List[str]:
    pkg = sys.modules[pkg_name]
    out: List[str] = []
    for mod in pkgutil.walk_packages(pkg.__path__, prefix=pkg_name + "."):  # type: ignore[attr-defined]
        base = mod.name.rsplit(".", 1)[-1]
        if not base.startswith("_"):
            out.append(mod.name)
    return sorted(out)

def _import_all_model_modules(pkg_name: str) -> List[str]:
    imported: List[str] = []
    for fq in _iter_modules(pkg_name):
        if fq in sys.modules:
            imported.append(fq); continue
        try:
            importlib.import_module(fq)
            imported.append(fq)
        except Exception as e:
            log.error("Model import failed: %s → %s", fq, e)
    return imported

def _check_mapper_duplicates() -> Dict[str, List[str]]:
    dups: Dict[str, List[str]] = {}
    try:
        name_map: Dict[str, set[str]] = {}
        for m in Base.registry.mappers:
            c = m.class_
            name_map.setdefault(c.__name__, set()).add(f"{c.__module__}.{c.__name__}")
        for k, v in name_map.items():
            if len(v) > 1:
                dups[k] = sorted(v)
        if dups:
            log.error("SQLAlchemy duplicate mappers: %s", dups)
        else:
            log.info("SQLAlchemy mappers OK (no duplicates)")
    except Exception as e:
        log.warning("mapper-dup-check failed: %s", e)
    return dups

# ───────────────────── Lifespan ─────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    # 1) Load models once via canonical namespace
    loaded = _import_all_model_modules("backend.models")
    log.info("Models loaded: %s", loaded)

    # 2) DB ping
    ok, ms, err = _db_ping()
    log.info("Starting SmartBiz (env=%s, starlette=%s, db_ok=%s, db_ms=%.1f, db=%s)",
             ENVIRONMENT, _STARLETTE_VER, ok, ms, _sanitize_db_url(os.getenv("DATABASE_URL","")))
    if not ok:
        log.error("Database ping failed at startup (%s)", err)

    # 3) Auto-create tables (unless disabled)
    if env_bool("AUTO_CREATE_TABLES", default=(ENVIRONMENT != "production")):
        with suppress(Exception):
            Base.metadata.create_all(bind=engine, checkfirst=True)
            log.info("Tables verified/created")

    # 4) Mapper sanity
    if env_bool("FAIL_ON_DUP_MAPPERS", True):
        d = _check_mapper_duplicates()
        if d:
            raise RuntimeError(f"Duplicate ORM mappers detected: {d}")

    try:
        yield
    finally:
        log.info("Shutting down SmartBiz")

# ───────────────────── App ─────────────────────
_docs_enabled = env_bool("ENABLE_DOCS", default=(ENVIRONMENT != "production"))
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
if env_bool("ENABLE_PROXY_HEADERS", True) and _Proxy:
    app.add_middleware(_Proxy, trusted_hosts="*")
elif env_bool("ENABLE_PROXY_HEADERS", True) and not _Proxy:
    log.warning("ProxyHeadersMiddleware missing (starlette=%s); using Uvicorn --proxy-headers.", _STARLETTE_VER)

hosts = env_list("ALLOWED_HOSTS")
if hosts and hosts != ["*"]:
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=hosts)

# CORS — tumia ALLOW_ORIGINS (+ ALLOW_CREDENTIALS)
allow_origins = env_list("ALLOW_ORIGINS") or [
    "https://smartbizsite.netlify.app",
    "https://smartbiz.site", "https://www.smartbiz.site",
    "http://localhost:5173", "http://127.0.0.1:5173",
]
allow_credentials = env_bool("ALLOW_CREDENTIALS", True)
if env_bool("CORS_ALLOW_ALL", False):
    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=".*",
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["set-cookie", "x-request-id"],
        max_age=600,
    )
    log.warning("CORS_ALLOW_ALL=1 (dev mode)")
else:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allow_origins,
        allow_credentials=allow_credentials,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["set-cookie", "x-request-id"],
        max_age=600,
    )

# Compression & security & timing
app.add_middleware(GZipMiddleware, minimum_size=1024)
app.add_middleware(SecurityHeaders, enable_hsts=env_bool("ENABLE_HSTS", True))
app.add_middleware(RequestIDAndTiming)

# ───────────────────── Router auto-include ─────────────────────
def _include_routes():
    included: List[str] = []
    for pkg_name in ("backend.routes", "routes"):  # scan canonical then alias
        try:
            pkg = sys.modules[pkg_name]
            for mod in pkgutil.walk_packages(pkg.__path__, prefix=pkg_name + "."):  # type: ignore[attr-defined]
                name = mod.name.rsplit(".", 1)[-1]
                if not name.endswith("_routes"):
                    continue
                try:
                    mod_obj = importlib.import_module(mod.name)
                    router = getattr(mod_obj, "router", None)
                    if router is not None:
                        app.include_router(router)
                        included.append(mod.name)
                except Exception as e:
                    log.error("Failed to include router %s: %s", mod.name, e)
        except Exception:
            pass

    # Whitelist ya ziada
    extra = [x.strip() for x in os.getenv("ENABLED_ROUTERS", "auth_routes").split(",") if x.strip()]
    for name in extra:
        for base in ("backend.routes", "routes"):
            modpath = f"{base}.{name}"
            if modpath in included:
                continue
            try:
                m = importlib.import_module(modpath)
                r = getattr(m, "router", None)
                if r is not None:
                    app.include_router(r); included.append(modpath)
            except Exception:
                pass

    log.info("Routers included: %s", included)

_include_routes()

# ───────────────────── Error handlers ─────────────────────
@app.exception_handler(HTTPException)
async def _http_exc(_: Request, exc: HTTPException):
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

@app.exception_handler(RequestValidationError)
async def _val_exc(_: Request, exc: RequestValidationError):
    return JSONResponse(status_code=422, content={"detail": "validation_error", "errors": exc.errors()})

@app.exception_handler(Exception)
async def _unhandled(request: Request, exc: Exception):
    rid = getattr(getattr(request, "state", None), "request_id", "-")
    log.exception("unhandled-exception xrid=%s", rid)
    return JSONResponse(status_code=500, content={"detail": "internal_error", "xrid": rid})

# ───────────────────── Routes ─────────────────────
@app.get("/")
def _root():
    return RedirectResponse("/docs" if _docs_enabled else "/health", status_code=302)

@app.get("/health")
@app.head("/health")
def _health():
    ok, ms, err = _db_ping()
    # libs check
    with suppress(Exception):
        import passlib  # noqa
    have_bcrypt = True
    try:
        import bcrypt  # noqa
    except Exception:
        have_bcrypt = False
    git_sha = os.getenv("RENDER_GIT_COMMIT") or os.getenv("GIT_SHA") or ""
    return {
        "status": "ok" if ok else "degraded",
        "db_ok": ok, "db_ms": ms, "db_err": err,
        "env": ENVIRONMENT,
        "starlette": _STARLETTE_VER,
        "git_sha": git_sha,
        "bcrypt": have_bcrypt,
        "ts": time.time(),
    }

@app.get("/readyz")
def _ready():
    ok, ms, _ = _db_ping()
    return {"ready": ok, "db_ms": ms}

# Local dev entrypoint (optional)
if __name__ == "__main__":  # pragma: no cover
    import uvicorn
    uvicorn.run(
        "backend.main:app",
        host=os.getenv("HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", "8000")),
        reload=env_bool("RELOAD", default=(ENVIRONMENT != "production")),
        proxy_headers=True,
        forwarded_allow_ips="*",
    )
