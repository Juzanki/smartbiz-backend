# backend/api/health.py
# Health & DB readiness endpoints for SmartBiz (FastAPI)
# - Robust DB URL handling (adds sslmode=require if missing)
# - Clear error messages
# - Latency check + basic DB info
# - Liveness (/health/live) & Readiness (/health/ready) endpoints

from __future__ import annotations

import os
import re
import time
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

router = APIRouter()

# ---- Helpers ---------------------------------------------------------------

def _ensure_sslmode_require(url: str) -> str:
    """Append sslmode=require if not already present (Render Postgres often needs it)."""
    if not url:
        return url
    if "sslmode=" in url:
        return url
    separator = "&" if "?" in url else "?"
    return f"{url}{separator}sslmode=require"

def _redact_dsn(url: str) -> str:
    """Hide password in DSN for safe logging/response."""
    if not url:
        return ""
    return re.sub(r"://([^:@/]+):([^@/]+)@", r"://\1:****@", url)

def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

# ---- Engine (global) -------------------------------------------------------

RAW_DB_URL = (os.getenv("DATABASE_URL") or "").strip()
DB_URL = _ensure_sslmode_require(RAW_DB_URL)

# Pool tuning via env (optional)
POOL_SIZE = int(os.getenv("DB_POOL_SIZE", "5"))
MAX_OVERFLOW = int(os.getenv("DB_POOL_MAX_OVERFLOW", "10"))
POOL_RECYCLE = int(os.getenv("DB_POOL_RECYCLE", "1800"))  # seconds

if not DB_URL:
    # We delay raising; endpoints will return 500 with a clear message
    engine = None  # type: ignore[assignment]
else:
    engine = create_engine(
        DB_URL,
        pool_pre_ping=True,         # avoid stale connections
        pool_size=POOL_SIZE,
        max_overflow=MAX_OVERFLOW,
        pool_recycle=POOL_RECYCLE,
        future=True,
    )

# ---- Liveness & Readiness --------------------------------------------------

@router.get("/health/live", tags=["health"])
def health_live():
    """Liveness: process is up."""
    return {
        "status": "ok",
        "service": os.getenv("APP_NAME", "smartbiz-backend"),
        "env": os.getenv("ENV", "production"),
        "time_utc": _utc_now_iso(),
    }

@router.get("/health/ready", tags=["health"])
def health_ready():
    """
    Readiness: ensure critical dependencies (DB) are reachable.
    Equivalent to hitting /health/db.
    """
    try:
        return health_db()
    except HTTPException as e:
        # Bubble up same message/status
        raise e

# ---- DB Health -------------------------------------------------------------

@router.get("/health/db", tags=["health"])
def health_db():
    """Checks DB connection, returns latency and minimal DB info."""
    if not DB_URL:
        raise HTTPException(
            status_code=500,
            detail="db_error: DATABASE_URL is not set in environment."
        )

    if engine is None:
        raise HTTPException(
            status_code=500,
            detail="db_error: SQL engine not initialized."
        )

    start = time.perf_counter()
    try:
        with engine.connect() as conn:
            # Simple ping
            conn.execute(text("SELECT 1"))

            # Optional: fetch small bits of info
            row = conn.execute(
                text("SELECT current_database() AS db, NOW() AT TIME ZONE 'UTC' AS now_utc")
            ).mappings().first()

        latency_ms = round((time.perf_counter() - start) * 1000, 2)

        payload = {
            "status": "ok",
            "service": os.getenv("APP_NAME", "smartbiz-backend"),
            "env": os.getenv("ENV", "production"),
            "time_utc": _utc_now_iso(),
            "db": {
                "status": "ok",
                "latency_ms": latency_ms,
                "database": (row or {}).get("db") if row else None,
            },
            "dsn": _redact_dsn(DB_URL),  # redacted for safety
        }

        # Pool stats (best-effort; depends on pool implementation)
        try:
            pool = getattr(engine, "pool", None)
            if pool is not None and hasattr(pool, "status"):
                payload["pool"] = {"status": pool.status()}
        except Exception:
            pass

        return payload

    except SQLAlchemyError as e:
        # SQLAlchemy-related operational errors
        raise HTTPException(
            status_code=500,
            detail=f"db_error: {type(e).__name__}: {e}"
        )
    except Exception as e:
        # Anything else (network, DNS, etc.)
        raise HTTPException(
            status_code=500,
            detail=f"db_error: {type(e).__name__}: {e}"
        )
