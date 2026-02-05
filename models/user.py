# backend/main.py
# -*- coding: utf-8 -*-
from __future__ import annotations

"""
SmartBiz Assistance — Main ASGI Entrypoint (Production-Grade, Render-Safe)
=========================================================================
v13.0.3 (ANNOUNCEMENTS GUARANTEE FIXED • INDENT SAFE • ROUTE-DEDUPED)

KEY UPGRADES:
✅ FIX: Announcements guarantee block is now correctly inside create_app() (no SyntaxError)
✅ Keeps your existing CORS guard + security middlewares + diagnostics + router guarantees
✅ Path-fallback router loader preserved
"""

import os
import re
import sys
import time
import json
import uuid
import types
import anyio
import logging
import inspect
import importlib
import importlib.util as _importlib_util
import pkgutil as _pkgutil
import traceback
from pathlib import Path
from typing import Dict, List, Tuple, Callable, Any, Optional, Set
from contextlib import asynccontextmanager, suppress

from backend.middleware.cors import setup_cors as setup_cors_guarded

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.middleware.cors import CORSMiddleware

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.responses import JSONResponse, RedirectResponse, Response, PlainTextResponse
from starlette.requests import ClientDisconnect


# Optional proxy middleware (only if installed)
try:
    from starlette.middleware.proxy_headers import ProxyHeadersMiddleware  # type: ignore
    _HAS_PROXY_MW = True
except Exception:  # pragma: no cover
    ProxyHeadersMiddleware = None  # type: ignore
    _HAS_PROXY_MW = False


# =====================================================================
# Path / namespace bootstrap (Render-safe)
# =====================================================================

_THIS = Path(__file__).resolve()
_BACKEND = _THIS.parent
_ROOT = _BACKEND.parent

if str(_ROOT) not in sys.path:
    sys.path.append(str(_ROOT))


def _ensure_pkg_ns(name: str, path: Path) -> types.ModuleType:
    mod = sys.modules.get(name)
    if mod is None:
        mod = types.ModuleType(name)
        mod.__path__ = [str(path)]  # type: ignore[attr-defined]
        sys.modules[name] = mod
    return mod


_ensure_pkg_ns("backend", _BACKEND)
_ensure_pkg_ns("backend.models", _BACKEND / "models")
_ensure_pkg_ns("backend.routes", _BACKEND / "routes")
_ensure_pkg_ns("backend.routers", _BACKEND / "routers")

# Legacy aliases (avoid breaking older imports)
sys.modules.setdefault("models", sys.modules["backend.models"])
sys.modules.setdefault("routes", sys.modules["backend.routes"])
sys.modules.setdefault("routers", sys.modules["backend.routers"])


# =====================================================================
# Env helpers
# =====================================================================

def _env_bool(k: str, default: bool = False) -> bool:
    v = os.getenv(k)
    return default if v is None else v.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(k: str, default: int) -> int:
    try:
        return int((os.getenv(k) or "").strip() or default)
    except Exception:
        return default


def _env_list(k: str) -> List[str]:
    raw = os.getenv(k, "") or ""
    return [x.strip() for x in raw.split(",") if x.strip()]


ENV = (os.getenv("ENVIRONMENT") or os.getenv("ENV") or "production").strip().lower()


def _default_import_all_models() -> bool:
    return False if ENV == "production" else True


# =====================================================================
# Logging (safe JSON)
# =====================================================================

LOG_JSON = (os.getenv("LOG_JSON", "0") or "").strip().lower() in {"1", "true", "yes", "on"}
LOG_LEVEL = getattr(logging, (os.getenv("LOG_LEVEL", "INFO") or "INFO").upper(), logging.INFO)


class _JsonFmt(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:  # type: ignore[override]
        out: Dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            out["exc"] = self.formatException(record.exc_info)
        return json.dumps(out, ensure_ascii=False)


_handler = logging.StreamHandler()
_handler.setFormatter(
    _JsonFmt() if LOG_JSON else logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s")
)
root_logger = logging.getLogger()
root_logger.handlers = [_handler]
root_logger.setLevel(LOG_LEVEL)

log = logging.getLogger("smartbiz.main")
routes_log = logging.getLogger("smartbiz.routes")

try:
    import starlette as _st
    STARLETTE_VER = getattr(_st, "__version__", "?")
except Exception:  # pragma: no cover
    STARLETTE_VER = "?"


# =====================================================================
# DB glue (robust)
# =====================================================================

Base = None  # type: ignore[assignment]
SessionLocal = None  # type: ignore[assignment]
engine = None  # type: ignore[assignment]


def datetime_now_utc():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc)


def datetime_utc_iso() -> str:
    return datetime_now_utc().isoformat()


def _noop(*args, **kwargs):
    return True


warm_db = _noop
reload_engine_from_env = _noop


def _sanitize_db(url: str) -> str:
    if not url:
        return ""
    return re.sub(r"://([^:@/]+):([^@/]+)@", r"://\1:****@", url)


_HAS_DB_HEALTHCHECK = False
_db_healthcheck_fn = None

try:
    from backend.db import (  # type: ignore
        Base as _Base,
        SessionLocal as _SessionLocal,
        engine as _engine,
        warm_db as _warm_db,
        reload_engine_from_env as _reload_engine_from_env,
    )
    Base = _Base
    SessionLocal = _SessionLocal
    engine = _engine
    warm_db = _warm_db
    reload_engine_from_env = _reload_engine_from_env

    try:
        from backend.db import db_healthcheck as _db_hc  # type: ignore
        _HAS_DB_HEALTHCHECK = True
        _db_healthcheck_fn = _db_hc
    except Exception:
        _HAS_DB_HEALTHCHECK = False
        _db_healthcheck_fn = None

except Exception as e:
    raise RuntimeError(f"❌ DB glue not found. Ensure backend/db.py exports Base, SessionLocal, engine. ({e})")

from sqlalchemy import text
from sqlalchemy.orm import Session


def get_db():
    db: Session = SessionLocal()  # type: ignore[misc]
    try:
        yield db
    finally:
        db.close()


def _db_ping() -> Tuple[bool, float, str]:
    t0 = time.perf_counter()
    try:
        with engine.connect() as conn:  # type: ignore[union-attr]
            conn.execute(text("SELECT 1"))
        return True, (time.perf_counter() - t0) * 1000.0, ""
    except Exception as e:
        return False, (time.perf_counter() - t0) * 1000.0, f"{type(e).__name__}: {str(e)[:220]}"


def _db_health_fallback() -> Dict[str, Any]:
    ok, ms, err = _db_ping()
    return {"ok": ok, "ms": ms, "error": err, "time_utc": datetime_utc_iso()}


def db_healthcheck() -> Dict[str, Any]:
    if _HAS_DB_HEALTHCHECK and callable(_db_healthcheck_fn):
        try:
            return _db_healthcheck_fn()  # type: ignore[misc]
        except Exception:
            return _db_health_fallback()
    return _db_health_fallback()


# =====================================================================
# Deterministic model import (registry-first) + fail-fast mapper config
# =====================================================================

with suppress(Exception):
    import backend.models  # noqa: F401

with suppress(Exception):
    from backend.models.registry import load_all_models  # type: ignore
    load_all_models()
    log.info("✅ load_all_models(): models registry loaded early")

_MAPPERS_OK = False
_MAPPERS_ERR = ""
_MAPPERS_ERR_FULL = ""
_MAPPERS_TRIGGER = ""
_MAPPERS_ORIGINAL = ""
_MAPPERS_TS = 0.0
_MAPPERS_TRACE = ""


def _extract_mapper_root(msg: str) -> Tuple[str, str]:
    trigger = ""
    original = ""
    s = msg or ""

    if "Triggering mapper:" in s:
        part = s.split("Triggering mapper:", 1)[1].strip()
        if ". Original exception was:" in part:
            trigger = part.split(". Original exception was:", 1)[0].strip()
        else:
            trigger = part.strip()

    if "Original exception was:" in s:
        original = s.split("Original exception was:", 1)[1].strip()

    return trigger, original


def _configure_mappers_failfast() -> None:
    global _MAPPERS_OK, _MAPPERS_ERR, _MAPPERS_ERR_FULL, _MAPPERS_TRIGGER, _MAPPERS_ORIGINAL, _MAPPERS_TS, _MAPPERS_TRACE
    _MAPPERS_TS = time.time()
    _MAPPERS_TRACE = ""

    try:
        from sqlalchemy.orm import configure_mappers
        configure_mappers()
        _MAPPERS_OK = True
        _MAPPERS_ERR = ""
        _MAPPERS_ERR_FULL = ""
        _MAPPERS_TRIGGER = ""
        _MAPPERS_ORIGINAL = ""
        return

    except Exception as e:
        _MAPPERS_OK = False
        full = str(e) or repr(e)
        short = f"{type(e).__name__}: {full}"
        _MAPPERS_ERR = short[:900]
        _MAPPERS_ERR_FULL = short

        trig, orig = _extract_mapper_root(short)
        _MAPPERS_TRIGGER = trig
        _MAPPERS_ORIGINAL = orig[:2000] if orig else ""

        _MAPPERS_TRACE = "".join(traceback.format_exception(type(e), e, e.__traceback__))

        log.error("❌ SQLAlchemy mapper configure FAILED")
        if _MAPPERS_TRIGGER:
            log.error("➡️ Triggering mapper: %s", _MAPPERS_TRIGGER)
        if _MAPPERS_ORIGINAL:
            log.error("➡️ Original exception: %s", _MAPPERS_ORIGINAL[:900])
        log.error("➡️ Hint: open /__diag/mappers/full or /__diag/mappers/trace for full info")
        log.exception("root traceback (platform may truncate)")
        raise RuntimeError(f"SQLAlchemy mapper configuration failed: {_MAPPERS_ERR}") from e


def check_dupe_mappers() -> Dict[str, List[str]]:
    out: Dict[str, List[str]] = {}
    try:
        if not Base or not getattr(Base, "registry", None):
            return out

        name_map: Dict[str, set[str]] = {}
        for m in Base.registry.mappers:  # type: ignore[union-attr]
            c = m.class_
            name_map.setdefault(c.__name__, set()).add(f"{c.__module__}.{c.__name__}")

        for k, v in name_map.items():
            if len(v) > 1:
                out[k] = sorted(v)

        if out:
            log.error("SQLAlchemy duplicate mappers: %s", out)
        else:
            log.info("SQLAlchemy mappers OK (no duplicates)")
    except Exception as e:
        log.warning("mapper check failed: %s", e)
    return out


# =====================================================================
# Security middlewares
# =====================================================================

class SecurityHeaders(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        try:
            resp: Response = await call_next(request)
        except (ClientDisconnect, anyio.EndOfStream):
            return Response(status_code=204)

        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        resp.headers.setdefault("Permissions-Policy", "geolocation=(), microphone=(), camera=()")

        if request.headers.get("x-forwarded-proto", "").lower() == "https":
            resp.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains; preload")

        resp.headers.setdefault("Vary", "Origin")
        return resp


class RequestIDTiming(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        rid = request.headers.get("x-request-id") or uuid.uuid4().hex
        t0 = time.perf_counter()
        try:
            resp: Response = await call_next(request)
        except (ClientDisconnect, anyio.EndOfStream):
            resp = Response(status_code=204)
        except Exception:
            log.exception("unhandled xrid=%s", rid)
            resp = JSONResponse(status_code=500, content={"detail": "internal_error", "xrid": rid})

        dur_ms = (time.perf_counter() - t0) * 1000.0
        resp.headers["x-request-id"] = rid
        resp.headers["x-process-time-ms"] = str(int(dur_ms))
        return resp


class NoCookieMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        try:
            resp: Response = await call_next(request)
        except (ClientDisconnect, anyio.EndOfStream):
            return Response(status_code=204)

        for k in list(resp.headers.keys()):
            if k.lower() == "set-cookie":
                del resp.headers[k]
        return resp


# =====================================================================
# Model import helpers (dev-only)
# =====================================================================

def _should_skip_module(fq: str) -> bool:
    base = fq.rsplit(".", 1)[-1]
    if base.startswith("_"):
        return True
    if any(bad in fq for bad in (".__disabled__", ".disabled", ".bak", ".backup")):
        return True
    return False


def _walk_pkg(pkg_name: str) -> List[str]:
    pkg = sys.modules[pkg_name]
    out: List[str] = []
    for m in _pkgutil.walk_packages(pkg.__path__, prefix=pkg_name + "."):  # type: ignore[attr-defined]
        fq = m.name
        if _should_skip_module(fq):
            continue
        out.append(fq)
    return sorted(out)


def _import_all_models_devonly() -> List[str]:
    loaded: List[str] = []
    for fq in _walk_pkg("backend.models"):
        try:
            importlib.import_module(fq)
            loaded.append(fq)
        except Exception as e:
            log.error("Model import failed: %s → %s", fq, e)
    return loaded


# =====================================================================
# Background tasks (env-gated)
# =====================================================================

async def _start_callable_in_tg(tg: anyio.abc.TaskGroup, fn: Callable[..., Any], *args, **kwargs) -> None:
    if inspect.iscoroutinefunction(fn):
        tg.start_soon(fn, *args, **kwargs)
    else:
        tg.start_soon(anyio.to_thread.run_sync, lambda: fn(*args, **kwargs))


async def _maybe_start_scheduler(tg: anyio.abc.TaskGroup) -> None:
    if not _env_bool("ENABLE_SCHEDULER", True):
        log.info("Scheduler disabled by env")
        return
    try:
        import backend.tasks.scheduler as _sched  # type: ignore
    except Exception as e:
        log.info("No scheduler module found (%s)", e)
        return

    entry: Optional[Callable[..., Any]] = None
    for candidate in ("start", "run", "serve", "main", "launch"):
        if hasattr(_sched, candidate):
            entry = getattr(_sched, candidate)
            break
    if entry is None:
        log.warning("Scheduler module present but no start/run/serve/main/launch found")
        return

    await _start_callable_in_tg(tg, entry)
    log.info("Scheduler started using %s()", entry.__name__)


async def _auto_end_live_loop(tg: anyio.abc.TaskGroup) -> None:
    if not _env_bool("ENABLE_CRON_AUTO_END", True):
        return
    try:
        from backend.cronjobs.auto_end_live import auto_end_inactive_streams  # type: ignore
    except Exception:
        log.info("auto_end_live cron not found; skipping")
        return

    interval = max(15, _env_int("CRON_AUTO_END_INTERVAL", 60))

    async def _loop():
        while True:
            try:
                await anyio.to_thread.run_sync(auto_end_inactive_streams)
            except Exception as e:
                log.warning("auto_end_live error: %s", e)
            await anyio.sleep(interval)

    tg.start_soon(_loop)
    log.info("Auto-end-live cron loop started (interval=%ss)", interval)


async def _badge_updater_loop(tg: anyio.abc.TaskGroup) -> None:
    if not _env_bool("ENABLE_BADGE_UPDATER", True):
        return
    try:
        from backend.cronjobs.badge_updater import run as _run_badges  # type: ignore
    except Exception:
        log.info("badge_updater cron not found; skipping")
        return

    interval = max(300, _env_int("BADGE_UPDATER_INTERVAL", 3600))

    async def _loop():
        while True:
            try:
                await anyio.to_thread.run_sync(_run_badges)
            except Exception as e:
                log.warning("badge_updater error: %s", e)
            await anyio.sleep(interval)

    tg.start_soon(_loop)
    log.info("Badge-updater cron loop started (interval=%ss)", interval)


# =====================================================================
# Lifespan (startup/shutdown)
# =====================================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    with suppress(Exception):
        reload_engine_from_env()

    if _env_bool("IMPORT_ALL_MODELS", _default_import_all_models()):
        loaded_models = _import_all_models_devonly()
        log.info("Models autoscan loaded (%d)", len(loaded_models))
    else:
        log.info("Models autoscan skipped (IMPORT_ALL_MODELS=0). Using registry only.")

    _configure_mappers_failfast()

    try:
        ok_warm = await anyio.to_thread.run_sync(warm_db)
        log.info("DB warm-up: %s", "OK" if ok_warm else "FAILED")
    except Exception as e:
        log.warning("DB warm-up error: %s", e)

    ok, ms, err = _db_ping()
    log.info(
        "🚀 SmartBiz boot: env=%s starlette=%s db_ok=%s db_ms=%.1f auth=bearer_only",
        ENV, STARLETTE_VER, ok, ms
    )
    if not ok:
        log.error("Database ping failed at startup (%s)", err)

    auto_create = (os.getenv("AUTO_CREATE_TABLES", "0" if ENV == "production" else "1") or "").strip().lower() in {
        "1", "true", "yes", "on"
    }
    if auto_create:
        with suppress(Exception):
            Base.metadata.create_all(bind=engine, checkfirst=True)  # type: ignore[union-attr]
            log.info("Tables verified/created (AUTO_CREATE_TABLES=1)")

    check_dupe_mappers()

    tg = anyio.create_task_group()
    await tg.__aenter__()
    app.state.task_group = tg

    with suppress(Exception):
        await _maybe_start_scheduler(tg)
    with suppress(Exception):
        await _auto_end_live_loop(tg)
    with suppress(Exception):
        await _badge_updater_loop(tg)

    try:
        yield
    finally:
        with suppress(Exception):
            tg.cancel_scope.cancel()
            await tg.__aexit__(None, None, None)
        log.info("Shutting down SmartBiz")


# =====================================================================
# CORS (FIXED) — kept for compatibility (you currently call setup_cors_guarded)
# =====================================================================

def setup_cors(app: FastAPI) -> None:
    static_defaults = [
        "https://smartbiz.site",
        "https://www.smartbiz.site",
        "https://smartbizsite.netlify.app",
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:4173",
    ]

    origins_env = _env_list("CORS_ALLOW_ORIGINS") + _env_list("FRONTEND_URLS")
    single = (os.getenv("FRONTEND_URL") or "").strip()
    if single:
        origins_env.append(single)

    origins: List[str] = []
    for o in origins_env + static_defaults:
        if o and o not in origins:
            origins.append(o)

    site = (os.getenv("NETLIFY_SITE_SUBDOMAIN") or "smartbizsite").strip()
    preview_regex = rf"^https://[a-z0-9\-]+--{re.escape(site)}\.netlify\.app$"

    allow_methods = _env_list("CORS_ALLOW_METHODS") or ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"]

    default_allow_headers = [
        "authorization",
        "content-type",
        "accept",
        "accept-language",
        "x-client-reqid",
        "x-store-id",
        "if-none-match",
        "if-modified-since",
        "x-request-id",
    ]
    allow_headers = _env_list("CORS_ALLOW_HEADERS") or default_allow_headers

    expose_headers = _env_list("CORS_EXPOSE_HEADERS") or [
        "etag",
        "x-request-id",
        "location",
        "link",
        "last-modified",
        "x-total-count",
        "x-page",
        "x-per-page",
        "x-pages",
        "x-process-time-ms",
    ]

    max_age = int(os.getenv("CORS_MAX_AGE", "86400"))
    allow_credentials = _env_bool("CORS_ALLOW_CREDENTIALS", True)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_origin_regex=preview_regex,
        allow_credentials=allow_credentials,
        allow_methods=allow_methods,
        allow_headers=allow_headers,
        expose_headers=expose_headers,
        max_age=max_age,
    )

    log.info("CORS configured: credentials=%s origins=%s regex=%s", allow_credentials, origins, preview_regex)


# =====================================================================
# Router mounting helpers (file fallback + BOM hint)
# =====================================================================

def _skip_routes_list() -> List[str]:
    return _env_list("ROUTES_SKIP")


def _auto_scan_enabled() -> bool:
    return _env_bool("ROUTES_AUTOSCAN", True)


def _is_route_module_candidate(fq: str) -> bool:
    base = fq.rsplit(".", 1)[-1]
    if base.startswith("_"):
        return False
    if any(tag in fq for tag in (".__disabled__", ".disabled", ".bak", ".backup")):
        return False
    return True


def _always_skip_for_safety() -> set[str]:
    return {
        "backend.routes.subscriptions",
    }


def _hint_if_bom(e: BaseException) -> str:
    msg = str(e) or ""
    if "U+FEFF" in msg or "non-printable character" in msg:
        return " (HINT: file has BOM/hidden char U+FEFF; re-save as UTF-8 without BOM)"
    return ""


def _load_router_by_path(app: FastAPI, module_name: str, file_path: Path, attr: str = "router") -> bool:
    try:
        if not file_path.exists():
            routes_log.error("Path fallback missing: %s", file_path)
            return False

        spec = _importlib_util.spec_from_file_location(module_name, str(file_path))
        if not spec or not spec.loader:
            routes_log.error("Path fallback spec failed: %s", file_path)
            return False

        mod = _importlib_util.module_from_spec(spec)  # type: ignore[attr-defined]
        sys.modules[module_name] = mod
        spec.loader.exec_module(mod)  # type: ignore[attr-defined]

        if not hasattr(mod, attr):
            routes_log.error("Path fallback loaded but no '%s': %s", attr, file_path)
            return False

        app.include_router(getattr(mod, attr))
        routes_log.info("Path-fallback included %s (%s)", module_name, file_path.name)
        return True

    except (UnicodeDecodeError, SyntaxError) as e:
        routes_log.error("Path fallback failed %s: %s%s", file_path, e, _hint_if_bom(e))
        return False
    except Exception as e:
        routes_log.error("Path fallback failed %s: %s", file_path, e)
        return False


# =====================================================================
# App factory
# =====================================================================

def create_app() -> FastAPI:
    app = FastAPI(
        title=os.getenv("APP_NAME", "SmartBiz Assistance API"),
        version=os.getenv("APP_VERSION", "1.0.0"),
        docs_url="/docs",
        redoc_url=None,
        openapi_url="/openapi.json",
        lifespan=lifespan,
    )
    app.router.redirect_slashes = True

    _trusted_hosts = [h.strip() for h in (os.getenv("TRUSTED_HOSTS") or "").split(",") if h.strip()]
    if _trusted_hosts and _trusted_hosts != ["*"]:
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=_trusted_hosts)

    if _HAS_PROXY_MW and _env_bool("ENABLE_PROXY_MW", True):
        with suppress(Exception):
            app.add_middleware(ProxyHeadersMiddleware, trusted_hosts="*")  # type: ignore[arg-type]
            log.info("ProxyHeadersMiddleware enabled")

    # You are using guarded CORS
    setup_cors_guarded(app, enable_guard=True)

    app.add_middleware(GZipMiddleware, minimum_size=1024)
    app.add_middleware(SecurityHeaders)
    app.add_middleware(NoCookieMiddleware)
    app.add_middleware(RequestIDTiming)

    mounted_modules: Set[str] = set()
    skip_modules = set(_skip_routes_list()) | _always_skip_for_safety()

    # --------------------------------------------------------------
    # Exception handlers
    # --------------------------------------------------------------

    @app.exception_handler(HTTPException)
    async def _http_exc(_: Request, e: HTTPException):
        return JSONResponse(status_code=e.status_code, content={"detail": e.detail})

    @app.exception_handler(RequestValidationError)
    async def _val_exc(_: Request, e: RequestValidationError):
        return JSONResponse(status_code=422, content={"detail": "validation_error", "errors": e.errors()})

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, e: Exception):
        rid = request.headers.get("x-request-id", "-")
        log.exception("unhandled-exception xrid=%s", rid)
        return JSONResponse(status_code=500, content={"detail": "internal_error", "xrid": rid})

    # --------------------------------------------------------------
    # Utility endpoints
    # --------------------------------------------------------------

    @app.get("/")
    def _root():
        return RedirectResponse("/docs", status_code=302)

    @app.head("/", include_in_schema=False)
    def _root_head():
        return Response(status_code=204)

    @app.get("/robots.txt", include_in_schema=False)
    def _robots():
        return PlainTextResponse("User-agent: *\nDisallow:\n", media_type="text/plain")

    @app.get("/favicon.ico", include_in_schema=False)
    def _favicon():
        return Response(status_code=204)

    @app.get("/health")
    def _health():
        h = db_healthcheck() if callable(db_healthcheck) else {}
        ok = bool(h.get("ok", False))
        return {
            "status": "ok" if ok else "degraded",
            "db_ok": ok,
            "db_msg": h.get("error"),
            "db_ms": h.get("ms"),
            "time_utc": h.get("time_utc"),
            "env": ENV,
            "starlette": STARLETTE_VER,
            "auth_mode": "bearer_only",
            "ts": time.time(),
        }

    @app.get("/readyz")
    def _ready():
        ok, ms, _ = _db_ping()
        return {"ready": ok, "db_ms": ms}

    @app.get("/_routes")
    def _routes_dump():
        items = []
        for r in app.router.routes:
            methods = sorted(list(getattr(r, "methods", []) or []))
            if methods:
                items.append(
                    {
                        "path": getattr(r, "path", getattr(r, "path_format", "")),
                        "methods": methods,
                        "name": getattr(r, "name", ""),
                    }
                )
        return sorted(items, key=lambda x: (x["path"], ",".join(x["methods"])))

    @app.get("/_about")
    def _about():
        return {
            "name": os.getenv("APP_NAME", "SmartBiz Assistance API"),
            "version": os.getenv("APP_VERSION", "1.0.0"),
            "env": ENV,
            "python": sys.version.split()[0],
            "starlette": STARLETTE_VER,
            "db_url_masked": _sanitize_db(os.getenv("DATABASE_URL", "")),
        }

    @app.get("/__diag/mappers")
    def _diag_mappers():
        return {
            "ok": _MAPPERS_OK,
            "error": _MAPPERS_ERR,
            "triggering_mapper": _MAPPERS_TRIGGER,
            "original_exception": _MAPPERS_ORIGINAL,
            "checked_at": _MAPPERS_TS,
            "time_utc": datetime_utc_iso(),
        }

    @app.get("/__diag/mappers/full")
    def _diag_mappers_full():
        return {
            "ok": _MAPPERS_OK,
            "error_full": _MAPPERS_ERR_FULL,
            "triggering_mapper": _MAPPERS_TRIGGER,
            "original_exception": _MAPPERS_ORIGINAL,
            "checked_at": _MAPPERS_TS,
            "time_utc": datetime_utc_iso(),
        }

    @app.get("/__diag/mappers/trace", include_in_schema=False)
    def _diag_mappers_trace():
        return PlainTextResponse(_MAPPERS_TRACE or "NO_TRACE_AVAILABLE", media_type="text/plain")

    # --------------------------------------------------------------
    # Router mounting (GUARANTEES first, then central bootstrap)
    # --------------------------------------------------------------

    def _include_if_exists(mod_name: str, attr: str = "router") -> bool:
        if mod_name in mounted_modules:
            return True
        if mod_name in skip_modules:
            routes_log.warning("Skipped: %s", mod_name)
            return False
        try:
            spec = _importlib_util.find_spec(mod_name)
            if not spec:
                routes_log.warning("%s not found", mod_name)
                return False

            mod = importlib.import_module(mod_name)
            if not hasattr(mod, attr):
                routes_log.warning("%s found but has no '%s'", mod_name, attr)
                return False

            app.include_router(getattr(mod, attr))
            mounted_modules.add(mod_name)
            routes_log.info("Included %s.%s", mod_name, attr)
            return True

        except (UnicodeDecodeError, SyntaxError) as e:
            routes_log.error("FAILED include %s (syntax/encoding): %s%s", mod_name, e, _hint_if_bom(e))
            return False
        except Exception as e:
            routes_log.error("FAILED include %s: %s", mod_name, e)
            return False

    def _include_any(candidates: List[str], attr: str = "router") -> bool:
        ok = False
        for name in candidates:
            ok = _include_if_exists(name, attr=attr) or ok
        return ok

    def _include_dual(mod_name: str, attr: str = "router") -> bool:
        if mod_name in mounted_modules:
            return True
        if mod_name in skip_modules:
            routes_log.warning("Skipped: %s", mod_name)
            return False
        try:
            spec = _importlib_util.find_spec(mod_name)
            if not spec:
                routes_log.warning("%s not found", mod_name)
                return False

            mod = importlib.import_module(mod_name)
            if not hasattr(mod, attr):
                routes_log.warning("%s found but has no '%s'", mod_name, attr)
                return False

            r = getattr(mod, attr)
            app.include_router(r)
            app.include_router(r, prefix="/api")
            mounted_modules.add(mod_name)
            routes_log.info("Included %s.%s (+ /api)", mod_name, attr)
            return True

        except (UnicodeDecodeError, SyntaxError) as e:
            routes_log.error("FAILED include %s (syntax/encoding): %s%s", mod_name, e, _hint_if_bom(e))
            return False
        except Exception as e:
            routes_log.error("FAILED include %s: %s", mod_name, e)
            return False

    # (A) Optional health router
    with suppress(Exception):
        from backend.routes import health_routes  # type: ignore
        app.include_router(health_routes.router)
        mounted_modules.add("backend.routes.health_routes")
        routes_log.info("Included backend.routes.health_routes")

    # (B) Core includes
    _include_if_exists("backend.routes.auth_routes")
    _include_if_exists("backend.routes.user_routes")
    _include_if_exists("backend.routes.profile_routes") or _include_if_exists("backend.routes.profile")

    # Uploads guarantee (R2)
    with suppress(Exception):
        from backend.routers import uploads  # type: ignore
        app.include_router(uploads.router)
        app.include_router(uploads.router, prefix="/api")
        routes_log.info("✅ Uploads router mounted (+ /api) (backend.routers.uploads)")

    # ✅✅✅ Announcements guarantee (UPDATED + INDENT SAFE)
    announce_module: str | None = None

    if _include_if_exists("backend.routes.announcements"):
        ok_announce = True
        announce_module = "backend.routes.announcements"
    else:
        ok_announce = False

    if not ok_announce:
        candidates = [
            "backend.routes.announcements_routes",
            "backend.routes.announcement_routes",
            "backend.routes.announcement",
            "backend.routes.announce_routes",
        ]
        ok_announce = _include_any(candidates)
        if ok_announce:
            announce_module = candidates[0]

    if not ok_announce:
        ok_announce = _load_router_by_path(
            app=app,
            module_name="backend.routes.__path_fallback_announcements__",
            file_path=_BACKEND / "routes" / "announcements.py",
            attr="router",
        )
        if ok_announce:
            announce_module = "backend.routes.__path_fallback_announcements__"

    if ok_announce:
        mounted_modules.add(announce_module or "backend.routes.announcements")
        routes_log.info("✅ Announcements router mounted (%s)", announce_module or "unknown")
    else:
        routes_log.info("❌ Announcements router NOT mounted (no module matched and path fallback failed)")

    # Dashboard guarantee
    ok_dash = _include_if_exists("backend.routes.dashboard_routes")
    if not ok_dash:
        ok_dash = _include_any(["backend.routes.dashboard", "backend.routes.dashboard_route", "backend.routes.dashboard_router"])
    if not ok_dash:
        ok_dash = _load_router_by_path(
            app=app,
            module_name="backend.routes.__path_fallback_dashboard__",
            file_path=_BACKEND / "routes" / "dashboard_routes.py",
            attr="router",
        )
    routes_log.info("✅ Dashboard router mounted" if ok_dash else "❌ Dashboard router NOT mounted")

    # Stats guarantee
    ok_stats = _include_if_exists("backend.routes.stats_routes")
    if not ok_stats:
        ok_stats = _include_any(["backend.routes.stats", "backend.routes.stats_route", "backend.routes.stats_router"])
    if not ok_stats:
        ok_stats = _load_router_by_path(
            app=app,
            module_name="backend.routes.__path_fallback_stats__",
            file_path=_BACKEND / "routes" / "stats_routes.py",
            attr="router",
        )
    routes_log.info("✅ Stats router mounted" if ok_stats else "❌ Stats router NOT mounted")

    # Inbox (+ /api compat)
    _include_dual("backend.routes.inbox_routes") or _include_dual("backend.routes.inbox")
    _include_dual("backend.routes.conversations_routes") or _include_dual("backend.routes.conversations")
    _include_dual("backend.routes.messages_routes") or _include_dual("backend.routes.messages")

    # Products guarantee
    ok_products = _include_if_exists("backend.routes.products")
    if not ok_products:
        ok_products = _include_any(["backend.routes.products_routes", "backend.routes.product_routes", "backend.routes.product"])
    if not ok_products:
        ok_products = _load_router_by_path(
            app=app,
            module_name="backend.routes.__path_fallback_products__",
            file_path=_BACKEND / "routes" / "products.py",
            attr="router",
        )
    routes_log.info("✅ Products router mounted" if ok_products else "❌ Products router NOT mounted")

    # MyStore hub guarantee
    ok_mystore = _include_if_exists("backend.routes.mystore")
    if not ok_mystore:
        ok_mystore = _include_any(["backend.routes.mystore_routes", "backend.routes.store_hub_routes"])
    if not ok_mystore:
        ok_mystore = _load_router_by_path(
            app=app,
            module_name="backend.routes.__path_fallback_mystore__",
            file_path=_BACKEND / "routes" / "mystore.py",
            attr="router",
        )
    routes_log.info("✅ MyStore router mounted" if ok_mystore else "❌ MyStore router NOT mounted")

    # mystore_products_compat optional
    with suppress(Exception):
        from backend.routes import mystore_products_compat  # type: ignore
        app.include_router(mystore_products_compat.router)
        app.include_router(mystore_products_compat.router, prefix="/api")
        mounted_modules.add("backend.routes.mystore_products_compat")
        routes_log.info("✅ mystore_products_compat mounted (+ /api)")

    # Central router bootstrap (optional)
    with suppress(Exception):
        from backend.routes import include_default_routers  # type: ignore
        added = include_default_routers(app)
        for m in added:
            mounted_modules.add(m)
        routes_log.info("Central bootstrap included: %s", added)

    # Optional autoscan leftovers (safe)
    if _auto_scan_enabled():
        with suppress(Exception):
            import backend.routes as _routes_pkg  # type: ignore
            for m in _pkgutil.walk_packages(_routes_pkg.__path__, prefix="backend.routes."):  # type: ignore[attr-defined]
                fq = m.name
                if not _is_route_module_candidate(fq):
                    continue
                if fq in mounted_modules or fq in skip_modules:
                    continue
                try:
                    mod = importlib.import_module(fq)
                    if hasattr(mod, "router"):
                        app.include_router(getattr(mod, "router"))
                        mounted_modules.add(fq)
                        routes_log.info("Auto-included %s.router", fq)
                except (SyntaxError, UnicodeDecodeError) as e:
                    routes_log.error("Failed auto-include %s (syntax/encoding): %s%s", fq, e, _hint_if_bom(e))
                except Exception as e:
                    routes_log.error("Failed auto-include %s: %s", fq, e)
    else:
        routes_log.warning("Autoscan disabled (ROUTES_AUTOSCAN=0)")

    return app


# =====================================================================
# Singleton ASGI app
# =====================================================================

app = create_app()


# =====================================================================
# Local dev runner
# =====================================================================

if __name__ == "__main__":  # pragma: no cover
    import uvicorn
    uvicorn.run(
        "backend.main:app",
        host=os.getenv("HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", "8000")),
        proxy_headers=True,
        forwarded_allow_ips="*",
        reload=(os.getenv("RELOAD") or ("0" if ENV == "production" else "1")).strip().lower() in {"1", "true", "yes", "on"},
    )
