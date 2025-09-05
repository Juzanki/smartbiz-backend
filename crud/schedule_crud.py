from __future__ import annotations
﻿from backend.schemas.user import UserOut
# backend/crud/schedule_crud.py
# -*- coding: utf-8 -*-
from typing import Optional, Dict, Any
from datetime import datetime, timezone
from sqlalchemy import update, inspect, MetaData, Table
from sqlalchemy.orm import Session

# ---- Core helper: resolve table dynamically (hakuna import ya ORM model) ----
CANDIDATE_TABLES = ("scheduled_messages", "scheduled_posts", "schedule", "scheduled_queue")

def _get_bind(db: Session):
    return getattr(db, "bind", None) or db.get_bind()

def _resolve_table(db: Session) -> Table:
    bind = _get_bind(db)
    insp = inspect(bind)
    for name in CANDIDATE_TABLES:
        if insp.has_table(name):
            md = MetaData()
            return Table(name, md, autoload_with=bind)
    raise RuntimeError(f"No scheduled table found. Tried: {', '.join(CANDIDATE_TABLES)}")

def _filter_values(table: Table, values: Dict[str, Any]) -> Dict[str, Any]:
    cols = {c.name for c in table.columns}
    return {k: v for k, v in values.items() if k in cols}

def mark_as_sent(db: Session, item_id: int, provider_message_id: Optional[str] = None, extra: Optional[Dict[str, Any]] = None) -> bool:
    table = _resolve_table(db)
    now = datetime.now(timezone.utc)
    values: Dict[str, Any] = {
        "status": "sent",
        "is_sent": True,
        "sent_at": now,
        "updated_at": now,
    }
    if provider_message_id:
        values["provider_message_id"] = provider_message_id
    stmt = update(table).where(table.c.id == item_id).values(**_filter_values(table, values))
    res = db.execute(stmt)
    db.commit()
    return (getattr(res, "rowcount", 0) or 0) > 0

def mark_as_failed(db: Session, item_id: int, error_message: Optional[str] = None, extra: Optional[Dict[str, Any]] = None) -> bool:
    table = _resolve_table(db)
    now = datetime.now(timezone.utc)
    values: Dict[str, Any] = {
        "status": "failed",
        "is_sent": False,
        "failed_at": now,
        "updated_at": now,
    }
    if error_message:
        values["last_error"] = error_message
    stmt = update(table).where(table.c.id == item_id).values(**_filter_values(table, values))
    res = db.execute(stmt)
    db.commit()
    return (getattr(res, "rowcount", 0) or 0) > 0

