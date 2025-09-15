# backend/db.py
# -*- coding: utf-8 -*-
"""
SmartBiz Assistance – SQLAlchemy (sync) for PostgreSQL on Render

Env vars (yenye kipaumbele):
  - DATABASE_URL                 # <— Render Postgres; huenda ikawa "postgres://" au "postgresql://"
  - RENDER_DATABASE_URL          # (hiari) jina mbadala ukipenda
  - LOCAL_DATABASE_URL           # (dev)
  - DB_USER, DB_PASSWORD, DB_HOST, DB_PORT, DB_NAME  # (fallback compose)

Vingine:
  - ENVIRONMENT                  # production | staging | development (default: development)
  - DB_ECHO=true|false           # print SQL
  - DB_POOL_SIZE, DB_MAX_OVERFLOW, DB_POOL_TIMEOUT, DB_POOL_RECYCLE
  - DB_REQUIRE_PASSWORD=true     # lazimisha password kwenye URL iliyokomaa
  - DB_SELF_CHECK=true           # fanya SELECT 1 wakati wa import
  - DATABASE_SSLMODE             # default 'require' kwenye production
  - DATABASE_SSLROOTCERT         # path ya CA kama unahitaji
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Iterator, Dict, Any

from sqlalchemy import create_engine, text
from sqlalchemy.engine.url import make_url
from sqlalchemy.orm import sessionmaker, declarative_base

# ───────────────────────────── Env & flags ─────────────────────────────

ENV_MODE = (os.getenv("ENVIRONMENT") or os.getenv("ENV") or "development").strip().lower()
IS_PROD = ENV_MODE in {"production", "prod", "staging"} or bool(os.getenv("RENDER_SERVICE_ID"))
DEBUG = (os.getenv("DEBUG", "false").lower() == "true") or (ENV_MODE == "development")
ECHO_SQL = os.getenv("DB_ECHO", "false").lower() == "true" or DEBUG
SELF_CHECK = os.getenv("DB_SELF_CHECK", "false").lower() == "true"
REQUIRE_PG_PASSWORD = os.getenv("DB_REQUIRE_PASSWORD", "false").lower() == "true"

POOL_SIZE = int(os.getenv("DB_POOL_SIZE", "5"))
MAX_OVERFLOW = int(os.getenv("DB_MAX_OVERFLOW", "10"))
POOL_TIMEOUT = int(os.getenv("DB_POOL_TIMEOUT", "30"))
POOL_RECYCLE = int(os.getenv("DB_POOL_RECYCLE", "1800"))  # sekunde (30min)

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
    # Badilisha kuwa driver wa psycopg2 waziwazi
    if url.startswith("postgres://"):
        return url.replace("postgres://", "postgresql+psycopg2://", 1)
    if url.startswith("postgresql://"):
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
            "DB ERROR: Hakuna DATABASE_URL na DB_* hazijatosha. "
            "Weka DATABASE_URL au DB_USER/DB_PASSWORD/DB_HOST/DB_PORT/DB_NAME."
        )
    return f"postgresql+psycopg2://{user}:{pwd}@{host}:{port}/{name}"

def _choose_database_url() -> str:
    # 1) Render/Generic
    url = os.getenv("DATABASE_URL")
    # 2) Mbinu mbadala ya jina (kama hutumii DATABASE_URL)
    if not url:
        url = os.getenv("RENDER_DATABASE_URL")
    # 3) Dev
    if not url:
        url = os.getenv("LOCAL_DATABASE_URL")
    # 4) Compose
    if not url:
        url = _compose_from_parts()
    return _coerce_postgres(url.strip())

def _validate_url(url: str) -> str:
    try:
        u = make_url(url)
    except Exception as e:
        raise RuntimeError(f"Invalid DATABASE_URL: {url!r} ({e})")
    if not u.drivername.startswith("postgresql"):
        raise RuntimeError(
            f"Driver '{u.drivername}' si PostgreSQL. "
            "Tumia 'postgresql+psycopg2://...'"
        )
    if not u.host or not u.database:
        raise RuntimeError("URL lazima iwe na host na database.")
    if REQUIRE_PG_PASSWORD and (u.password in (None, "")):
        raise RuntimeError("Password ni lazima (DB_REQUIRE_PASSWORD=true).")
    return url

def _ssl_connect_args(url: str) -> Dict[str, Any]:
    # psycopg2 inasoma 'sslmode' n.k. kupitia connect_args
    if not url.startswith("postgresql+psycopg2://"):
        return {}
    sslmode = os.getenv("DATABASE_SSLMODE") or ("require" if IS_PROD else None)
    sslrootcert = os.getenv("DATABASE_SSLROOTCERT")
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
    pool_pre_ping=True,
    echo=ECHO_SQL,
    pool_size=POOL_SIZE,
    max_overflow=MAX_OVERFLOW,
    pool_timeout=POOL_TIMEOUT,
    pool_recycle=POOL_RECYCLE,
    connect_args=_ssl_connect_args(DB_URL),
)

engine = create_engine(DB_URL, **_engine_kwargs)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
Base = declarative_base()

print(f"[DB] Using: {_mask_url(DB_URL)}  (env={ENV_MODE}, prod={IS_PROD}, echo={ECHO_SQL})")

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
    """Context manager kwa matumizi ya nje ya FastAPI (scripts, jobs)."""
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
