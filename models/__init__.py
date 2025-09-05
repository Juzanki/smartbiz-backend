# -*- coding: utf-8 -*-
"""
Auto-register all SQLAlchemy models in a safe, deterministic order.

- Strong debug tracing (load order + ms + errors).
- Fail-fast on dev / STRICT_MODE=1.
- Early mapper verification (configure_mappers()).
- Flexible allow/deny via env: MODELS_ONLY, MODELS_EXCLUDE.
- Stable import order for cross-linked models (live_stream → gift_movement → gift_transaction → user).
- __all__ exported dynamically from Base.registry.mappers.
"""
from __future__ import annotations

import importlib
import logging
import os
import pkgutil
import time
import traceback
from pathlib import Path
from typing import Iterable

# ✅ Tumia local package (SI backend.*)
from db import Base, engine  # noqa: F401

log = logging.getLogger("smartbiz.models")

_PKG_NAME = __name__                 # "models"
_PKG_PATH = Path(__file__).resolve().parent

# ───────────────────────── Env flags ─────────────────────────
_ENV = (os.getenv("ENVIRONMENT") or "development").strip().lower()
_DEV = _ENV in {"dev", "development", "local"}
_STRICT = os.getenv("STRICT_MODE", "0").strip().lower() in {"1", "true", "yes", "on", "y"}

def _env_list(name: str) -> list[str]:
    raw = os.getenv(name) or ""
    return [s.strip() for s in raw.split(",") if s.strip()]

_ONLY = set(_env_list("MODELS_ONLY"))
_EXCL = set(_env_list("MODELS_EXCLUDE"))
# Optional explicit order override via env (e.g. "live_stream,gift_movement,gift_transaction,user")
_ENV_HARD_ORDER = _env_list("MODELS_HARD_ORDER")

# ─────────────────────── Import ordering ───────────────────────
_CRITICAL_FIRST: tuple[str, ...] = (
    "live_stream",
    "gift_movement",
    "gift_transaction",
    "user",
)
_PREFERRED_NEXT: tuple[str, ...] = ("guest", "guests", "co_host")
_PREFERRED_LATE: tuple[str, ...] = ("order", "product", "products_live")

_ORDER_RULES: tuple[tuple[str, str], ...] = (
    ("live_stream", "gift_movement"),
    ("gift_movement", "gift_transaction"),
    ("gift_transaction", "user"),
)

def _is_hidden_or_legacy(name: str) -> bool:
    if name.startswith("_") or name in {"__init__", "__pycache__"}:
        return True
    suffixes = (".bak", ".backup", ".old", "~", ".tmp", ".swp", ".swo")
    return any(tok in name for tok in suffixes) or name.endswith("_legacy")

def _discover() -> list[str]:
    mods: list[str] = []
    for _, modname, ispkg in pkgutil.iter_modules([str(_PKG_PATH)]):
        if ispkg or _is_hidden_or_legacy(modname):
            continue
        mods.append(modname)
    if _ONLY:
        mods = [m for m in mods if m in _ONLY]
    if _EXCL:
        mods = [m for m in mods if m not in _EXCL]
    if "guest" in mods and "guests" in mods:
        mods.remove("guests")
        log.warning("Both 'guest' and 'guests' modules found; preferring 'guest'.")
    return mods

def _apply_rules(seq: list[str], rules: Iterable[tuple[str, str]]) -> None:
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

def _order_modules(found: Iterable[str]) -> list[str]:
    pool = list(found)
    ordered: list[str] = []

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
    importlib.import_module(fq)   # idempotent
    dt_ms = (time.perf_counter() - t0) * 1000.0
    log.debug("Loaded model module: %s (%.1f ms)", fq, dt_ms)

def _configure_mappers_now() -> None:
    from sqlalchemy.orm import configure_mappers
    configure_mappers()
    log.info("SQLAlchemy mapper configuration OK.")

def _post_verify(loaded: list[str]) -> None:
    if not (_DEV or _STRICT):
        return
    registry = getattr(Base, "registry", None)
    names = set(getattr(registry, "_class_registry", {})) if registry else set()
    expected: list[str] = []
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
    seen: set[str] = set()
    for mapper in sorted(getattr(Base, "registry").mappers, key=lambda m: m.class_.__name__):
        cls = mapper.class_
        name = cls.__name__
        globals()[name] = cls
        if name not in seen:
            __all__.append(name)
            seen.add(name)

# ─────────────────────── Public loader (idempotent) ───────────────────────
_LOADED_ONCE = False
_LOADED: list[str] = []
_SKIPPED: list[str] = []

def load_models() -> tuple[list[str], list[str]]:
    """Import all model modules and configure mappers. Safe to call multiple times."""
    global _LOADED_ONCE, _LOADED, _SKIPPED
    if _LOADED_ONCE:
        return _LOADED, _SKIPPED

    # Log engine URL if available
    try:
        log.info("Using DB: %s (env=%s)", engine.url, _ENV)  # type: ignore[attr-defined]
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
            except Exception as e:
                tb = traceback.format_exc(limit=3)
                _SKIPPED.append(f"{m} ← {e.__class__.__name__}: {e}")
                if _STRICT or _DEV:
                    raise
                log.warning("Skipped model '%s' due to %s: %s\n%s", m, e.__class__.__name__, e, tb)

        _configure_mappers_now()
        _post_verify(_LOADED)

        if _LOADED:
            log.info("Models loaded (%d): %s", len(_LOADED), ", ".join(_LOADED))
        if _SKIPPED:
            log.warning("Models skipped (%d): %s", len(_SKIPPED), " | ".join(_SKIPPED))

        _LOADED_ONCE = True
        _rebuild_exports()
        return _LOADED, _SKIPPED

    except Exception as boot_err:
        log.error("Model auto-loader failed: %s", boot_err, exc_info=True)
        if _STRICT or _DEV:
            raise
        return _LOADED, _SKIPPED

# Run immediately on import (common usage)
load_models()
