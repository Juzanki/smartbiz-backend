# backend/crud/message_queue_crud.py
# -*- coding: utf-8 -*-
from __future__ import annotations
"""
CRUD helpers for scheduled/queued messages.

Design goals (mobile-first/backoffice-safe):
- Idempotent operations (safe to retry).
- Graceful fallbacks (do not crash if optional columns/logs are absent).
- Double-send protection via short row lock (SELECT … FOR UPDATE NOWAIT when possible).
- Auto-discovery of the correct SQLAlchemy ORM model for scheduled messages.
"""
import os
import pkgutil
import importlib
import inspect
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Sequence, Type, List

from sqlalchemy.orm import Session
from sqlalchemy.exc import OperationalError

__all__ = [
    "STATUS_QUEUED",
    "STATUS_PROCESSING",
    "STATUS_SENT",
    "STATUS_FAILED",
    "MessageModel",
    "mark_as_processing",
    "mark_as_sent",
    "mark_as_failed",
    "mark_as_queued",
    "utcnow",
]

# ---------------------------------------------------------------------------
# Model resolver
# ---------------------------------------------------------------------------

def _import_from_path(dotted: str) -> Optional[Type[Any]]:
    """Import 'package.module:ClassName' or 'package.module.ClassName' and return the class."""
    if not dotted:
        return None
    dotted = dotted.replace(":", ".")
    parts = dotted.split(".")
    if len(parts) < 2:
        return None
    mod_path = ".".join(parts[:-1])
    cls_name = parts[-1]
    mod = importlib.import_module(mod_path)
    return getattr(mod, cls_name, None)


def _score_candidate(cls: Type[Any]) -> int:
    """
    Score a SQLAlchemy model as a 'scheduled message' candidate.
    Higher score = more likely to be the correct model.
    """
    score = 0
    name = cls.__name__.lower()
    table = getattr(getattr(cls, "__table__", None), "name", "") or ""
    fields = set(dir(cls))

    # Names / table hints
    if "message" in name or "message" in table:
        score += 4
    if any(k in name for k in ("schedule", "scheduled")) or "schedule" in table:
        score += 3
    if "post" in name or "post" in table:
        score += 2

    # Typical columns
    if "id" in fields:
        score += 3
    if "status" in fields or "state" in fields:
        score += 2
    if any(f in fields for f in ("sent_at", "delivered_at")):
        score += 1
    if any(f in fields for f in ("scheduled_at", "schedule_time", "send_at", "dispatch_at")):
        score += 1
    if any(f in fields for f in ("content", "text", "body")):
        score += 1
    return score


def _autodiscover_model() -> Optional[Type[Any]]:
    """Search backend.models.* for a mapped class that looks like a scheduled message."""
    try:
        models_pkg = importlib.import_module("backend.models")
    except Exception:
        try:
            models_pkg = importlib.import_module(".models", package="backend")
        except Exception:
            return None

    best: tuple[Optional[Type[Any]], int] = (None, -1)
    for _, name, ispkg in pkgutil.iter_modules(models_pkg.__path__):
        if ispkg:
            continue
        try:
            mod = importlib.import_module(f"{models_pkg.__name__}.{name}")
        except Exception:
            continue
        for _, obj in inspect.getmembers(mod, inspect.isclass):
            # Heuristic: mapped classes usually expose __table__ and an 'id' attribute
            if hasattr(obj, "__table__") and hasattr(obj, "id"):
                s = _score_candidate(obj)
                if s > best[1]:
                    best = (obj, s)
    return best[0]


def _resolve_message_model() -> Type[Any]:
    """
    Resolve the ORM class for scheduled messages using:
      1) Env var SCHEDULED_MESSAGE_MODEL (e.g. "backend.models.my.Message")
      2) Common defaults
      3) Auto-discovery in backend.models
    """
    # 1) Configurable path
    cfg = os.getenv("SCHEDULED_MESSAGE_MODEL", "").strip()
    if cfg:
        cls = _import_from_path(cfg)
        if cls is not None:
            return cls  # type: ignore[return-value]

    # 2) Common defaults
    defaults = [
        "backend.models.message.Message",
        "backend.models.scheduled_post.ScheduledPost",
        "backend.models.scheduled_message.ScheduledMessage",
        # Relative fallbacks when running inside the 'backend' package
        ".models.message.Message",
        ".models.scheduled_post.ScheduledPost",
        ".models.scheduled_message.ScheduledMessage",
    ]
    for dotted in defaults:
        try:
            cls = _import_from_path(dotted)
            if cls is not None:
                return cls  # type: ignore[return-value]
        except Exception:
            pass

    # 3) Auto-discovery
    cls = _autodiscover_model()
    if cls is not None:
        return cls  # type: ignore[return-value]

    raise ImportError(
        "Could not resolve the scheduled message model. "
        "Set SCHEDULED_MESSAGE_MODEL='backend.models.<module>.<Class>' "
        "or ensure a model exists with typical fields (id/status/sent_at) under backend.models.*"
    )


# The model used throughout this module
MessageModel: Type[Any] = _resolve_message_model()

# Optional log model (best-effort)
try:
    from backend.models.message_log import MessageLog  # type: ignore[attr-defined]
except Exception:
    try:
        from .message_log import MessageLog  # type: ignore
    except Exception:
        MessageLog = None  # type: ignore


# ---------------------------------------------------------------------------
# Status constants and small utils
# ---------------------------------------------------------------------------
STATUS_QUEUED = "queued"
STATUS_PROCESSING = "processing"
STATUS_SENT = "sent"
STATUS_FAILED = "failed"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def set_if_has(obj: Any, field: str, value: Any) -> None:
    """Set attribute only if it exists on the object (soft updates)."""
    if hasattr(obj, field):
        setattr(obj, field, value)


def _record_log(
    db: Session,
    *,
    message_id: int | str,
    action: str,
    note: Optional[str] = None,
    provider: Optional[str] = None,
    external_id: Optional[str] = None,
    level: str = "info",
    metadata: Optional[Dict[str, Any]] = None,
    error: Optional[str] = None,
) -> None:
    """Best-effort insert into MessageLog if the model exists."""
    if not MessageLog:
        return
    try:
        payload: Dict[str, Any] = dict(
            message_id=message_id,
            action=action,
            note=note,
            provider=provider,
            external_id=external_id,
            level=level,
            created_at=utcnow(),
        )
        if hasattr(MessageLog, "metadata") and metadata is not None:
            payload["metadata"] = metadata
        if hasattr(MessageLog, "error") and error is not None:
            payload["error"] = error
        db.add(MessageLog(**payload))  # type: ignore[arg-type]
    except Exception:
        # Never let logging break the flow
        pass


def _get_for_update(db: Session, message_id: int | str):
    """
    Try to acquire a short row lock to prevent double-sends.
    Falls back to a normal SELECT if NOWAIT is unsupported.
    """
    q = db.query(MessageModel).filter(MessageModel.id == message_id)
    try:
        return q.with_for_update(nowait=True).one_or_none()
    except Exception:
        return q.one_or_none()


# ---------------------------------------------------------------------------
# Public CRUD helpers (idempotent & mobile friendly)
# ---------------------------------------------------------------------------

def mark_as_processing(
    db: Session,
    message_id: int | str,
    *,
    idempotency_key: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
    **kwargs: Any,
):
    msg = _get_for_update(db, message_id)
    if not msg:
        return None
    current = getattr(msg, "status", None)
    if current in (STATUS_PROCESSING, STATUS_SENT, STATUS_FAILED):
        return msg
    set_if_has(msg, "status", STATUS_PROCESSING)
    set_if_has(msg, "processing_at", utcnow())
    if idempotency_key:
        set_if_has(msg, "idempotency_key", idempotency_key)
    db.add(msg)
    _record_log(
        db,
        message_id=message_id,
        action="processing",
        note="Picked for processing.",
        metadata=metadata,
    )
    db.commit()
    db.refresh(msg)
    return msg


def mark_as_sent(
    db: Session,
    message_id: int | str,
    *,
    provider: Optional[str] = None,
    external_id: Optional[str] = None,
    note: Optional[str] = None,
    idempotency_key: Optional[str] = None,
    delivered_at: Optional[datetime] = None,
    metadata: Optional[Dict[str, Any]] = None,
    force: bool = False,
    **kwargs: Any,
):
    try:
        msg = _get_for_update(db, message_id)
    except OperationalError:
        return None
    if not msg:
        return None

    current = getattr(msg, "status", None)
    if current == STATUS_SENT and not force:
        return msg
    if current == STATUS_FAILED and not force:
        return msg

    set_if_has(msg, "status", STATUS_SENT)
    set_if_has(msg, "sent_at", utcnow())
    if delivered_at:
        set_if_has(msg, "delivered_at", delivered_at)
    if idempotency_key:
        set_if_has(msg, "idempotency_key", idempotency_key)
    if provider:
        set_if_has(msg, "provider", provider)
    if external_id:
        for f in ("external_id", "provider_message_id", "remote_id"):
            if hasattr(msg, f):
                setattr(msg, f, external_id)
                break

    db.add(msg)
    _record_log(
        db,
        message_id=message_id,
        action="sent",
        provider=provider,
        external_id=external_id,
        note=note,
        metadata=metadata,
    )
    db.commit()
    db.refresh(msg)
    return msg


def mark_as_failed(
    db: Session,
    message_id: int | str,
    *,
    reason: str,
    provider: Optional[str] = None,
    external_id: Optional[str] = None,
    idempotency_key: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
    **kwargs: Any,
):
    try:
        msg = _get_for_update(db, message_id)
    except OperationalError:
        return None
    if not msg:
        return None
    if getattr(msg, "status", None) == STATUS_SENT:
        return msg

    set_if_has(msg, "status", STATUS_FAILED)
    set_if_has(msg, "failed_at", utcnow())
    set_if_has(msg, "failure_reason", reason)
    if idempotency_key:
        set_if_has(msg, "idempotency_key", idempotency_key)
    if provider:
        set_if_has(msg, "provider", provider)
    if external_id:
        for f in ("external_id", "provider_message_id", "remote_id"):
            if hasattr(msg, f):
                setattr(msg, f, external_id)
                break

    db.add(msg)
    _record_log(
        db,
        message_id=message_id,
        action="failed",
        provider=provider,
        external_id=external_id,
        note="Message failed.",
        metadata=metadata,
        error=reason,
        level="error",
    )
    db.commit()
    db.refresh(msg)
    return msg


def mark_as_queued(
    db: Session,
    message_id: int | str,
    *,
    metadata: Optional[Dict[str, Any]] = None,
    **kwargs: Any,
):
    msg = db.query(MessageModel).filter(MessageModel.id == message_id).one_or_none()
    if not msg:
        return None
    set_if_has(msg, "status", STATUS_QUEUED)
    set_if_has(msg, "queued_at", utcnow())
    db.add(msg)
    _record_log(
        db,
        message_id=message_id,
        action="queued",
        note="Message put back to queue.",
        metadata=metadata,
    )
    db.commit()
    db.refresh(msg)
    return msg
