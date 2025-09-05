# -*- coding: utf-8 -*-
# env.py — Alembic bootstrap (SmartBiz)
from __future__ import annotations

import os
import sys
import logging
import importlib
import pkgutil
from pathlib import Path
from logging.config import fileConfig
from typing import Iterable

from alembic import context
from sqlalchemy import engine_from_config, pool

# -----------------------------------------------------------------------------
# 1) Put project root on sys.path
#    <repo>/backend/alembic/env.py -> ROOT == <repo>
# -----------------------------------------------------------------------------
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# -----------------------------------------------------------------------------
# 2) Import Base (declarative metadata)
# -----------------------------------------------------------------------------
from backend.db import Base  # noqa: E402

log = logging.getLogger("alembic.env")

# -----------------------------------------------------------------------------
# 3) Import all model modules exactly once (walk nested packages)
# -----------------------------------------------------------------------------
def _iter_model_modules() -> Iterable[str]:
    """Yield import paths for modules under backend.models (recursively)."""
    import backend.models as models_pkg  # noqa: WPS433
    prefix = models_pkg.__name__ + "."
    for finder, name, ispkg in pkgutil.walk_packages(models_pkg.__path__, prefix=prefix):
        short = name.rsplit(".", 1)[-1]
        # skip private or obvious non-model files
        if short.startswith("_") or short.endswith(("_test", "_tests")):
            continue
        yield name

def import_all_models() -> list[str]:
    """Import every module under backend.models once so all mappers register."""
    imported: list[str] = []
    for name in _iter_model_modules():
        if name in sys.modules:
            continue
        try:
            importlib.import_module(name)
            imported.append(name)
        except Exception as exc:
            # Don't block migrations because of one bad module
            log.warning("Skipping model module %s due to import error: %s", name, exc)
    return imported

_IMPORTED = import_all_models()
if _IMPORTED:
    log.info("Imported models: %s", ", ".join(_IMPORTED))

# -----------------------------------------------------------------------------
# 4) Alembic config & DB URL discovery
# -----------------------------------------------------------------------------
config = context.config
if config.config_file_name:
    fileConfig(config.config_file_name)  # logging config from alembic.ini

def get_url() -> str:
    # Prefer common env vars
    for key in ("DATABASE_URL", "SQLALCHEMY_DATABASE_URL", "SQLALCHEMY_DATABASE_URI"):
        val = os.getenv(key)
        if val:
            return val
    # Fallback: use project's engine if present
    try:
        from backend.db import engine  # noqa: WPS433
        return str(engine.url)
    except Exception:
        # Last resort: local sqlite so "revision --autogenerate" still runs
        return "sqlite:///./smartbiz.db"

config.set_main_option("sqlalchemy.url", get_url())

# What Alembic uses to autogenerate migrations
target_metadata = Base.metadata

# Optional: control where alembic stores its version table (good for Postgres)
VERSION_TABLE = os.getenv("ALEMBIC_VERSION_TABLE", "alembic_version")
VERSION_SCHEMA = os.getenv("ALEMBIC_VERSION_SCHEMA", "public")

# -----------------------------------------------------------------------------
# 5) Autogenerate tuning
# -----------------------------------------------------------------------------
def include_object(object, name, type_, reflected, compare_to):
    """
    Skip objects that exist ONLY in the database (would be DROPs),
    but allow objects that exist only in metadata (CREATEs).

    Set ALEMBIC_ALLOW_DROPS=1 to allow autogenerate to propose DROPs.
    """
    allow_drops = os.getenv("ALEMBIC_ALLOW_DROPS", "0") in ("1", "true", "True")
    if not allow_drops and reflected and compare_to is None:
        # Example: table/index present in DB but missing in models → would drop → skip
        return False
    return True

def process_revision_directives(context_, revision, directives):
    """
    Avoid creating empty migration files when nothing changed after filtering.
    """
    if not directives:
        return
    script = directives[0]
    if getattr(script, "upgrade_ops", None) and not script.upgrade_ops.is_empty():
        return
    log.info("No schema changes detected; skipping empty migration file.")
    directives[:] = []  # drop the directive → Alembic won't create a file

COMMON_CONFIG = dict(
    target_metadata=target_metadata,
    include_object=include_object,
    compare_type=True,               # column type diffs
    compare_server_default=True,     # DEFAULT diffs
    render_as_batch=False,           # Postgres: no need for batch
    process_revision_directives=process_revision_directives,
    version_table=VERSION_TABLE,
    version_table_schema=VERSION_SCHEMA,
    # include_schemas=False,         # enable if you manage multiple schemas
)

# -----------------------------------------------------------------------------
# 6) Migration runners
# -----------------------------------------------------------------------------
def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        literal_binds=True,
        **COMMON_CONFIG,
    )
    with context.begin_transaction():
        context.run_migrations()

def run_migrations_online() -> None:
    """Run migrations in 'online' mode."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            **COMMON_CONFIG,
        )
        with context.begin_transaction():
            context.run_migrations()

if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
