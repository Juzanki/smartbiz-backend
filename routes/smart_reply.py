# backend/routes/smart_replies.py
# -*- coding: utf-8 -*-
"""
Smart Replies (mobile-first):
- POST /smart-replies?mode=replace|append  -> weka/ongeza majibu kwa room
- GET  /smart-replies/{room_id}?q=&limit= -> pata majibu (na utafutaji)
- DELETE /smart-replies/{room_id}         -> ondoa jibu moja kwa id/message

Vitu muhimu:
- Normalization: huondoa tupu, hukata urefu, huondoa marudio (dedupe)
- Limits salama: MAX_REPLIES, MAX_CHARS
- Concurrency: .with_for_update() wakati wa append ili kuepuka race
- Transaction moja: atomic replace/append
"""
from __future__ import annotations

from typing import List, Optional, Literal
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy import func
import re

from backend.db import get_db
from backend.models.smart_reply import SmartReply
from backend.schemas.smart_reply_schema import SmartReplyCreate, SmartReplyOut

router = APIRouter(prefix="/smart-replies", tags=["Smart Replies"])

# —— Mobile-first guards ——
MAX_REPLIES = 8         # on-screen chips (3–8 bora kwa mobile)
MAX_CHARS   = 64        # chip text isiwe ndefu sana

def _normalize_replies(items: List[str]) -> List[str]:
    """Safisha, kata urefu, na dedupe kwa mpangilio uliopewa."""
    seen = set()
    cleaned: List[str] = []
    for raw in items or []:
        # collapse spaces + trim
        msg = re.sub(r"\s+", " ", (raw or "")).strip()
        if not msg:
            continue
        if len(msg) > MAX_CHARS:
            msg = msg[:MAX_CHARS].rstrip()
        key = msg.casefold()
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(msg)
    return cleaned[:MAX_REPLIES]


@router.post(
    "/",
    response_model=List[SmartReplyOut],
    status_code=status.HTTP_201_CREATED,
    summary="Set/append smart replies for a room",
)
def set_smart_replies(
    data: SmartReplyCreate,
    db: Session = Depends(get_db),
    mode: Literal["replace", "append"] = Query("replace", description="replace or append"),
):
    """
    mode=replace: futa zote za room, weka mpya (atomic).
    mode=append: ongeza mpya bila kuvuka MAX_REPLIES (hufunga rows za room).
    """
    try:
        new_msgs = _normalize_replies(data.replies)
        if not new_msgs:
            raise HTTPException(status_code=400, detail="No valid replies after normalization")

        if mode == "replace":
            # Transaction moja: futa kisha weka
            with db.begin():
                db.query(SmartReply).filter(SmartReply.room_id == data.room_id)\
                  .delete(synchronize_session=False)
                db.add_all([SmartReply(room_id=data.room_id, message=m) for m in new_msgs])

        else:  # append
            with db.begin():
                # funga rows za room ili kuepuka race kwenye counting/insert
                existing = (db.query(SmartReply)
                              .filter(SmartReply.room_id == data.room_id)
                              .with_for_update()
                              .all())
                existing_texts = {re.sub(r"\s+", " ", (r.message or "")).strip().casefold()
                                  for r in existing}
                remaining = MAX_REPLIES - len(existing)
                if remaining <= 0:
                    raise HTTPException(status_code=409, detail=f"Room already has {MAX_REPLIES} replies")
                to_insert = [m for m in new_msgs if m.casefold() not in existing_texts][:remaining]
                if not to_insert:
                    # Nothing new to add
                    pass
                else:
                    db.add_all([SmartReply(room_id=data.room_id, message=m) for m in to_insert])

        # Rudisha zilizopo sasa (mpangilio kulingana na id)
        results = (db.query(SmartReply)
                     .filter(SmartReply.room_id == data.room_id)
                     .order_by(SmartReply.id.asc())
                     .limit(MAX_REPLIES)
                     .all())
        return results

    except HTTPException:
        raise
    except SQLAlchemyError:
        raise HTTPException(status_code=500, detail="Database error while saving smart replies")
    except Exception:
        raise HTTPException(status_code=500, detail="Unexpected error while saving smart replies")


@router.get(
    "/{room_id}",
    response_model=List[SmartReplyOut],
    summary="Get smart replies for a room (search + limit)"
)
def get_smart_replies(
    room_id: str,
    db: Session = Depends(get_db),
    q: Optional[str] = Query(None, max_length=40, description="case-insensitive search substring"),
    limit: int = Query(MAX_REPLIES, ge=1, le=MAX_REPLIES),
):
    query = db.query(SmartReply).filter(SmartReply.room_id == room_id)
    if q:
        query = query.filter(SmartReply.message.ilike(f"%{q.strip()}%"))
    return query.order_by(SmartReply.id.asc()).limit(limit).all()


@router.delete(
    "/{room_id}",
    summary="Delete one reply by id or exact message",
)
def delete_smart_reply(
    room_id: str,
    db: Session = Depends(get_db),
    reply_id: Optional[int] = Query(None, ge=1),
    message: Optional[str] = Query(None, description="exact message to delete"),
):
    if reply_id is None and not message:
        raise HTTPException(status_code=400, detail="Provide reply_id or message")
    q = db.query(SmartReply).filter(SmartReply.room_id == room_id)
    if reply_id is not None:
        q = q.filter(SmartReply.id == reply_id)
    else:
        q = q.filter(SmartReply.message == message.strip())

    deleted = q.delete(synchronize_session=False)
    db.commit()
    if not deleted:
        raise HTTPException(status_code=404, detail="Reply not found")
    return {"deleted": deleted}
