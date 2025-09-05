# -*- coding: utf-8 -*-
from __future__ import annotations
"""
Unified CRUD exports (safe & strict):
- Re-exports user CRUD, post CRUD, and scheduler helpers
- Tries multiple legacy/new module locations before giving up
- Never raises ImportError at import time; raises NotImplementedError only when a missing function is CALLED
"""
from importlib import import_module
from contextlib import suppress
from typing import List, Any, Dict

# ---------------- helpers ----------------
def _safe_from(mod: str, names: List[str]) -> None:
    """
    Import selected names from .<mod> if module and attributes exist.
    Silently skips anything missing.
    """
    with suppress(Exception):
        m = import_module(f"{__name__}.{mod}")
        for n in names:
            if hasattr(m, n):
                globals()[n] = getattr(m, n)

def _export_first(func_names: List[str], modules_in_order: List[str]) -> None:
    """
    For each function name, bind the first implementation found in modules_in_order.
    If none has it, install a stub that raises NotImplementedError when called.
    """
    for fname in func_names:
        bound = False
        for mod in modules_in_order:
            with suppress(Exception):
                m = import_module(f"{__name__}.{mod}")
                if hasattr(m, fname):
                    globals()[fname] = getattr(m, fname)
                    bound = True
                    break
        if not bound:
            def _stub(*args: Any, __fname: str = fname, __mods: List[str] = modules_in_order, **kwargs: Any) -> Any:
                raise NotImplementedError(
                    f"CRUD function '{__fname}' not available. "
                    f"Expected in one of: {', '.join(__mods)}"
                )
            _stub.__name__ = fname
            globals()[fname] = _stub

# ---------------- users ----------------
_safe_from("user_crud", [
    "get_user", "get_users", "create_user",
    "get_user_by_email", "get_user_by_username", "get_user_by_phone",
    "update_user_profile", "get_user_by_identifier",
])

# Provide a robust default for get_user_by_identifier if not exported above
if "get_user_by_identifier" not in globals():
    import re
    from contextlib import suppress as _suppress_local

    _EMAIL_RE = re.compile(r"^[^@]+@[^@]+\.[^@]+$")

    def get_user_by_identifier(db, identifier: str):
        """
        Lookup user by email / phone / username / id.
        Works even if your DB columns differ slightly (phone/msisdn/mobile/phone_number).
        """
        from sqlalchemy import or_
        from sqlalchemy.orm import Session
from backend.models.user import User

        ident = (identifier or "").strip()
        if not ident:
            return None

        q = db.query(User)

        # 1) Email
        if _EMAIL_RE.fullmatch(ident) and hasattr(User, "email"):
            with _suppress_local(Exception):
                return q.filter(getattr(User, "email").ilike(ident)).first()

        # 2) Phone-like
        digits = "".join(ch for ch in ident if ch.isdigit())
        if len(digits) >= 7:
            filters = []
            for col_name in ("phone", "phone_number", "msisdn", "mobile", "contact_phone"):
                if hasattr(User, col_name):
                    col = getattr(User, col_name)
                    filters.append(col.like(f"%{digits[-9:]}%"))
            if filters:
                user = q.filter(or_(*filters)).first()
                if user:
                    return user

        # 3) Username/handle/name
        for col_name in ("username", "handle", "name"):
            if hasattr(User, col_name):
                col = getattr(User, col_name)
                with _suppress_local(Exception):
                    user = q.filter(col.ilike(ident)).first()
                    if user:
                        return user

        # 4) Fallback by numeric id
        with _suppress_local(Exception):
            uid = int(ident)
            if hasattr(User, "id"):
                return q.filter(getattr(User, "id") == uid).first()

        return None

# ---------------- posts ----------------
_safe_from("post_crud", ["create_post"])

# ---------------- scheduled messaging (used by backend.tasks.scheduler) ----------------
_SCHEDULER_MODULES: List[str] = [
    "scheduler_crud",   # preferred
    "schedule_crud",    # alternative
    "messages_crud",    # legacy?
    "message_log_crud", # legacy?
]

_export_first(
    ["get_due_unsent_messages", "mark_as_sent", "mark_as_failed"],
    modules_in_order=_SCHEDULER_MODULES,
)

# ---------------- public API ----------------
__all__ = sorted(
    name for name, obj in globals().items()
    if not name.startswith("_") and callable(obj)
)

