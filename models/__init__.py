# -*- coding: utf-8 -*-
"""
Auto-register all SQLAlchemy models in a safe, deterministic order.

Features
- Single canonical models package (prevents double-imports like 'models.*' vs 'backend.models.*').
- Strong debug tracing (load order + ms + errors).
- Fail-fast on dev / STRICT_MODE=1.
- Early mapper verification (configure_mappers()).
- Flexible allow/deny via env: MODELS_ONLY, MODELS_EXCLUDE, MODELS_HARD_ORDER.
- Stable import order for cross-linked models.
- Exports mapped classes into backend.models globals (__all__), so:
    from backend.models import LiveStream

Env vars (optional)
- ENVIRONMENT=development|production
- STRICT_MODE=1 (raise on any skip/dup)
- MODELS_ONLY="live_stream,user"
- MODELS_EXCLUDE="legacy_model,old_stuff"
- MODELS_HARD_ORDER="live_stream,gift_movement,gift_transaction,user"
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
from typing import Dict, Iterable, List, Tuple

# ─────────────────────────── Paths & package aliases ───────────────────────────
_THIS_DIR = Path(__file__).resolve().parent            # .../backend/models
_BACKEND_ROOT = _THIS_DIR.parent                       # .../backend

# If app is run as "uvicorn main:app" from backend/, ensure 'backend' exists
if "backend" not in sys.modules:
    _backend_mod = types.ModuleType("backend")
    _backend_mod.__path__ = [str(_BACKEND_ROOT)]
    sys.modules["backend"] = _backend_mod

# Make this module the canonical models package under BOTH names:
#   - 'backend.models'
#   - 'models'
_pkg_obj = sys.modules.get(__name__)
if _pkg_obj is not None:
    sys.modules.setdefault("backend.models", _pkg_obj)
    sys.modules.setdefault("models", _pkg_obj)

# ─────────────────────────── Base / engine import ──────────────────────────────
# IMPORTANT: Every model file should import Base from backend.models (this module).
#            Never create another Declarative Base elsewhere.
try:
    from ..db import Base, engine  # when imported as backend.models
except Exception:  # noqa: BLE001
    try:
        from db import Base, engine  # when imported as top-level models
    except Exception:  # noqa: BLE001
        from backend.db import Base, engine  # last resort (requires alias above)

log = logging.getLogger("smartbiz.models")

_PKG_NAME = "backend.models" if "backend" in __name__ else __name__
_PKG_PATH = _THIS_DIR

# ─────────────────────────────── Env flags ─────────────────────────────────────
_ENV = (os.getenv("ENVIRONMENT") or "development").strip().lower()
_DEV = _ENV in {"dev", "development", "local"}
_STRICT = os.getenv("STRICT_MODE", "0").strip().lower() in {"1", "true", "yes", "on", "y"}

def _env_list(name: str) -> List[str]:
    raw = os.getenv(name) or ""
    return [s.strip() for s in raw.split(",") if s.strip()]

_ONLY = set(_env_list("MODELS_ONLY"))
_EXCL = set(_env_list("MODELS_EXCLUDE"))
_ENV_HARD_ORDER = _env_list("MODELS_HARD_ORDER")

# ───────────────────────────── Import ordering ─────────────────────────────────
# Tweak to your domain: these help with inter-dependent models
_CRITICAL_FIRST: Tuple[str, ...] = (
    "live_stream",
    "gift_movement",
    "gift_transaction",
    "user",
)
_PREFERRED_NEXT: Tuple[str, ...] = ("guest", "guests", "co_host")
_PREFERRED_LATE: Tuple[str, ...] = ("order", "product", "products_live")

# Soft constraints (a before b)
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
                    ib = seq.index(b)  # index might change
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

# ───────────────────── Duplicates & mapper verification ───────────────────────
def _collect_registry_names() -> Dict[str, List[str]]:
    """
    Return mapping of ClassName -> [fully.qualified.module.ClassName, ...]
    Only for entries that look like user classes (capitalized names).
    """
    out: Dict[str, List[str]] = {}
    reg = getattr(Base, "registry", None)
    class_registry = getattr(reg, "_class_registry", {}) if reg else {}
    for key, val in class_registry.items():
        if not isinstance(key, str):
            continue
        if not key[:1].isupper():  # skip internals
            continue
        cls = val  # SQLAlchemy stores class itself
        mod = getattr(cls, "__module__", "<unknown>")
        name = getattr(cls, "__name__", key)
        out.setdefault(name, []).append(f"{mod}.{name}")
    return out

def _fail_on_duplicates() -> None:
    """
    Detect same ClassName mapped from different modules (common cause:
    package imported as both 'models.*' and 'backend.models.*').
    """
    dupes = {k: v for k, v in _collect_registry_names().items() if len(v) > 1}
    if dupes:
        lines = ["Duplicate mapped class names detected:"]
        for k, paths in sorted(dupes.items()):
            lines.append(f"  - {k}:")
            for p in paths:
                lines.append(f"      • {p}")
        lines.append(
            "Hints:\n"
            "  • Ensure ALL models import Base from 'backend.models' only.\n"
            "  • Avoid importing the models package via two paths ('models' vs 'backend.models').\n"
            "  • Prefer module-qualified relationship targets, e.g. "
            "\"backend.models.live_stream.LiveStream\".\n"
            "  • This module already aliases both names to ONE package, but if duplicates "
            "exist, you likely imported before aliasing, or defined a class twice."
        )
        msg = "\n".join(lines)
        if _STRICT or _DEV:
            raise RuntimeError(msg)
        log.error(msg)

def _configure_mappers_now() -> None:
    from sqlalchemy.orm import configure_mappers
    configure_mappers()
    log.info("SQLAlchemy mapper configuration OK.")

def _post_verify(loaded: List[str]) -> None:
    # Minimal sanity checks for key classes
    reg_names = _collect_registry_names()
    expected = []
    if "gift_movement" in loaded:
        expected.append("GiftMovement")
    if "gift_transaction" in loaded:
        expected.append("GiftTransaction")
    if "live_stream" in loaded:
        expected.append("LiveStream")
    if "user" in loaded:
        expected.append("User")
    missing = [cls for cls in expected if cls not in reg_names]
    if missing:
        msg = f"Post-verify: missing mapped classes {missing} (loaded={loaded})"
        if _STRICT:
            raise RuntimeError(msg)
        log.warning(msg)

# ───────────────────────── Dynamic __all__ exports ────────────────────────────
__all__: List[str] = []

def _rebuild_exports() -> None:
    __all__.clear()
    seen = set()
    mappers = getattr(Base.registry, "mappers", [])
    # In some SQLAlchemy versions, .mappers may be a set; sort by class name
    for mapper in sorted(mappers, key=lambda m: m.class_.__name__):
        cls = mapper.class_
        name = cls.__name__
        globals()[name] = cls
        if name not in seen:
            __all__.append(name)
            seen.add(name)

# ───────────────────────────── Public loader ──────────────────────────────────
_LOADED_ONCE = False
_LOADED: List[str] = []
_SKIPPED: List[str] = []

def load_models() -> tuple[List[str], List[str]]:
    """
    Import all model modules and configure mappers. Safe to call multiple times.
    Returns (loaded_modules, skipped_modules_with_errors).
    """
    global _LOADED_ONCE, _LOADED, _SKIPPED
    if _LOADED_ONCE:
        return _LOADED, _SKIPPED

    # Log engine URL if available (masked elsewhere)
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
                tb = traceback.format_exc(limit=6)
                _SKIPPED.append(f"{m} ← {e.__class__.__name__}: {e}")
                if _STRICT or _DEV:
                    raise
                log.warning(
                    "Skipped model '%s' due to %s: %s\n%s",
                    m, e.__class__.__name__, e, tb
                )

        # Before configuring, detect duplicates proactively
        _fail_on_duplicates()

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
