# -*- coding: utf-8 -*-
"""
Auto-register all SQLAlchemy models in a safe, deterministic order.

- Strong debug tracing (load order + ms + errors).
- Fail-fast on dev / STRICT_MODE=1.
- Early mapper verification (configure_mappers()).
- Flexible allow/deny via env: MODELS_ONLY, MODELS_EXCLUDE.
- Stable import order for cross-linked models.
- Works whether the project is imported as "backend.*" or run from the backend
  directory with "main:app" (no top-level 'backend' package).
"""
from __future__ import annotations

import importlib
import logging
import os
import pkgutil
import sys
import time
import traceback
import types
from pathlib import Path
from typing import Iterable, List, Tuple

# ────────────────────── Make 'backend' importable if missing ──────────────────
# When running from the backend directory (uvicorn main:app), 'backend' may not
# be a top-level package name. Create a lightweight alias so imports like
# "from backend.db import Base" keep working inside individual model modules.
_THIS_DIR = Path(__file__).resolve().parent                  # .../backend/models
_BACKEND_ROOT = _THIS_DIR.parent                             # .../backend
if "backend" not in sys.modules:
    _backend_mod = types.ModuleType("backend")
    # Allow importing backend.<anything> from the backend folder
    _backend_mod.__path__ = [str(_BACKEND_ROOT)]
    sys.modules["backend"] = _backend_mod

# ───────────────── Base/engine (robust import) ─────────────────
# Try relative first (when package is 'backend.models'), then local ('models'
# used as top-level), and finally fully-qualified.
try:
    from ..db import Base, engine  # type: ignore
except Exception:  # noqa: BLE001
    try:
        from db import Base, engine  # type: ignore
    except Exception:  # noqa: BLE001
        from backend.db import Base, engine  # type: ignore  # last resort

log = logging.getLogger("smartbiz.models")

_PKG_NAME = __name__                 # "backend.models" or simply "models"
_PKG_PATH = _THIS_DIR

# ───────────────────────── Env flags ─────────────────────────
_ENV = (os.getenv("ENVIRONMENT") or "development").strip().lower()
_DEV = _ENV in {"dev", "development", "local"}
_STRICT = os.getenv("STRICT_MODE", "0").strip().lower() in {"1", "true", "yes", "on", "y"}

def _env_list(name: str) -> List[str]:
    raw = os.getenv(name) or ""
    return [s.strip() for s in raw.split(",") if s.strip()]

_ONLY = set(_env_list("MODELS_ONLY"))
_EXCL = set(_env_list("MODELS_EXCLUDE"))
# Optional explicit order override via env (e.g. "live_stream,gift_movement,gift_transaction,user")
_ENV_HARD_ORDER = _env_list("MODELS_HARD_ORDER")

# ─────────────────────── Import ordering ───────────────────────
_CRITICAL_FIRST: Tuple[str, ...] = (
    "live_stream",
    "gift_movement",
    "gift_transaction",
    "user",
)
_PREFERRED_NEXT: Tuple[str, ...] = ("guest", "guests", "co_host")
_PREFERRED_LATE: Tuple[str, ...] = ("order", "product", "products_live")

_ORDER_RULES: Tuple[Tuple[str, str], ...] = (
    ("live_stream", "gift_movement"),
    ("gift_movement", "gift_transaction"),
    ("gift_transaction", "user"),
)

def _is_hidden_or_legacy(name: str) -> bool:
    if name.startswith("_") or name in {"__init__", "__pycache__"}:
        return True
    suffixes = (".bak", ".backup", ".old", "~", ".tmp", ".swp", ".swo")
    return any(tok in name for tok in suffixes) or name.endswith("_legacy")

def _discover() -> List[str]:
    mods: List[str] = []
    for _, modname, ispkg in pkgutil.iter_modules([str(_PKG_PATH)]):
        if ispkg or _is_hidden_or_legacy(modname):
            continue
        mods.append(modname)
    if _ONLY:
        mods = [m for m in mods if m in _ONLY]
    if _EXCL:
        mods = [m for m in mods if m not in _EXCL]
    # Prefer 'guest' over 'guests' if both exist
    if "guest" in mods and "guests" in mods:
        mods.remove("guests")
        log.warning("Both 'guest' and 'guests' modules found; preferring 'guest'.")
    return mods

def _apply_rules(seq: List[str], rules: Iterable[Tuple[str, str]]) -> None:
    changed = True
    while changed:
        changed = False
        for a, b in rules:
            if a in seq and b in seq:
                ia, ib = seq.index(a), seq.index(b)
                if ia > ib:
                    item = seq.pop(ia)
                    ib = seq.index(b)
                    seq.insert(ib, item)
                    changed = True

def _order_modules(found: Iterable[str]) -> List[str]:
    pool = list(found)
    ordered: List[str] = []

    def take(names: Iterable[str]):
        for n in list(names):
            if n in pool:
                ordered.append(n)
                pool.remove(n)

    if _ENV_HARD_ORDER:
        take(_ENV_HARD_ORDER)
    take(_CRITICAL_FIRST)
    take(_PREFERRED_NEXT)
    take(_PREFERRED_LATE)
    ordered.extend(sorted(pool))
    _apply_rules(ordered, _ORDER_RULES)
    return ordered

def _safe_import(module_basename: str) -> None:
    fq = f"{_PKG_NAME}.{module_basename}"
    t0 = time.perf_counter()
    importlib.import_module(fq)   # idempotent if already imported
    dt_ms = (time.perf_counter() - t0) * 1000.0
    log.debug("Loaded model module: %s (%.1f ms)", fq, dt_ms)

def _configure_mappers_now() -> None:
    from sqlalchemy.orm import configure_mappers
    configure_mappers()
    log.info("SQLAlchemy mapper configuration OK.")

def _post_verify(loaded: List[str]) -> None:
    if not (_DEV or _STRICT):
        return
    registry = getattr(Base, "registry", None)
    names = set(getattr(registry, "_class_registry", {})) if registry else set()
    expected = []
    if "gift_movement" in loaded:
        expected.append("GiftMovement")
    if "gift_transaction" in loaded:
        expected.append("GiftTransaction")
    if "live_stream" in loaded:
        expected.append("LiveStream")
    if "user" in loaded:
        expected.append("User")
    missing = [cls for cls in expected if cls not in names]
    if missing:
        msg = f"Post-verify failed: missing mapped classes: {missing}"
        if _STRICT:
            raise RuntimeError(msg)
        log.warning(msg)

# ─────────────────────── Dynamic __all__ exports ───────────────────────
__all__: list[str] = []

def _rebuild_exports() -> None:
    __all__.clear()
    seen = set()
    for mapper in sorted(getattr(Base.registry, "mappers", []), key=lambda m: m.class_.__name__):
        cls = mapper.class_
        name = cls.__name__
        globals()[name] = cls
        if name not in seen:
            __all__.append(name)
            seen.add(name)

# ─────────────────────── Public loader (idempotent) ───────────────────────
_LOADED_ONCE = False
_LOADED: List[str] = []
_SKIPPED: List[str] = []

def load_models() -> tuple[list[str], list[str]]:
    """Import all model modules and configure mappers. Safe to call multiple times."""
    global _LOADED_ONCE, _LOADED, _SKIPPED
    if _LOADED_ONCE:
        return _LOADED, _SKIPPED

    # Log engine URL if available (mask elsewhere)
    try:
        log.info("Using DB engine: %s (env=%s)", engine.url, _ENV)
    except Exception:
        pass

    try:
        discovered = _discover()
        ordered = _order_modules(discovered)
        log.debug("Model import order: %s", " -> ".join(ordered))

        for m in ordered:
            try:
                _safe_import(m)
                _LOADED.append(m)
            except Exception as e:  # noqa: BLE001
                tb = traceback.format_exc(limit=3)
                _SKIPPED.append(f"{m} ← {e.__class__.__name__}: {e}")
                if _STRICT or _DEV:
                    raise
                log.warning(
                    "Skipped model '%s' due to %s: %s\n%s",
                    m, e.__class__.__name__, e, tb
                )

        _configure_mappers_now()
        _post_verify(_LOADED)

        if _LOADED:
            log.info("Models loaded (%d): %s", len(_LOADED), ", ".join(_LOADED))
        if _SKIPPED:
            log.warning("Models skipped (%d): %s", len(_SKIPPED), " | ".join(_SKIPPED))

        _LOADED_ONCE = True
        _rebuild_exports()
        return _LOADED, _SKIPPED

    except Exception as boot_err:  # noqa: BLE001
        log.error("Model auto-loader failed: %s", boot_err, exc_info=True)
        if _STRICT or _DEV:
            raise
        return _LOADED, _SKIPPED

# Run immediately on import (common usage)
load_models()
