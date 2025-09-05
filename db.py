# backend/db.py
# -*- coding: utf-8 -*-
"""
SmartBiz Assistance – Database bootstrap (SQLAlchemy sync)
(PostgreSQL ONLY – no SQLite fallback)

Priority for DB URL:
  1) DATABASE_URL
  2) (prod) RAILWAY_DATABASE_URL / RENDER_DATABASE_URL / PROD_DATABASE_URL
  3) (dev)  LOCAL_DATABASE_URL
  4) Compose from DB_* pieces (user, pass, host, port, name)
If nothing valid is found, raise a clear RuntimeError.

Extras:
- Coerces 'postgres://' → 'postgresql+psycopg2://'
- Optional SSL (DATABASE_SSLMODE, DATABASE_SSLROOTCERT)
- Sensible pools for PostgreSQL
- Self-check (DB_SELF_CHECK=true)
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Iterator, Dict, Any

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker, declarative_base
from sqlalchemy.engine.url import make_url

# ─────────────── Env & flags ───────────────
ENV_MODE = (os.getenv("ENVIRONMENT") or os.getenv("ENV") or "development").strip().lower()
IS_PROD = ENV_MODE in {"production", "prod", "staging"} or any(
    os.getenv(k) for k in (
        "RAILWAY_ENVIRONMENT", "RENDER_SERVICE_ID", "FLY_APP_NAME", "DYNO"  # Railway/Render/Fly/Heroku
    )
)
DEBUG = (os.getenv("DEBUG", "false").lower() == "true") or (ENV_MODE == "development")
ECHO_SQL = os.getenv("DB_ECHO", "false").lower() == "true" or DEBUG
SELF_CHECK = os.getenv("DB_SELF_CHECK", "false").lower() == "true"

# Pool defaults (Postgres)
POOL_SIZE = int(os.getenv("DB_POOL_SIZE", "5"))
MAX_OVERFLOW = int(os.getenv("DB_MAX_OVERFLOW", "10"))
POOL_TIMEOUT = int(os.getenv("DB_POOL_TIMEOUT", "30"))
POOL_RECYCLE = int(os.getenv("DB_POOL_RECYCLE", "1800"))  # 30 min

# Optional stricter check for password
REQUIRE_PG_PASSWORD = os.getenv("DB_REQUIRE_PASSWORD", "false").lower() == "true"


# ─────────────── Helpers ───────────────
def _mask_url(url: str) -> str:
    """Mask password section for safe logging."""
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
    if url.startswith("postgres://"):
        return url.replace("postgres://", "postgresql+psycopg2://", 1)
    if url.startswith("postgresql://"):  # add driver hint
        return url.replace("postgresql://", "postgresql+psycopg2://", 1)
    return url


def _compose_from_parts() -> str:
    user = os.getenv("DB_USER", "postgres")
    pwd = os.getenv("DB_PASSWORD", "")
    host = os.getenv("DB_HOST", "localhost")
    port = os.getenv("DB_PORT", "5432")
    name = os.getenv("DB_NAME", "smartbiz_db")

    if not (user and name):
        raise RuntimeError(
            "DB ERROR: No DATABASE_URL and incomplete DB_* parts. "
            "Provide DATABASE_URL or set DB_USER/DB_PASSWORD/DB_NAME."
        )
    return f"postgresql+psycopg2://{user}:{pwd}@{host}:{port}/{name}"


def _choose_database_url() -> str:
    # 1) Direct env
    url = os.getenv("DATABASE_URL")

    # 2) Production fallbacks
    if not url and IS_PROD:
        url = (
            os.getenv("RAILWAY_DATABASE_URL")
            or os.getenv("RENDER_DATABASE_URL")
            or os.getenv("PROD_DATABASE_URL")
        )

    # 3) Dev override
    if not url:
        url = os.getenv("LOCAL_DATABASE_URL")

    # 4) Compose (Postgres only)
    if not url:
        url = _compose_from_parts()

    if not url:
        raise RuntimeError(
            "DATABASE_URL is missing. Provide a PostgreSQL URL via "
            "DATABASE_URL/LOCAL_DATABASE_URL/RAILWAY_DATABASE_URL/etc., "
            "or set DB_* pieces."
        )

    return _coerce_postgres(url.strip())


def _ssl_connect_args(url: str) -> Dict[str, Any]:
    """Return connect_args for drivers that accept SSL params (psycopg2)."""
    if not url.startswith("postgresql+psycopg2://"):
        return {}
    sslmode = os.getenv("DATABASE_SSLMODE")      # e.g. "require"
    sslrootcert = os.getenv("DATABASE_SSLROOTCERT")  # path to CA cert
    args: Dict[str, Any] = {}
    if sslmode:
        args["sslmode"] = sslmode
    if sslrootcert:
        args["sslrootcert"] = sslrootcert
    return args


def _validate_url(url: str) -> str:
    """Validate that the URL is PostgreSQL and structurally correct."""
    try:
        u = make_url(url)
    except Exception as e:
        raise RuntimeError(f"Invalid DATABASE_URL: {url!r} ({e})")

    if not u.drivername.startswith("postgresql"):
        raise RuntimeError(
            f"Only PostgreSQL is allowed. Got driver '{u.drivername}'. "
            "Use postgresql+psycopg2:// or postgresql+psycopg://"
        )

    if not u.host or not u.database:
        raise RuntimeError("PostgreSQL URL must include host and database name.")

    if REQUIRE_PG_PASSWORD and (u.password in (None, "")):
        raise RuntimeError("PostgreSQL URL must include a password (DB_REQUIRE_PASSWORD=true).")

    return url


# ─────────────── Engine & Session setup ───────────────
DB_URL = _validate_url(_choose_database_url())

_engine_kwargs: Dict[str, Any] = dict(
    pool_pre_ping=True,
    echo=ECHO_SQL,
    future=True,
    pool_size=POOL_SIZE,
    max_overflow=MAX_OVERFLOW,
    pool_timeout=POOL_TIMEOUT,
    pool_recycle=POOL_RECYCLE,
    connect_args=_ssl_connect_args(DB_URL),
)

engine = create_engine(DB_URL, **_engine_kwargs)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
Base = declarative_base()

print(f" Using DB: {_mask_url(DB_URL)}  (env: {ENV_MODE}, prod={IS_PROD}, echo_sql={ECHO_SQL})")

# Optional connection self-check
if SELF_CHECK:
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        print(" DB self-check: OK")
    except Exception as exc:
        print(f" DB self-check: FAILED -> {exc}")


# ─────────────── FastAPI dependency ───────────────
def get_db() -> Iterator:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ─────────────── Convenience API ───────────────
@contextmanager
def session_scope() -> Iterator:
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
