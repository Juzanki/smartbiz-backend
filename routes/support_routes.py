# backend/routes/support_routes.py
# -*- coding: utf-8 -*-
from __future__ import annotations
"""
Support API (mobile-first, international-ready)

Endpoints
- POST   /support/tickets                          -> create a ticket (idempotent)
- GET    /support/tickets                          -> list tickets (filters + cursor pagination)
- GET    /support/tickets/{ticket_id}              -> ticket details
- PATCH  /support/tickets/{ticket_id}              -> partial update (status/priority/assign/tags/subject/category)
- POST   /support/tickets/{ticket_id}/reply        -> add a message (public/internal; idempotent)
- GET    /support/tickets/{ticket_id}/messages     -> list messages (cursor pagination)
- POST   /support/tickets/{ticket_id}/close        -> close ticket
- POST   /support/tickets/{ticket_id}/assign       -> assign to agent
- POST   /support/tickets/{ticket_id}/csat         -> customer satisfaction rating
- GET    /support/stats                            -> quick counters (open/pending/resolved for current user)

Notes
- Uses support_crud if available; otherwise falls back to ORM.
- Idempotency headers supported if your model/CRUD has *_idempotency_key fields.
- UTC ISO timestamps for mobile/web clients.
"""
import re
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Literal, Optional

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query,
    Header,
    status,
)
from pydantic import BaseModel, Field, conint, constr
from sqlalchemy.orm import Session

from backend.db import get_db
from backend.auth import get_current_user
from backend.models.user import User

# Optional: CRUD helpers if you have them
try:
    from backend.crud import support_crud  # type: ignore
except Exception:  # pragma: no cover
    support_crud = None  # type: ignore

# Optional: ORM models for fallback path
try:
    from backend.models.support import SupportTicket
    from backend.models.support import SupportTicket
except Exception:  # pragma: no cover
    SupportTicket = None  # type: ignore
    SupportMessage = None  # type: ignore

router = APIRouter(prefix="/support", tags=["Support"])

# ------ mobile-first defaults ------
DEFAULT_PAGE_SIZE = 30
MAX_PAGE_SIZE = 100
MAX_SUBJECT = 120
MAX_BODY = 4000
MAX_TAGS = 10
MAX_TAG = 24

# ------ helpers ------
def _utcnow() -> datetime:
    return datetime.now(timezone.utc)

def _to_iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.astimezone(timezone.utc).isoformat() if isinstance(dt, datetime) else None

def _norm_text(s: str, max_len: int) -> str:
    s = re.sub(r"\s+", " ", (s or "")).strip()
    return s[:max_len].rstrip()

def _unique_tags(tags: Optional[List[str]]) -> List[str]:
    out, seen = [], set()
    for t in tags or []:
        tt = re.sub(r"\s+", "-", t.strip().lower())[:MAX_TAG]
        if tt and tt not in seen:
            seen.add(tt)
            out.append(tt)
        if len(out) >= MAX_TAGS:
            break
    return out

def _user_can(db: Session, user: User, action: str, ticket_id: Optional[int] = None) -> bool:
    guard = getattr(support_crud, "user_can", None)
    if callable(guard):
        return bool(guard(db, user, action, ticket_id=ticket_id))
    # Default allow creator/moderator semantics are handled per endpoint; no-op here.
    return True

# ------ Schemas ------
class AttachmentIn(BaseModel):
    url: Optional[str] = Field(None, description="Public or signed URL")
    file_id: Optional[str] = Field(None, description="If you store uploads by id")
    name: Optional[str] = None
    mime: Optional[str] = None
    size: Optional[int] = Field(None, ge=0)

class TicketCreate(BaseModel):
    subject: constr(min_length=3, max_length=MAX_SUBJECT)
    body: constr(min_length=1, max_length=MAX_BODY)
    category: Optional[str] = Field(None, description="e.g., billing, technical, abuse")
    priority: Literal["low", "normal", "high", "urgent"] = "normal"
    tags: Optional[List[str]] = None
    attachments: Optional[List[AttachmentIn]] = None
    metadata: Optional[Dict[str, Any]] = None

class TicketPatch(BaseModel):
    subject: Optional[constr(min_length=3, max_length=MAX_SUBJECT)] = None
    category: Optional[str] = None
    priority: Optional[Literal["low", "normal", "high", "urgent"]] = None
    status: Optional[Literal["open", "pending", "resolved", "closed"]] = None
    assigned_to: Optional[int] = Field(None, ge=1)
    tags: Optional[List[str]] = None

class TicketOut(BaseModel):
    id: int
    subject: str
    category: Optional[str] = None
    priority: str
    status: str
    requester_id: int
    assigned_to: Optional[int] = None
    tags: List[str] = []
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    last_activity_at: Optional[str] = None
    unread_for_requester: Optional[bool] = None
    unread_for_agent: Optional[bool] = None

class TicketSummaryOut(TicketOut):
    message_count: Optional[int] = None
    last_message_preview: Optional[str] = None

class MessageCreate(BaseModel):
    body: constr(min_length=1, max_length=MAX_BODY)
    visibility: Literal["public", "internal"] = "public"
    attachments: Optional[List[AttachmentIn]] = None
    metadata: Optional[Dict[str, Any]] = None

class MessageOut(BaseModel):
    id: int
    ticket_id: int
    sender_id: int
    body: str
    visibility: str
    created_at: Optional[str] = None
    attachments: Optional[List[AttachmentIn]] = None

class PageMeta(BaseModel):
    next_cursor: Optional[int] = None
    count: int

class TicketPageOut(BaseModel):
    meta: PageMeta
    items: List[TicketSummaryOut]

class MessagePageOut(BaseModel):
    meta: PageMeta
    items: List[MessageOut]

class AssignIn(BaseModel):
    assigned_to: int = Field(..., ge=1)

class CSATIn(BaseModel):
    rating: conint(ge=1, le=5)
    comment: Optional[constr(max_length=280)] = None

class CSATOut(BaseModel):
    ok: bool
    rating: int
    comment: Optional[str] = None

# ------ Serialization helpers (fallback path) ------
def _ser_ticket(t: Any) -> TicketOut:
    tags = getattr(t, "tags", []) or []
    if isinstance(tags, str):
        tags = [s for s in tags.split(",") if s]
    return TicketOut(
        id=int(getattr(t, "id")),
        subject=getattr(t, "subject"),
        category=getattr(t, "category", None),
        priority=getattr(t, "priority", "normal"),
        status=getattr(t, "status", "open"),
        requester_id=int(getattr(t, "requester_id")),
        assigned_to=getattr(t, "assigned_to", None),
        tags=list(tags),
        created_at=_to_iso(getattr(t, "created_at", None)),
        updated_at=_to_iso(getattr(t, "updated_at", None)),
        last_activity_at=_to_iso(getattr(t, "last_activity_at", None)),
    )

def _ser_message(m: Any) -> MessageOut:
    return MessageOut(
        id=int(getattr(m, "id")),
        ticket_id=int(getattr(m, "ticket_id")),
        sender_id=int(getattr(m, "sender_id")),
        body=getattr(m, "body"),
        visibility=getattr(m, "visibility", "public"),
        created_at=_to_iso(getattr(m, "created_at", None)),
        attachments=getattr(m, "attachments", None),
    )

# ------ Routes ------
@router.post(
    "/tickets",
    response_model=TicketOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create a support ticket",
)
def create_ticket(
    payload: TicketCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    idempotency_key: Optional[str] = Header(
        None, convert_underscores=False, description="Optional idempotency key"
    ),
):
    subj = _norm_text(payload.subject, MAX_SUBJECT)
    body = _norm_text(payload.body, MAX_BODY)
    tags = _unique_tags(payload.tags)

    if support_crud and hasattr(support_crud, "create_ticket"):
        return support_crud.create_ticket(
            db,
            requester_id=current_user.id,
            subject=subj,
            body=body,
            category=payload.category,
            priority=payload.priority,
            tags=tags,
            attachments=payload.attachments,
            metadata=payload.metadata,
            idempotency_key=idempotency_key,
        )

    if not SupportTicket or not SupportMessage:
        raise HTTPException(status_code=500, detail="Support models/CRUD are not available")

    # Fallback ORM path
    ticket = SupportTicket(
        subject=subj,
        category=payload.category,
        priority=payload.priority,
        status="open",
        requester_id=current_user.id,
        tags=",".join(tags) if hasattr(SupportTicket, "tags") else None,
        created_at=_utcnow(),
        updated_at=_utcnow(),
        last_activity_at=_utcnow(),
        ticket_idempotency_key=idempotency_key if hasattr(SupportTicket, "ticket_idempotency_key") else None,
    )
    db.add(ticket)
    db.flush()  # get ticket.id

    msg = SupportMessage(
        ticket_id=ticket.id,
        sender_id=current_user.id,
        body=body,
        visibility="public",
        created_at=_utcnow(),
        message_idempotency_key=idempotency_key if hasattr(SupportMessage, "message_idempotency_key") else None,
    )
    db.add(msg)
    db.commit()
    db.refresh(ticket)
    return _ser_ticket(ticket)

@router.get(
    "/tickets",
    response_model=TicketPageOut,
    summary="List tickets (filters + cursor pagination)",
)
def list_tickets(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    cursor_id: Optional[int] = Query(None, description="Paginate backward: id < cursor_id"),
    limit: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    q: Optional[str] = Query(None, max_length=80, description="Search subject/body"),
    status_eq: Optional[str] = Query(None, description="open|pending|resolved|closed"),
    category: Optional[str] = None,
    priority: Optional[str] = None,
    mine: bool = Query(True, description="If true, only my tickets; agents can set false"),
    assigned: Optional[bool] = Query(None, description="Filter by assigned state"),
):
    if support_crud and hasattr(support_crud, "list_tickets"):
        result = support_crud.list_tickets(
            db,
            user=current_user,
            cursor_id=cursor_id,
            limit=limit,
            q=q,
            status_eq=status_eq,
            category=category,
            priority=priority,
            mine=mine,
            assigned=assigned,
        )
        items = result.get("items", [])
        next_cursor = result.get("next_cursor")
        return TicketPageOut(meta=PageMeta(next_cursor=next_cursor, count=len(items)), items=items)

    if not SupportTicket:
        raise HTTPException(status_code=500, detail="SupportTicket model/CRUD not available")

    # Fallback ORM query
    query = db.query(SupportTicket)
    if mine:
        query = query.filter(SupportTicket.requester_id == current_user.id)
    if status_eq:
        query = query.filter(SupportTicket.status == status_eq)
    if category:
        query = query.filter(SupportTicket.category == category)
    if priority:
        query = query.filter(SupportTicket.priority == priority)
    if assigned is not None and hasattr(SupportTicket, "assigned_to"):
        if assigned:
            query = query.filter(SupportTicket.assigned_to.isnot(None))
        else:
            query = query.filter(SupportTicket.assigned_to.is_(None))
    if q:
        like = f"%{q.strip()}%"
        # If body search requires join to messages, skip for fallback
        query = query.filter(SupportTicket.subject.ilike(like))

    if cursor_id:
        query = query.filter(SupportTicket.id < cursor_id)
    rows = query.order_by(SupportTicket.id.desc()).limit(limit).all()

    # Build summaries (no heavy joins)
    items: List[TicketSummaryOut] = []
    for t in rows:
        preview = None
        if hasattr(SupportTicket, "last_message_preview"):
            preview = getattr(t, "last_message_preview")
        items.append(TicketSummaryOut(**_ser_ticket(t).model_dump(), last_message_preview=preview))
    next_cursor = rows[-1].id if rows else None
    return TicketPageOut(meta=PageMeta(next_cursor=next_cursor, count=len(items)), items=items)

@router.get(
    "/tickets/{ticket_id}",
    response_model=TicketOut,
    summary="Get ticket details",
)
def get_ticket(
    ticket_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if support_crud and hasattr(support_crud, "get_ticket"):
        t = support_crud.get_ticket(db, ticket_id, viewer=current_user)
        if not t:
            raise HTTPException(status_code=404, detail="Ticket not found")
        return t

    if not SupportTicket:
        raise HTTPException(status_code=500, detail="SupportTicket model/CRUD not available")

    t = db.query(SupportTicket).filter(SupportTicket.id == ticket_id).one_or_none()
    if not t:
        raise HTTPException(status_code=404, detail="Ticket not found")
    # Basic view permission: requester or assigned agent
    if getattr(t, "requester_id", None) != current_user.id and getattr(t, "assigned_to", None) != current_user.id:
        if not _user_can(db, current_user, "view_any", ticket_id=ticket_id):
            raise HTTPException(status_code=403, detail="Not allowed to view this ticket")
    return _ser_ticket(t)

@router.patch(
    "/tickets/{ticket_id}",
    response_model=TicketOut,
    summary="Update ticket fields (partial)",
)
def update_ticket(
    ticket_id: int,
    patch: TicketPatch,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    if_match: Optional[str] = Header(None, convert_underscores=False, description="Optional optimistic lock token"),
):
    if support_crud and hasattr(support_crud, "update_ticket"):
        t = support_crud.update_ticket(db, ticket_id, patch, actor=current_user, if_match=if_match)
        if not t:
            raise HTTPException(status_code=404, detail="Ticket not found")
        return t

    if not SupportTicket:
        raise HTTPException(status_code=500, detail="SupportTicket model/CRUD not available")

    t = db.query(SupportTicket).filter(SupportTicket.id == ticket_id).with_for_update().one_or_none()
    if not t:
        raise HTTPException(status_code=404, detail="Ticket not found")
    # Permission: requester can modify subject/category; agents can modify status/priority/assign
    if getattr(t, "requester_id", None) != current_user.id and not _user_can(db, current_user, "modify_any", ticket_id):
        raise HTTPException(status_code=403, detail="Not allowed to modify this ticket")

    data = patch.model_dump(exclude_unset=True)
    if "tags" in data:
        data["tags"] = ",".join(_unique_tags(data["tags"])) if hasattr(SupportTicket, "tags") else None
    if "subject" in data:
        data["subject"] = _norm_text(data["subject"], MAX_SUBJECT)

    for k, v in data.items():
        if hasattr(t, k):
            setattr(t, k, v)
    if hasattr(t, "updated_at"):
        t.updated_at = _utcnow()
    db.commit()
    db.refresh(t)
    return _ser_ticket(t)

@router.post(
    "/tickets/{ticket_id}/reply",
    response_model=MessageOut,
    status_code=status.HTTP_201_CREATED,
    summary="Add a message to a ticket",
)
def add_reply(
    ticket_id: int,
    payload: MessageCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    idempotency_key: Optional[str] = Header(None, convert_underscores=False),
):
    body = _norm_text(payload.body, MAX_BODY)

    if support_crud and hasattr(support_crud, "add_message"):
        msg = support_crud.add_message(
            db,
            ticket_id=ticket_id,
            sender_id=current_user.id,
            body=body,
            visibility=payload.visibility,
            attachments=payload.attachments,
            metadata=payload.metadata,
            idempotency_key=idempotency_key,
        )
        if not msg:
            raise HTTPException(status_code=404, detail="Ticket not found")
        return msg

    if not (SupportTicket and SupportMessage):
        raise HTTPException(status_code=500, detail="Support models/CRUD not available")

    t = db.query(SupportTicket).filter(SupportTicket.id == ticket_id).with_for_update().one_or_none()
    if not t:
        raise HTTPException(status_code=404, detail="Ticket not found")
    if getattr(t, "requester_id", None) != current_user.id and getattr(t, "assigned_to", None) != current_user.id:
        if not _user_can(db, current_user, "reply_any", ticket_id=ticket_id):
            raise HTTPException(status_code=403, detail="Not allowed to reply to this ticket")

    m = SupportMessage(
        ticket_id=ticket_id,
        sender_id=current_user.id,
        body=body,
        visibility=payload.visibility,
        created_at=_utcnow(),
        message_idempotency_key=idempotency_key if hasattr(SupportMessage, "message_idempotency_key") else None,
        attachments=payload.attachments if hasattr(SupportMessage, "attachments") else None,
        metadata=payload.metadata if hasattr(SupportMessage, "metadata") else None,
    )
    db.add(m)
    # touch ticket
    if hasattr(t, "last_activity_at"):
        t.last_activity_at = _utcnow()
    if hasattr(t, "updated_at"):
        t.updated_at = _utcnow()
    db.commit()
    db.refresh(m)
    return _ser_message(m)

@router.get(
    "/tickets/{ticket_id}/messages",
    response_model=MessagePageOut,
    summary="List messages in a ticket (cursor pagination)",
)
def list_messages(
    ticket_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    cursor_id: Optional[int] = Query(None, description="id < cursor_id"),
    limit: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    visibility: Optional[Literal["public", "internal"]] = Query(None),
):
    if support_crud and hasattr(support_crud, "list_messages"):
        result = support_crud.list_messages(
            db,
            ticket_id=ticket_id,
            viewer=current_user,
            cursor_id=cursor_id,
            limit=limit,
            visibility=visibility,
        )
        items = result.get("items", [])
        next_cursor = result.get("next_cursor")
        return MessagePageOut(meta=PageMeta(next_cursor=next_cursor, count=len(items)), items=items)

    if not (SupportTicket and SupportMessage):
        raise HTTPException(status_code=500, detail="Support models/CRUD not available")

    # Basic permission
    t = db.query(SupportTicket).filter(SupportTicket.id == ticket_id).one_or_none()
    if not t:
        raise HTTPException(status_code=404, detail="Ticket not found")
    if getattr(t, "requester_id", None) != current_user.id and getattr(t, "assigned_to", None) != current_user.id:
        if not _user_can(db, current_user, "view_any", ticket_id=ticket_id):
            raise HTTPException(status_code=403, detail="Not allowed to view this ticket")

    q = db.query(SupportMessage).filter(SupportMessage.ticket_id == ticket_id)
    if visibility:
        q = q.filter(SupportMessage.visibility == visibility)
    if cursor_id:
        q = q.filter(SupportMessage.id < cursor_id)
    rows = q.order_by(SupportMessage.id.desc()).limit(limit).all()
    next_cursor = rows[-1].id if rows else None
    return MessagePageOut(meta=PageMeta(next_cursor=next_cursor, count=len(rows)),
                          items=[_ser_message(m) for m in rows])

@router.post(
    "/tickets/{ticket_id}/close",
    response_model=TicketOut,
    summary="Close a ticket (status=closed)",
)
def close_ticket(
    ticket_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if support_crud and hasattr(support_crud, "close_ticket"):
        t = support_crud.close_ticket(db, ticket_id, actor=current_user)
        if not t:
            raise HTTPException(status_code=404, detail="Ticket not found")
        return t
    return update_ticket(ticket_id, TicketPatch(status="closed"), db, current_user)

@router.post(
    "/tickets/{ticket_id}/assign",
    response_model=TicketOut,
    summary="Assign a ticket to an agent",
)
def assign_ticket(
    ticket_id: int,
    payload: AssignIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if support_crud and hasattr(support_crud, "assign_ticket"):
        t = support_crud.assign_ticket(db, ticket_id, assigned_to=payload.assigned_to, actor=current_user)
        if not t:
            raise HTTPException(status_code=404, detail="Ticket not found")
        return t
    return update_ticket(ticket_id, TicketPatch(assigned_to=payload.assigned_to), db, current_user)

@router.post(
    "/tickets/{ticket_id}/csat",
    response_model=CSATOut,
    summary="Record a CSAT rating for a ticket",
)
def csat_ticket(
    ticket_id: int,
    payload: CSATIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if support_crud and hasattr(support_crud, "csat_ticket"):
        ok = support_crud.csat_ticket(db, ticket_id, user_id=current_user.id, rating=payload.rating, comment=payload.comment)
        return CSATOut(ok=bool(ok), rating=payload.rating, comment=payload.comment)

    # Fallback: store on ticket if fields exist
    if not SupportTicket:
        raise HTTPException(status_code=500, detail="SupportTicket model/CRUD not available")
    t = db.query(SupportTicket).filter(SupportTicket.id == ticket_id).with_for_update().one_or_none()
    if not t:
        raise HTTPException(status_code=404, detail="Ticket not found")
    if hasattr(t, "csat_rating"):
        t.csat_rating = int(payload.rating)
    if hasattr(t, "csat_comment"):
        t.csat_comment = payload.comment
    if hasattr(t, "updated_at"):
        t.updated_at = _utcnow()
    db.commit()
    return CSATOut(ok=True, rating=payload.rating, comment=payload.comment)

@router.get(
    "/stats",
    summary="Quick support stats for current user",
)
def get_stats(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if support_crud and hasattr(support_crud, "get_stats"):
        return support_crud.get_stats(db, current_user)

    if not SupportTicket:
        raise HTTPException(status_code=500, detail="SupportTicket model/CRUD not available")

    # Simple counts (fallback)
    base = db.query(SupportTicket).filter(SupportTicket.requester_id == current_user.id)
    def _count(status_val: str) -> int:
        return base.filter(SupportTicket.status == status_val).count()
    return {
        "open": _count("open"),
        "pending": _count("pending"),
        "resolved": _count("resolved"),
        "closed": _count("closed"),
    }



