from __future__ import annotations
﻿from backend.schemas.user import UserOut
# backend/crud/message_log_crud.py
# -*- coding: utf-8 -*-
"""
Message logging helpers

Mobile-first principles:
- Idempotent (optional idempotency_key) for safe retries from unstable networks.
- Graceful fallbacks: only set columns that exist on your model.
- Content hygiene: trims & bounds content length; removes control chars.
- Safe logging: never crash your request path; optional raise_on_error.
- Flexible metadata: tags/extra/device/ip/user_agent stored when columns exist.
"""
import logging
import unicodedata
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, Optional, Sequence

from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

# Adjust this import to match your project layout
from backend.models.message_log import MessageLog

logger = logging.getLogger(__name__)

# ---- utilities ---------------------------------------------------------------

ALLOWED_SOURCES = {
    "telegram", "whatsapp", "web", "mobile", "api", "system", "admin", "bot",
}


def now_utc() -> datetime:
    """Timezone-aware UTC timestamp."""
    return datetime.now(timezone.utc)


def normalize_source(source: Optional[str]) -> str:
    if not source:
        return "unknown"
    s = source.strip().lower()
    return s if s in ALLOWED_SOURCES else "unknown"


def collapse_ws(text: str) -> str:
    """Collapse whitespace runs to a single space, keep newlines."""
    # Replace runs of spaces/tabs with a single space, preserve newlines
    out = []
    prev_space = False
    for ch in text:
        if ch in (" ", "\t"):
            if not prev_space:
                out.append(" ")
                prev_space = True
        else:
            out.append(ch)
            prev_space = False
    return "".join(out)


def strip_control_chars(text: str) -> str:
    """
    Remove control/format characters (Cc, Cf) but keep emojis and normal text.
    """
    return "".join(
        ch for ch in text
        if unicodedata.category(ch) not in {"Cc", "Cf"}
    )


def clean_content(content: str, max_len: int) -> str:
    """
    Sanitize and bound message content for safe storage and mobile constraints.
    """
    if content is None:
        return ""
    text = content.strip()
    text = strip_control_chars(text)
    text = collapse_ws(text)
    if max_len > 0 and len(text) > max_len:
        # Keep head & tail to preserve context
        head = max_len - 20 if max_len > 40 else max_len
        tail = 0 if head == max_len else 20
        text = f"{text[:head]}â€¦{text[-tail:]}" if tail else text[:head]
    return text


def has_attr(obj: Any, field: str) -> bool:
    return hasattr(obj, field)


def set_if_has(obj: Any, field: str, value: Any) -> None:
    if hasattr(obj, field):
        setattr(obj, field, value)


def _find_existing_by_idempotency(db: Session, key: str) -> Optional[MessageLog]:
    """
    If your model has an `idempotency_key` column and a unique constraint,
    this prevents duplicates on retries.
    """
    if not has_attr(MessageLog, "idempotency_key"):
        return None
    try:
        return db.query(MessageLog).filter(
            MessageLog.idempotency_key == key  # type: ignore[attr-defined]
        ).one_or_none()
    except Exception:
        return None


# ---- public API --------------------------------------------------------------

def log_message(
    db: Session,
    sender_id: str,
    sender_name: str,
    content: str,
    *,
    source: str = "telegram",
    message_id: Optional[int] = None,
    room_id: Optional[str] = None,
    user_id: Optional[int] = None,
    idempotency_key: Optional[str] = None,
    tags: Optional[Sequence[str]] = None,
    extra: Optional[Dict[str, Any]] = None,
    device: Optional[str] = None,
    ip: Optional[str] = None,
    user_agent: Optional[str] = None,
    timestamp: Optional[datetime] = None,
    max_content_length: int = 4000,
    autocommit: bool = True,
    raise_on_error: bool = False,
) -> Optional[MessageLog]:
    """
    Create a log row for an inbound/outbound message.

    Returns the persisted MessageLog (or existing row if idempotent hit),
    or None on failure when raise_on_error=False.
    """

    try:
        # Idempotency: short-circuit if we already wrote this log
        if idempotency_key:
            existing = _find_existing_by_idempotency(db, idempotency_key)
            if existing:
                return existing

        # Normalize/sanitize input
        norm_source = normalize_source(source)
        safe_content = clean_content(content, max_content_length)
        ts = timestamp or now_utc()

        # Build the row (set only columns that exist on your model)
        row = MessageLog()  # type: ignore[call-arg]

        # required-ish fields
        set_if_has(row, "sender_id", sender_id)
        set_if_has(row, "sender_name", sender_name)
        set_if_has(row, "content", safe_content)
        set_if_has(row, "source", norm_source)
        # time fields may vary by schema: prefer timezone-aware
        for field in ("timestamp", "created_at", "logged_at"):
            if has_attr(row, field):
                setattr(row, field, ts)
                break

        # optional relational/context fields
        if message_id is not None:
            set_if_has(row, "message_id", message_id)
        if room_id is not None:
            set_if_has(row, "room_id", room_id)
        if user_id is not None:
            set_if_has(row, "user_id", user_id)

        # network / device metadata
        if device is not None:
            set_if_has(row, "device", device)
        if ip is not None:
            set_if_has(row, "ip", ip)
        if user_agent is not None:
            set_if_has(row, "user_agent", user_agent)

        # idempotency + flexible metadata
        if idempotency_key is not None:
            set_if_has(row, "idempotency_key", idempotency_key)
        if tags and has_attr(MessageLog, "tags"):
            # store as list/JSON if the column supports it
            set_if_has(row, "tags", list(tags))
        if extra and has_attr(MessageLog, "extra"):
            set_if_has(row, "extra", extra)

        db.add(row)

        if autocommit:
            try:
                db.commit()
            except IntegrityError:
                # Race on unique(idempotency_key). Fetch the row and return.
                db.rollback()
                if idempotency_key:
                    existing = _find_existing_by_idempotency(db, idempotency_key)
                    if existing:
                        return existing
                # If we cannot recover gracefully, re-raise below.
                raise
            # refresh only after a successful commit
            try:
                db.refresh(row)
            except OperationalError:
                # Some backends/models may not need refresh; ignore
                pass
        else:
            # No commit: just flush so IDs materialize if possible.
            db.flush()
            try:
                db.refresh(row)
            except Exception:
                pass

        return row

    except Exception as exc:
        # Never take the app down because of logging unless asked to
        logger.warning("log_message failed: %s", exc, exc_info=not raise_on_error)
        if autocommit:
            try:
                db.rollback()
            except Exception:
                pass
        if raise_on_error:
            raise
        return None


