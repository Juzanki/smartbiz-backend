# backend/main.py
# -*- coding: utf-8 -*-
from __future__ import annotations
"""
SmartBiz Assistance API bootstrap (Render-stable edition)

Key guarantees:
- Bearer-only API (no cookies leaked to clients)
- Tight CORS (Netlify preview + production domain)
- Security headers, request timing, request id
- Background schedulers / crons (env gated)
- Safe model import before startup to avoid SQLAlchemy circular mapper crashes
- Health + metrics endpoints via backend.routes.health_routes

This file intentionally centralizes:
- FastAPI app creation
- Middleware wiring
- Router registration (auth, live, customers, etc.)
- Diagnostics helpers (/health, /readyz, /_routes, /_about, etc.)
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
from pathlib import Path
from typing import Dict, List, Tuple, Callable, Any
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.middleware.cors import CORSMiddleware

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
try:
    from starlette.middleware.proxy_headers import ProxyHeadersMiddleware  # type: ignore
    _HAS_PROXY_MW = True
except Exception:  # pragma: no cover
    ProxyHeadersMiddleware = None  # type: ignore
    _HAS_PROXY_MW = False

from starlette.requests import ClientDisconnect
from starlette.responses import (
    JSONResponse,
    RedirectResponse,
    Response,
    PlainTextResponse,
)

# ────────────────────────────── Path / namespace bootstrap ──────────────────────────────
_THIS = Path(__file__).resolve()
_BACKEND = _THIS.parent
_ROOT = _BACKEND.parent

# make sure project root is importable (Render shell, uvicorn factory, etc)
if str(_ROOT) not in sys.path:
    sys.path.append(str(_ROOT))

def _ensure_pkg_ns(name: str, path: Path) -> types.ModuleType:
    """
    Ensure modules like 'backend', 'backend.models', 'backend.routes'
    exist in sys.modules as packages even in weird runtime modes.
    """
    mod = sys.modules.get(name)
    if mod is None:
        mod = types.ModuleType(name)
        mod.__path__ = [str(path)]  # type: ignore[attr-defined]
        sys.modules[name] = mod
    return mod

_ensure_pkg_ns("backend", _BACKEND)
_ensure_pkg_ns("backend.models", _BACKEND / "models")
_ensure_pkg_ns("backend.routes", _BACKEND / "routes")

# legacy fallbacks so accidental "import models" / "import routes" doesn't explode
sys.modules.setdefault("models", sys.modules["backend.models"])
sys.modules.setdefault("routes", sys.modules["backend.routes"])

# ────────────────────────────── Env helpers ──────────────────────────────
def _env_bool(k: str, default: bool = False) -> bool:
    v = os.getenv(k)
    return default if v is None else v.strip().lower() in {"1", "true", "yes", "on"}

def _env_int(k: str, default: int) -> int:
    try:
        return int(os.getenv(k, "").strip() or default)
    except Exception:
        return default

def _env_list(k: str) -> List[str]:
    raw = os.getenv(k, "")
    return [x.strip() for x in raw.split(",") if x.strip()]

ENV = (os.getenv("ENVIRONMENT") or os.getenv("ENV") or "production").strip().lower()

# ────────────────────────────── Logging setup ──────────────────────────────
LOG_JSON = (os.getenv("LOG_JSON", "0") or "").strip().lower() in {"1", "true", "yes", "on"}
LOG_LEVEL = getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO)

class _JsonFmt(logging.Formatter):
    def format(self, r: logging.LogRecord) -> str:
        out = {
            "ts": self.formatTime(r, "%Y-%m-%dT%H:%M:%S"),
            "level": r.levelname,
            "logger": r.name,
            "msg": r.getMessage(),
        }
        if r.exc_info:
            out["exc"] = self.formatException(r.exc_info)
        return json.dumps(out, ensure_ascii=False)

_handler = logging.StreamHandler()
_handler.setFormatter(
    _JsonFmt() if LOG_JSON else logging.Formatter(
        "%(asctime)s | %(levelname)s | %(name)s | %(message)s"
    )
)
root_logger = logging.getLogger()
root_logger.handlers = [_handler]
root_logger.setLevel(LOG_LEVEL)

log = logging.getLogger("smartbiz.main")

try:
    import starlette as _st
    STARLETTE_VER = getattr(_st, "__version__", "?")
except Exception:  # pragma: no cover
    STARLETTE_VER = "?"

# ────────────────────────────── DB glue ──────────────────────────────
try:
    from backend.db import (  # type: ignore
        Base,
        SessionLocal,
        engine,
        warm_db,
        reload_engine_from_env,
        db_healthcheck,
    )
except Exception:  # pragma: no cover
    # fallback if someone does `uvicorn main:app` directly
    from db import (  # type: ignore
        Base,
        SessionLocal,
        engine,
        warm_db,
        reload_engine_from_env,
        db_healthcheck,
    )

from sqlalchemy import text
from sqlalchemy.orm import Session

def get_db():
    """
    FastAPI dependency to yield a DB session.
    Routers can `from backend.main import get_db` safely.
    """
    db: Session = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def _db_ping() -> Tuple[bool, float, str]:
    """
    Very light DB ping for readiness.
    """
    t0 = time.perf_counter()
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True, (time.perf_counter() - t0) * 1000.0, ""
    except Exception as e:
        return False, (time.perf_counter() - t0) * 1000.0, f"{type(e).__name__}"

def _sanitize_db(url: str) -> str:
    """
    Mask password in DATABASE_URL for diagnostics.
    """
    if not url:
        return ""
    return re.sub(r"://([^:@/]+):([^@/]+)@", r"://\1:****@", url)

# ────────────────────────────── Security middleware ──────────────────────────────
class SecurityHeaders(BaseHTTPMiddleware):
    """
    Add strict security headers to every response.
    Adds HSTS only if request came via https proxy (x-forwarded-proto=https).
    """
    async def dispatch(self, request: Request, call_next):
        try:
            resp: Response = await call_next(request)
        except (ClientDisconnect, anyio.EndOfStream):
            # 499 = client closed early
            return Response(status_code=499)

        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        resp.headers.setdefault(
            "Permissions-Policy", "geolocation=(), microphone=(), camera=()"
        )

        # Send HSTS only if https
        if request.headers.get("x-forwarded-proto", "").lower() == "https":
            resp.headers.setdefault(
                "Strict-Transport-Security",
                "max-age=31536000; includeSubDomains; preload",
            )

        # Help caches differentiate
        resp.headers.setdefault("Vary", "Origin")
        return resp


class RequestIDTiming(BaseHTTPMiddleware):
    """
    - Attach x-request-id if not provided
    - Attach x-process-time-ms with wall-clock processing time
    - Catch unexpected exceptions and make them 500 JSON
    """
    async def dispatch(self, request: Request, call_next):
        rid = request.headers.get("x-request-id") or uuid.uuid4().hex
        t0 = time.perf_counter()
        try:
            resp: Response = await call_next(request)
        except Exception:
            log.exception("unhandled xrid=%s", rid)
            resp = JSONResponse(status_code=500, content={"detail": "internal_error"})
        dur_ms = (time.perf_counter() - t0) * 1000.0
        resp.headers["x-request-id"] = rid
        resp.headers["x-process-time-ms"] = str(int(dur_ms))
        return resp


class NoCookieMiddleware(BaseHTTPMiddleware):
    """
    Force bearer-token only API by stripping any Set-Cookie headers.
    """
    async def dispatch(self, request: Request, call_next):
        resp: Response = await call_next(request)
        for k in list(resp.headers.keys()):
            if k.lower() == "set-cookie":
                del resp.headers[k]
        return resp

# ────────────────────────────── Model import + mapper sanity ──────────────────────────────
def _should_skip_module(fq: str) -> bool:
    base = fq.rsplit(".", 1)[-1]
    if base.startswith("_"):
        return True
    if any(bad in fq for bad in (".__disabled__", ".disabled", ".bak", ".backup")):
        return True
    return False

def _walk_pkg(pkg_name: str) -> List[str]:
    """
    Return all modules in pkg_name.* (sorted so backend.models.user loads first).
    """
    pkg = sys.modules[pkg_name]
    out: List[str] = []
    for m in _pkgutil.walk_packages(pkg.__path__, prefix=pkg_name + "."):  # type: ignore[attr-defined]
        fq = m.name
        if _should_skip_module(fq):
            continue
        out.append(fq)
    # load user model first so relationship("User") resolves early
    return sorted(out, key=lambda s: (s != "backend.models.user", s))

def _import_all_models() -> List[str]:
    """
    Import every backend.models.* file so SQLAlchemy mappers are registered
    ONE TIME at startup. This prevents the classic circular import crash:
      "Mapper[User] has no property X"
    ...as long as relationships are declared consistently.
    """
    loaded: List[str] = []
    for fq in _walk_pkg("backend.models"):
        try:
            importlib.import_module(fq)
            loaded.append(fq)
        except Exception as e:
            log.error("Model import failed: %s → %s", fq, e)
    return loaded

def check_dupe_mappers() -> Dict[str, List[str]]:
    """
    Detect accidental duplicate ORM mappers (copy/paste model with same class name).
    Logs error if found.
    """
    out: Dict[str, List[str]] = {}
    try:
        name_map: Dict[str, set[str]] = {}
        for m in Base.registry.mappers:
            c = m.class_
            name_map.setdefault(c.__name__, set()).add(f"{c.__module__}.{c.__name__}")
        for k, v in name_map.items():
            if len(v) > 1:
                out[k] = sorted(v)
        if out:
            log.error("SQLAlchemy duplicate mappers: %s", out)
        else:
            log.info("SQLAlchemy mappers OK")
    except Exception as e:
        log.warning("mapper check failed: %s", e)
    return out

# ────────────────────────────── Background task helpers ──────────────────────────────
async def _start_callable_in_tg(
    tg: anyio.abc.TaskGroup, fn: Callable[..., Any], *args, **kwargs
):
    if inspect.iscoroutinefunction(fn):
        tg.start_soon(fn, *args, **kwargs)
    else:
        tg.start_soon(anyio.to_thread.run_sync, lambda: fn(*args, **kwargs))

async def _maybe_start_scheduler(tg: anyio.abc.TaskGroup) -> None:
    """
    backend.tasks.scheduler.start()/run()/serve()/main()/launch()
    controlled by ENABLE_SCHEDULER.
    """
    if not _env_bool("ENABLE_SCHEDULER", True):
        log.info("Scheduler disabled by env")
        return
    try:
        import backend.tasks.scheduler as _sched  # type: ignore
    except Exception as e:
        log.info("No scheduler module found (%s)", e)
        return

    entry: Callable[..., Any] | None = None
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
    """
    Periodically end inactive livestreams.
    ENABLE_CRON_AUTO_END / CRON_AUTO_END_INTERVAL.
    """
    if not _env_bool("ENABLE_CRON_AUTO_END", True):
        return
    try:
        from backend.cronjobs.auto_end_live import auto_end_inactive_streams  # type: ignore
    except Exception:
        log.info("auto_end_live cron not found; skipping")
        return

    interval = max(15, _env_int("CRON_AUTO_END_INTERVAL", 60))  # seconds

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
    """
    Periodically recompute badges / ranks.
    ENABLE_BADGE_UPDATER / BADGE_UPDATER_INTERVAL.
    """
    if not _env_bool("ENABLE_BADGE_UPDATER", True):
        return
    try:
        from backend.cronjobs.badge_updater import run as _run_badges  # type: ignore
    except Exception:
        log.info("badge_updater cron not found; skipping")
        return

    interval = max(300, _env_int("BADGE_UPDATER_INTERVAL", 3600))  # seconds

    async def _loop():
        while True:
            try:
                await anyio.to_thread.run_sync(_run_badges)
            except Exception as e:
                log.warning("badge_updater error: %s", e)
            await anyio.sleep(interval)

    tg.start_soon(_loop)
    log.info("Badge-updater cron loop started (interval=%ss)", interval)

# ────────────────────────────── Lifespan (startup / shutdown) ──────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Startup:
      - Reload DB engine from env
      - Import all models (register mappers deterministically)
      - Warm DB connections
      - Ping DB
      - Optionally Base.metadata.create_all() (if not strict prod)
      - Check for duplicate mappers
      - Launch background cron loops in a TaskGroup

    Shutdown:
      - Cancel TaskGroup nicely
    """
    # make sure DB engine matches env's DATABASE_URL
    with suppress(Exception):
        reload_engine_from_env()

    # Import all ORM models BEFORE touching sessions
    loaded_models = _import_all_models()
    log.info("Models loaded: %s", loaded_models)

    # Pre-warm DB pool
    try:
        ok_warm = await anyio.to_thread.run_sync(warm_db)
        log.info("DB warm-up: %s", "OK" if ok_warm else "FAILED")
    except Exception as e:
        log.warning("DB warm-up error: %s", e)

    ok, ms, err = _db_ping()
    log.info(
        "Starting SmartBiz (env=%s, starlette=%s, db_ok=%s, db_ms=%.1f)",
        ENV,
        STARLETTE_VER,
        ok,
        ms,
    )
    if not ok:
        log.error("Database ping failed at startup (%s)", err)

    # Auto create tables (dev/staging). In prod default is off.
    if (os.getenv("AUTO_CREATE_TABLES", "1" if ENV != "production" else "0")
        .lower()
        in {"1", "true", "yes", "on"}):
        with suppress(Exception):
            Base.metadata.create_all(bind=engine, checkfirst=True)
            log.info("Tables verified/created")

    # Detect broken duplicate mappers
    if (os.getenv("FAIL_ON_DUP_MAPPERS", "0").lower() in {"1", "true", "yes", "on"}):
        d = check_dupe_mappers()
        if d:
            raise RuntimeError(f"Duplicate ORM mappers: {d}")
    else:
        check_dupe_mappers()

    # Background workers
    tg = await anyio.create_task_group().__aenter__()
    app.state.task_group = tg  # debug handle

    with suppress(Exception):
        await _maybe_start_scheduler(tg)
    with suppress(Exception):
        await _auto_end_live_loop(tg)
    with suppress(Exception):
        await _badge_updater_loop(tg)

    try:
        yield
    finally:
        # graceful shutdown
        with suppress(Exception):
            tg.cancel_scope.cancel()
            await tg.__aexit__(None, None, None)
        log.info("Shutting down SmartBiz")

# ────────────────────────────── CORS config ──────────────────────────────
def setup_cors(app: FastAPI) -> None:
    """
    Stateless CORS for Bearer-only API (no cookies).
    - allow_credentials=False always
    - allow production host + Netlify previews
    """
    def _list_env(k: str) -> List[str]:
        raw = os.getenv(k, "")
        return [x.strip() for x in raw.split(",") if x.strip()]

    # 1. Allowed origins (explicit env override first)
    origins = _list_env("CORS_ALLOW_ORIGINS") or _list_env("FRONTEND_URLS")
    if not origins:
        single = os.getenv("FRONTEND_URL", "").strip()
        if single:
            origins = [single]
    if not origins:
        # final fallback → your actual Netlify prod
        origins = ["https://smartbizsite.netlify.app"]

    # 2. Netlify preview regex: https://randomhash--smartbizsite.netlify.app
    site = (os.getenv("NETLIFY_SITE_SUBDOMAIN") or "smartbizsite").strip()
    preview_regex = rf"^https://[a-z0-9]+--{re.escape(site)}\.netlify\.app$"

    # 3. Allowed methods/headers
    allow_methods = (
        _list_env("CORS_ALLOW_METHODS")
        or ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"]
    )
    default_allow_headers = [
        "authorization",
        "content-type",
        "accept",
        "accept-language",
        "x-client-reqid",
        "if-none-match",
        "if-modified-since",
    ]
    allow_headers = _list_env("CORS_ALLOW_HEADERS") or default_allow_headers
    expose_headers = (
        _list_env("CORS_EXPOSE_HEADERS")
        or ["etag", "x-request-id", "location", "link", "last-modified"]
    )
    max_age = int(os.getenv("CORS_MAX_AGE", "86400"))

    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_origin_regex=preview_regex,
        allow_credentials=False,  # BEARER ONLY
        allow_methods=allow_methods,
        allow_headers=allow_headers,
        expose_headers=expose_headers,
        max_age=max_age,
    )

    log.info(
        "CORS configured: origins=%s allow_headers=%s expose=%s regex=%s",
        origins,
        allow_headers,
        expose_headers,
        preview_regex,
    )

# ────────────────────────────── FastAPI app factory ──────────────────────────────
def create_app() -> FastAPI:
    app = FastAPI(
        title=os.getenv("APP_NAME", "SmartBiz API"),
        version=os.getenv("APP_VERSION", "1.0.0"),
        docs_url="/docs",
        redoc_url=None,
        openapi_url="/openapi.json",
        lifespan=lifespan,
    )

    # normalize /foo vs /foo/
    app.router.redirect_slashes = True

    # TrustedHostMiddleware if TRUSTED_HOSTS is set
    _trusted_hosts = [
        h.strip()
        for h in (os.getenv("TRUSTED_HOSTS") or "").split(",")
        if h.strip()
    ]
    if _trusted_hosts and _trusted_hosts != ["*"]:
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=_trusted_hosts)

    # Respect x-forwarded-for / x-forwarded-proto from Render proxy
    if _HAS_PROXY_MW and _env_bool("ENABLE_PROXY_MW", True):
        with suppress(Exception):
            app.add_middleware(ProxyHeadersMiddleware, trusted_hosts="*")  # type: ignore
            log.info("ProxyHeadersMiddleware enabled")

    # Middleware order matters
    setup_cors(app)
    app.add_middleware(GZipMiddleware, minimum_size=1024)
    app.add_middleware(SecurityHeaders)
    app.add_middleware(NoCookieMiddleware)
    app.add_middleware(RequestIDTiming)

    # ─────────────────────── Router mounting ───────────────────────
    _routes_logger = logging.getLogger("smartbiz.routes")
    mounted_modules: set[str] = set()

    def _include_if_exists(mod_name: str, attr: str = "router") -> bool:
        """
        Try to include mod_name.router once.
        Safe even if module/file missing.
        """
        if mod_name in mounted_modules:
            return True
        spec = _importlib_util.find_spec(mod_name)
        if not spec:
            _routes_logger.warning("%s not found", mod_name)
            return False
        mod = importlib.import_module(mod_name)
        if not hasattr(mod, attr):
            _routes_logger.warning("%s found but has no '%s'", mod_name, attr)
            return False
        app.include_router(getattr(mod, attr))
        mounted_modules.add(mod_name)
        _routes_logger.info("Included %s.%s", mod_name, attr)
        return True

    # 1. Our official health + metrics routes (prevents 405 spam on /metrics/overview, /stats/overview, etc.)
    with suppress(Exception):
        from backend.routes import health_routes  # type: ignore
        app.include_router(health_routes.router)
        mounted_modules.add("backend.routes.health_routes")
        _routes_logger.info("Included backend.routes.health_routes")

    # 2. Backward-compat health routers (optional). We keep them just in case old code still exists.
    with suppress(Exception):
        from backend.api import health as _health_mod  # type: ignore
        if hasattr(_health_mod, "router"):
            app.include_router(_health_mod.router)
            mounted_modules.add("backend.api.health")
            _routes_logger.info("Included backend.api.health (legacy)")

    with suppress(Exception):
        from backend.api import legacy_views as _legacy_mod  # type: ignore
        if hasattr(_legacy_mod, "router"):
            app.include_router(_legacy_mod.router)
            mounted_modules.add("backend.api.legacy_views")
            _routes_logger.info("Included legacy_views router")

    # 3. Critical routers explicitly
    _include_if_exists("backend.routes.auth_routes")
    _include_if_exists("backend.routes.live_routes")

    # 4. Customers router explicitly (for /customers/{id}, etc.)
    with suppress(Exception):
        _include_if_exists("backend.routes.customers")

    # 5. Autoscan other backend.routes.* that expose `router`
    with suppress(Exception):
        import backend.routes as _routes_pkg  # type: ignore
        for m in _pkgutil.walk_packages(_routes_pkg.__path__, prefix="backend.routes."):  # type: ignore[attr-defined]
            fq = m.name
            base = fq.rsplit(".", 1)[-1]
            if base.startswith("_") or any(tag in fq for tag in (".__disabled__", ".disabled", ".bak", ".backup")):
                continue
            if fq in mounted_modules:
                continue
            try:
                mod = importlib.import_module(fq)
                if hasattr(mod, "router"):
                    app.include_router(getattr(mod, "router"))
                    mounted_modules.add(fq)
                    _routes_logger.info("Auto-included %s.router", fq)
            except Exception as e:
                _routes_logger.error("Failed auto-include %s: %s", fq, e)

    # 6. Optional helper aggregators
    with suppress(Exception):
        from backend.routes import autoscan_routes as _autoscan_router  # type: ignore
        app.include_router(_autoscan_router)
        mounted_modules.add("backend.routes.autoscan_routes")
        _routes_logger.info("Included autoscan_routes")

    with suppress(Exception):
        from backend.routes import include_default_routers, log_registered_routes  # type: ignore
        included_modules = include_default_routers(app)
        if included_modules:
            _routes_logger.info(
                "Routes included via bootstrap: %s", included_modules
            )
        log_registered_routes(app)

    # ─────────────────────── Built-in utility endpoints ───────────────────────
    @app.options("/{path:path}")
    async def preflight_ok(_: Request, path: str):
        """Always return 204 for CORS preflight."""
        return Response(status_code=204)

    @app.exception_handler(HTTPException)
    async def _http_exc(_: Request, e: HTTPException):
        return JSONResponse(status_code=e.status_code, content={"detail": e.detail})

    @app.exception_handler(RequestValidationError)
    async def _val_exc(_: Request, e: RequestValidationError):
        return JSONResponse(
            status_code=422,
            content={"detail": "validation_error", "errors": e.errors()},
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, e: Exception):
        rid = request.headers.get("x-request-id", "-")
        log.exception("unhandled-exception xrid=%s", rid)
        return JSONResponse(
            status_code=500,
            content={"detail": "internal_error", "xrid": rid},
        )

    # root → docs
    @app.get("/")
    def _root():
        return RedirectResponse("/docs", status_code=302)

    @app.head("/", include_in_schema=False)
    def _root_head():
        return Response(status_code=204)

    # public robots.txt
    @app.get("/robots.txt", include_in_schema=False)
    def _robots():
        return PlainTextResponse("User-agent: *\nDisallow:\n", media_type="text/plain")

    # placeholder favicon (204 no content)
    @app.get("/favicon.ico", include_in_schema=False)
    def _favicon():
        return Response(status_code=204)

    # live health snapshot (includes db status)
    @app.get("/health")
    def _health():
        h = db_healthcheck()
        have_bcrypt = False
        try:
            import bcrypt  # type: ignore  # noqa: F401
            have_bcrypt = True
        except Exception:
            have_bcrypt = False
        return {
            "status": "ok" if h.get("ok") else "degraded",
            "db_ok": bool(h.get("ok")),
            "db_msg": h.get("error"),
            "time_utc": h.get("time_utc"),
            "env": ENV,
            "starlette": STARLETTE_VER,
            "bcrypt": have_bcrypt,
            "auth_mode": "bearer_only",
            "ts": time.time(),
        }

    # readiness probe for Render/Netlify/etc
    @app.get("/readyz")
    def _ready():
        ok, ms, _ = _db_ping()
        return {"ready": ok, "db_ms": ms}

    # dump list of registered routes (nice for debugging on Render shell)
    @app.get("/_routes")
    def _routes_dump():
        items = []
        for r in app.router.routes:
            methods = sorted(list(getattr(r, "methods", []) or []))
            if methods:
                items.append({
                    "path": getattr(r, "path", getattr(r, "path_format", "")),
                    "methods": methods,
                    "name": getattr(r, "name", ""),
                })
        with suppress(Exception):
            nice = "\n".join(
                f"{','.join(x['methods']):10s} {x['path']}"
                for x in items
            )
            log.info("Registered routes:\n%s", nice)
        return sorted(items, key=lambda x: (x["path"], ",".join(x["methods"])))

    # about / mask DB url
    @app.get("/_about")
    def _about():
        return {
            "name": os.getenv("APP_NAME", "SmartBiz API"),
            "version": os.getenv("APP_VERSION", "1.0.0"),
            "env": ENV,
            "python": sys.version.split()[0],
            "starlette": STARLETTE_VER,
            "db_url_masked": _sanitize_db(os.getenv("DATABASE_URL", "")),
        }

    # show info about auth_routes (helps debug if Render can't find auth router)
    @app.get("/__which_auth", include_in_schema=False)
    def __which_auth():
        out: dict[str, Any] = {}
        try:
            spec = _importlib_util.find_spec("backend.routes.auth_routes")
            out["auth_routes_found"] = bool(spec)
            out["auth_routes_file"] = getattr(spec, "origin", None) if spec else None
            if spec:
                mod = importlib.import_module("backend.routes.auth_routes")
                out["auth_routes_has_router"] = hasattr(mod, "router")
        except Exception as e:
            out["auth_routes_error"] = f"{type(e).__name__}: {e}"
        return out

    # safe environment view (secrets masked)
    @app.get("/__env_safe", include_in_schema=False)
    def __env_safe():
        def _mask(k: str, v: str) -> str:
            if any(s in k.upper() for s in ("SECRET", "TOKEN", "KEY", "PASS")):
                return "****"
            return v
        return {k: _mask(k, v) for k, v in os.environ.items()}

    # debug current CORS expectations
    @app.get("/__cors_debug", include_in_schema=False)
    def __cors_debug():
        site = (os.getenv("NETLIFY_SITE_SUBDOMAIN") or "smartbizsite").strip()
        return {
            "allow_origins": _env_list("CORS_ALLOW_ORIGINS")
                or _env_list("FRONTEND_URLS")
                or [os.getenv("FRONTEND_URL", "")],
            "allow_methods": _env_list("CORS_ALLOW_METHODS")
                or ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
            "allow_headers": _env_list("CORS_ALLOW_HEADERS")
                or [
                    "authorization",
                    "content-type",
                    "accept",
                    "accept-language",
                    "x-client-reqid",
                    "if-none-match",
                    "if-modified-since",
                ],
            "expose_headers": _env_list("CORS_EXPOSE_HEADERS")
                or [
                    "etag",
                    "x-request-id",
                    "location",
                    "link",
                    "last-modified",
                ],
            "allow_credentials": False,
            "allow_origin_regex": rf"^https://[a-z0-9]+--{re.escape(site)}\.netlify\.app$",
        }

    return app

# ────────────────────────────── Singleton ASGI app ──────────────────────────────
app = create_app()

# ────────────────────────────── Local dev runner ──────────────────────────────
if __name__ == "__main__":  # pragma: no cover
    import uvicorn
    uvicorn.run(
        "backend.main:app",
        host=os.getenv("HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", "8000")),
        proxy_headers=True,
        forwarded_allow_ips="*",
        reload=(
            os.getenv("RELOAD") or ("0" if ENV == "production" else "1")
        ).lower() in {"1", "true", "yes", "on"},
    )
