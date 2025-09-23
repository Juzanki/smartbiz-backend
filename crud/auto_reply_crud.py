# backend/crud/auto_reply.py
# -*- coding: utf-8 -*-
from __future__ import annotations

from typing import Optional, List
from sqlalchemy import select, delete, and_
from sqlalchemy.orm import Session

# Rekebisha path hii kama model yako iko tofauti, mfano: backend.models.auto_reply
from backend.models.auto_reply import AutoReply  # noqa: F401


# ------------------------- Helpers -------------------------
def _norm(v: Optional[str]) -> str:
    """Trim + lowercase; hurudisha '' kama tupu."""
    return (v or "").strip().lower()


# ------------------------- CRUD ----------------------------
def create_auto_reply(
    db: Session,
    *,
    platform: str,
    keyword: str,
    reply: str,
) -> AutoReply:
    """
    Unda au sasisha (upsert) AutoReply kwa (platform, keyword).
    - Inanormalize platform/keyword kuwa lowercase.
    """
    platform_n = _norm(platform)
    keyword_n = _norm(keyword)
    if not platform_n:
        raise ValueError("platform is required")
    if not keyword_n:
        raise ValueError("keyword is required")
    if not reply or not reply.strip():
        raise ValueError("reply is required")

    existing = db.execute(
        select(AutoReply).where(
            and_(AutoReply.platform == platform_n, AutoReply.keyword == keyword_n)
        )
    ).scalar_one_or_none()

    try:
        if existing:
            existing.reply = reply
            db.add(existing)
            db.commit()
            db.refresh(existing)
            return existing

        obj = AutoReply(platform=platform_n, keyword=keyword_n, reply=reply)
        db.add(obj)
        db.commit()
        db.refresh(obj)
        return obj
    except Exception:
        db.rollback()
        raise


def get_auto_replies(
    db: Session,
    *,
    platform: Optional[str] = None,
    q: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
) -> List[AutoReply]:
    """
    Orodhesha auto-replies (ina filters + pagination).
    - platform: chuja kwa platform (exact, normalized)
    - q:       tafuta kwa ILIKE %q% kwenye keyword
    """
    limit = max(1, min(int(limit or 100), 500))
    offset = max(0, int(offset or 0))

    stmt = select(AutoReply)
    if platform:
        stmt = stmt.where(AutoReply.platform == _norm(platform))
    if q:
        stmt = stmt.where(AutoReply.keyword.ilike(f"%{q.strip()}%"))

    stmt = stmt.order_by(AutoReply.id.desc()).limit(limit).offset(offset)
    return list(db.execute(stmt).scalars().all())


def get_reply_for_keyword(
    db: Session,
    *,
    platform: str,
    keyword: str,
) -> Optional[AutoReply]:
    """
    Pata rekodi moja ya (platform, keyword) baada ya normalization.
    """
    platform_n = _norm(platform)
    keyword_n = _norm(keyword)
    stmt = select(AutoReply).where(
        and_(AutoReply.platform == platform_n, AutoReply.keyword == keyword_n)
    )
    return db.execute(stmt).scalar_one_or_none()


def update_auto_reply(
    db: Session,
    *,
    reply_id: int,
    platform: Optional[str] = None,
    keyword: Optional[str] = None,
    reply: Optional[str] = None,
) -> Optional[AutoReply]:
    """
    Sasisha sehemu za AutoReply kwa id. Hurudisha None kama haipo.
    """
    obj = db.get(AutoReply, int(reply_id))
    if not obj:
        return None

    if platform is not None:
        obj.platform = _norm(platform)
    if keyword is not None:
        obj.keyword = _norm(keyword)
    if reply is not None:
        if not reply.strip():
            raise ValueError("reply cannot be empty")
        obj.reply = reply

    try:
        db.add(obj)
        db.commit()
        db.refresh(obj)
        return obj
    except Exception:
        db.rollback()
        raise


def delete_auto_reply(db: Session, *, reply_id: int) -> bool:
    """
    Futa kwa id. Hurejesha True kama imefutwa, vinginevyo False.
    """
    try:
        res = db.execute(delete(AutoReply).where(AutoReply.id == int(reply_id)))
        db.commit()
        # rowcount inaweza kuwa None kwenye baadhi ya engines; tumia default 0
        return (getattr(res, "rowcount", 0) or 0) > 0
    except Exception:
        db.rollback()
        raise
