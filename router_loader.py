# backend/router_loader.py
from __future__ import annotations
import os
import importlib
import logging
from typing import Iterable, List, Tuple
from fastapi import FastAPI

log = logging.getLogger(__name__)

# where the router object may live inside a module
CANDIDATE_ATTRS = ("router", "api", "app")

# try these module name patterns in order
def _module_candidates(name: str) -> List[str]:
    # name can be "profile" or "backend.routes.profile" etc.
    if "." in name:
        return [name]  # already fully-qualified
    return [
        f"backend.routes.{name}",
        f"backend.{name}",
        name,  # top-level shim, if you created any
    ]

def _import_router(modname: str):
    """Try to import a module and return (router_obj, attr_name) if found."""
    try:
        m = importlib.import_module(modname)
    except ModuleNotFoundError:
        return None, None
    except Exception as e:
        log.warning("Router import error in %s: %s", modname, e)
        return None, None

    for attr in CANDIDATE_ATTRS:
        r = getattr(m, attr, None)
        if r is not None:
            return r, attr
    return None, None

def include_named_routers(
    app: FastAPI,
    names: Iterable[str],
    *,
    required: Iterable[str] = (),
    prefix_map: dict[str, str] | None = None,
) -> List[Tuple[str, str]]:
    """
    Try to include routers by short names (e.g., 'profile', 'pay_mpesa').
    - Searches several module patterns per name (backend.routes.NAME, backend.NAME, NAME).
    - Looks for attributes: router/api/app.
    - required: names that must import or we raise ImportError.
    - prefix_map: optional {name: "/prefix"} to mount under a path prefix.
    Returns a list of (module_name, attr_name) that were included.
    """
    prefix_map = prefix_map or {}
    included: List[Tuple[str, str]] = []

    for name in names:
        found = False
        for mod in _module_candidates(name):
            obj, attr = _import_router(mod)
            if obj is None:
                continue
            app.include_router(obj, prefix=prefix_map.get(name, ""))
            log.info("Included router: %s (attr=%s)", mod, attr)
            included.append((mod, attr))
            found = True
            break

        if not found:
            msg = f"Router '{name}' not found in any of: {', '.join(_module_candidates(name))}"
            if name in set(required):
                # hard failure for required routers
                raise ImportError(msg)
            else:
                # optional: keep log level low to avoid noisy console
                log.debug("Skipping optional router '%s' (%s)", name, msg)

    return included

def include_routers_from_env(app: FastAPI) -> None:
    """
    Control routers via env vars:
    - INCLUDE_ROUTERS: CSV of names to try (defaults provided below).
    - REQUIRE_ROUTERS: CSV subset that must exist (raise if missing).
    - ROUTER_PREFIXES: CSV of NAME=/prefix pairs (e.g., 'profile=/profile,auth_routes=/auth')
    """
    default = [
        "register", "logout", "auth_routes", "profile", "forgot_password",
        "pay_mpesa", "admin_routes", "ai_responder", "subscription",
        "broadcast", "negotiation_bot", "telegram_bot",
    ]
    names = os.getenv("INCLUDE_ROUTERS", ",".join(default)).split(",")
    names = [n.strip() for n in names if n.strip()]

    required = os.getenv("REQUIRE_ROUTERS", "").split(",")
    required = [n.strip() for n in required if n.strip()]

    prefix_pairs = os.getenv("ROUTER_PREFIXES", "")
    prefix_map: dict[str, str] = {}
    for pair in prefix_pairs.split(","):
        if "=" in pair:
            k, v = pair.split("=", 1)
            prefix_map[k.strip()] = v.strip()

    include_named_routers(app, names, required=required, prefix_map=prefix_map)
