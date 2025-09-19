# backend/db.py
# -*- coding: utf-8 -*-
"""
SmartBiz Assistance – SQLAlchemy (sync) for PostgreSQL (Render-friendly)

ENV vars (kwa kipaumbele):
  DATABASE_URL               # Render Postgres; inaweza kuwa postgres:// au postgresql://
  RENDER_DATABASE_URL        # jina mbadala (optional)
  LOCAL_DATABASE_URL         # kwa dev
  DB_USER, DB_PASSWORD, DB_HOST, DB_PORT, DB_NAME  # fallback compose

Vingine (optional):
  ENVIRONMENT=production|staging|development      (default: development)
  DEBUG=true|false
  DB_ECHO=true|false                              (print SQL)
  DB_POOL_SIZE=5
  DB_MAX_OVERFLOW=10
  DB_POOL_TIMEOUT=30
  DB_POOL_RECYCLE=1800
  DB_USE_PGBOUNCER=true|false                     (Render + pgbouncer → tumia NullPool)
  DB_REQUIRE_PASSWORD=true|false                  (kuziba URL zisizo na password)
  DB_SELF_CHECK=true|false                        (jaribu SELECT 1)
  DB_STATEMENT_TIMEOUT_MS=30000                   (SET statement_timeout)
  DATABASE_SSLMODE=require|verify-ca|verify-full  (default: require in prod)
  DATABASE_SSLROOTCERT=/path/to/ca.pem
  DB_APPLICATION_NAME="smartbiz-backend"
"""

from __future__ import annotations

import os
import sys
from contextlib import contextmanager
from typing import Iterator, Dict, Any

from sqlalchemy import create_engine, text, event
from sqlalchemy.engine.url import make_url
from sqlalchemy.orm import sessionmaker, declarative_base
from sqlalchemy.pool import NullPool

# ───────────────────────────── Env & flags ─────────────────────────────

def _env(key: str, default: str = "") -> str:
    v = os.getenv(key)
    return v if v is not None else default

ENV_MODE = (_env("ENVIRONMENT") or _env("ENV") or "development").strip().lower()
IS_PROD = ENV_MODE in {"production", "prod", "staging"} or bool(_env("RENDER_SERVICE_ID"))
DEBUG = (_env("DEBUG", "false").lower() == "true") or (ENV_MODE == "development")
ECHO_SQL = _env("DB_ECHO", "false").lower() == "true" or DEBUG
SELF_CHECK = _env("DB_SELF_CHECK", "false").lower() == "true"
REQUIRE_PG_PASSWORD = _env("DB_REQUIRE_PASSWORD", "false").lower() == "true"
USE_PGBOUNCER = _env("DB_USE_PGBOUNCER", "false").lower() == "true"

POOL_SIZE = int(_env("DB_POOL_SIZE", "5"))
MAX_OVERFLOW = int(_env("DB_MAX_OVERFLOW", "10"))
POOL_TIMEOUT = int(_env("DB_POOL_TIMEOUT", "30"))
POOL_RECYCLE = int(_env("DB_POOL_RECYCLE", "1800"))  # sekunde (30min)
STATEMENT_TIMEOUT_MS = int(_env("DB_STATEMENT_TIMEOUT_MS", "0"))  # 0 = usiweke
APP_NAME = _env("DB_APPLICATION_NAME", "smartbiz-backend")

# ───────────────────────────── Helpers ─────────────────────────────

def _mask_url(url: str) -> str:
    try:
        if "://" not in url or "@" not in url:
            return url
        scheme, rest = url.split("://", 1)
        creds, tail = rest.split("@", 1)
        if ":" in creds:
            user, _ = creds.split(":", 1)
            return f"{scheme}://{user}:*****@{tail}"
        return url
    except Exception:
        return url

def _coerce_postgres(url: str) -> str:
    # weka driver wazi: psycopg2 (sync)
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql+psycopg2://", 1)
    elif url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+psycopg2://", 1)
    return url

def _compose_from_parts() -> str:
    user = _env("DB_USER", "postgres")
    pwd  = _env("DB_PASSWORD", "")
    host = _env("DB_HOST", "localhost")
    port = _env("DB_PORT", "5432")
    name = _env("DB_NAME", "smartbiz_db")
    if not (user and name):
        raise RuntimeError(
            "DB ERROR: Hakuna DATABASE_URL na DB_* hazijatosha. "
            "Weka DATABASE_URL au DB_USER/PASSWORD/HOST/PORT/NAME."
        )
    return f"postgresql+psycopg2://{user}:{pwd}@{host}:{port}/{name}"

def _choose_database_url() -> str:
    url = _env("DATABASE_URL") or _env("RENDER_DATABASE_URL") or _env("LOCAL_DATABASE_URL")
    if not url:
        url = _compose_from_parts()
    return _coerce_postgres(url.strip())

def _validate_url(url: str) -> str:
    try:
        u = make_url(url)
    except Exception as e:
        raise RuntimeError(f"Invalid DATABASE_URL: {url!r} ({e})")
    if not str(u.drivername).startswith("postgresql"):
        raise RuntimeError(f"Driver '{u.drivername}' si PostgreSQL; tumia 'postgresql+psycopg2://...'")
    if not u.host or not u.database:
        raise RuntimeError("DATABASE_URL inahitaji host na database.")
    if REQUIRE_PG_PASSWORD and (u.password in (None, "")):
        raise RuntimeError("DB_REQUIRE_PASSWORD=true lakini URL haina password.")
    return url

def _ssl_connect_args(url: str) -> Dict[str, Any]:
    # psycopg2 husoma via connect_args
    if not url.startswith("postgresql+psycopg2://"):
        return {}
    sslmode = _env("DATABASE_SSLMODE") or ("require" if IS_PROD else None)
    sslrootcert = _env("DATABASE_SSLROOTCERT") or None
    args: Dict[str, Any] = {}
    if sslmode:
        args["sslmode"] = sslmode
    if sslrootcert:
        args["sslrootcert"] = sslrootcert
    return args

# ───────────────────────────── Engine & Session ─────────────────────────────

DB_URL = _validate_url(_choose_database_url())

_engine_kwargs: Dict[str, Any] = dict(
    future=True,
    pool_pre_ping=True,   # huondoa stale connections
    echo=ECHO_SQL,
    connect_args=_ssl_connect_args(DB_URL),
)

# kama unatumia pgbouncer (kama Render inavyopendekeza), usitumie pool ya SQLAlchemy
if USE_PGBOUNCER:
    _engine_kwargs["poolclass"] = NullPool
else:
    _engine_kwargs.update(
        pool_size=POOL_SIZE,
        max_overflow=MAX_OVERFLOW,
        pool_timeout=POOL_TIMEOUT,
        pool_recycle=POOL_RECYCLE,
    )

engine = create_engine(DB_URL, **_engine_kwargs)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
Base = declarative_base()

print(f"[DB] Using: {_mask_url(DB_URL)}  (env={ENV_MODE}, prod={IS_PROD}, echo={ECHO_SQL}, pgbouncer={USE_PGBOUNCER})")

# Tunapounganisha, weka vitu vya session (UTC, application_name, statement_timeout)
@event.listens_for(engine, "connect")
def _set_session_settings(dbapi_conn, connection_record):  # pragma: no cover
    try:
        cur = dbapi_conn.cursor()
        # time zone + app name
        cur.execute("SET TIME ZONE 'UTC'")
        if APP_NAME:
            cur.execute("SET application_name = %s", (APP_NAME,))
        # optional statement timeout
        if STATEMENT_TIMEOUT_MS and STATEMENT_TIMEOUT_MS > 0:
            cur.execute(f"SET statement_timeout = {int(STATEMENT_TIMEOUT_MS)}")
        cur.close()
    except Exception as e:
        # tusizuie boot ingawa ni bora
        sys.stderr.write(f"[DB] session settings failed: {e}\n")

# Self-check (hiari)
if SELF_CHECK:
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        print("[DB] self-check: OK")
    except Exception as exc:
        print(f"[DB] self-check: FAILED -> {exc}")

# ───────────────────────────── FastAPI dependency ─────────────────────────────

def get_db() -> Iterator:
    """FastAPI dependency: with SessionLocal() as db: yield db"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# ───────────────────────────── Convenience context ─────────────────────────────

@contextmanager
def session_scope() -> Iterator:
    """Context manager kwa scripts/jobs nje ya FastAPI."""
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

# ───────────────────────────── Health helper (optional) ─────────────────────────────

def db_healthcheck() -> Dict[str, Any]:
    """
    Tumia kwenye /health au management tasks.
    """
    try:
        with engine.connect() as conn:
            val = conn.execute(text("SELECT now() AT TIME ZONE 'UTC'")).scalar_one()
        return {"ok": True, "time_utc": str(val)}
    except Exception as e:
        return {"ok": False, "error": str(e)}
