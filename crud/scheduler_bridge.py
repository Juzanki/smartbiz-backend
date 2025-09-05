# backend/crud/scheduler_bridge.py
# -*- coding: utf-8 -*-
"""
Scheduler Bridge:
- Dynamically resolves CRUD functions from candidate modules (scheduler_crud,
  schedule_crud, messages_crud, message_log_crud).
- If a function is missing, uses safe no-op fallbacks so the scheduler won't crash.
"""
from __future__ import annotations
import logging
from importlib import import_module
from typing import Any, Callable, Dict, List, Optional
from datetime import datetime

log = logging.getLogger("smartbiz.scheduler.bridge")

# Candidate modules to search for the CRUD functions
_CANDIDATE_MODULES = [
    "backend.crud.scheduler_crud",
    "backend.crud.schedule_crud",
    "backend.crud.messages_crud",
    "backend.crud.message_log_crud",
]


def _resolve(func_name: str) -> Optional[Callable[..., Any]]:
    """Try to resolve a callable by name from the candidate modules."""
    for mod_name in _CANDIDATE_MODULES:
        try:
            mod = import_module(mod_name)
            fn = getattr(mod, func_name, None)
            if callable(fn):
                log.debug("Resolved %s from %s", func_name, mod_name)
                return fn
        except Exception:
            # Ignore import errors and continue searching
            continue
    return None


# ---------- Public API consumed by the scheduler ----------

def get_due_unsent_messages(db, now: datetime, limit: int = 100) -> List[Dict[str, Any]]:
    """
    Return a list of messages that are due to be sent.
    Each item should look like:
      {
        "id": int|str,
        "channel": "push|sms|email|in_app",
        "payload": dict,
        "scheduled_at": datetime,
        ...
      }
    If no implementation exists, return an empty list.
    """
    fn = _resolve("get_due_unsent_messages")
    if fn:
        return fn(db, now, limit=limit)
    log.debug("[Bridge] get_due_unsent_messages: no implementation found -> []")
    return []


def mark_message_sent(db, message_id: Any, sent_at: Optional[datetime] = None) -> None:
    """Mark the given message as sent (if an implementation exists)."""
    fn = (
        _resolve("mark_message_sent")
        or _resolve("set_message_sent")
        or _resolve("message_mark_sent")
    )
    if fn:
        return fn(db, message_id, sent_at)
    log.debug("[Bridge] mark_message_sent: no implementation; skipping")


def mark_message_failed(db, message_id: Any, error: str, attempt: Optional[int] = None) -> None:
    """Mark the given message as failed (if an implementation exists)."""
    fn = (
        _resolve("mark_message_failed")
        or _resolve("set_message_failed")
        or _resolve("message_mark_failed")
    )
    if fn:
        return fn(db, message_id, error, attempt)
    log.warning("[Bridge] mark_message_failed (no impl): id=%s err=%s", message_id, error[:200])


def log_message_event(db, message_id: Any, level: str, message: str) -> None:
    """Append a log entry for the message (if an implementation exists)."""
    fn = (
        _resolve("log_message_event")
        or _resolve("append_log_entry")
        or _resolve("create_message_log")
    )
    if fn:
        return fn(db, message_id, level, message)
    log.debug("[Bridge] log_message_event (no impl): %s - %s", level, message[:120])
