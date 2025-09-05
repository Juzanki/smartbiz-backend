# -*- coding: utf-8 -*-
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional, List, Iterable, Dict

from fastapi import (
    APIRouter, Depends, HTTPException, status, Query, Path, Response
)
from sqlalchemy.orm import Session
from sqlalchemy import func, or_

from backend.db import get_db
from backend.models.support import SupportTicket
from backend.models.user import User as UserModel
from backend.schemas import SupportTicketOut
from backend.dependencies import get_current_user

# --------------------------- RBAC (admin/owner) --------------------------- #
try:
    # Kama una guard ya admin tayari, itumie.
    from backend.dependencies import check_admin as _admin_guard  # type: ignore

    def admin_guard(_=Depends(_admin_guard)) -> None:
        return None
except Exception:
    # Fallback: ruhusu admin/owner tu.
    def admin_guard(current_user=Depends(get_current_user)) -> None:
        if str(getattr(current_user, "role", "")).lower() not in {"admin", "owner"}:
            raise HTTPException(status_code=403, detail="Not authorized")

# ------------------------------- Router ---------------------------------- #
router = APIRouter(
    prefix="/admin/support",
    tags=["Admin Support"],
    dependencies=[Depends(admin_guard)],
)

# ------------------------------- Helpers --------------------------------- #
MAX_LIMIT = 100
ALLOWED_SORT_TICKET = ("created_at", "updated_at", "priority", "id", "status")
ALLOWED_ORDER = ("asc", "desc")
ALLOWED_STATUS = {"open", "pending", "closed", "resolved"}   # rekebisha kulingana na domain yako
ALLOWED_PRIORITY = {"low", "normal", "high", "urgent"}       # rekebisha kulingana na domain yako

def _clamp_limit(limit: Optional[int], default: int = 20) -> int:
    if not limit:
        return default
    return max(1, min(int(limit), MAX_LIMIT))

def _order_by_whitelist(model, sort_by: str, order: str, allow: Iterable[str]):
    key = sort_by if sort_by in allow else next(iter(allow))
    col = getattr(model, key)
    return col.asc() if order == "asc" else col.desc()

def _parse_dt(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except Exception:
        try:
            return datetime.strptime(s, "%Y-%m-%d")
        except Exception:
            return None

def _next_cursor(items: List[SupportTicket]) -> Optional[int]:
    if not items:
        return None
    # Tunatumia ID cursor (kwenye sort desc ni item ya mwisho)
    return int(items[-1].id)

# ============================= UPDATE PAYLOAD ============================= #
# Pydantic model sahihi (badala ya class inayorithi dict)
try:
    # tumia Pydantic v2 ikiwa ipo
    from pydantic import BaseModel, Field, ConfigDict
    _P2 = True
except Exception:  # v1 fallback
    from pydantic import BaseModel, Field  # type: ignore
    ConfigDict = dict  # type: ignore
    _P2 = False

class TicketUpdatePayload(BaseModel):
    """Vigezo vinavyoruhusiwa kubadilishwa kwa ticket."""
    status: Optional[str] = Field(None, description="open|pending|closed|resolved")
    priority: Optional[str] = Field(None, description="low|normal|high|urgent")
    assignee_id: Optional[int] = Field(None, ge=1)
    category: Optional[str] = Field(None, max_length=64)
    note: Optional[str] = Field(None, max_length=500)

    # lowecase normalizers
    def _norm(self) -> None:
        if self.status is not None:
            self.status = self.status.strip().lower()
        if self.priority is not None:
            self.priority = self.priority.strip().lower()
        if self.category is not None:
            self.category = self.category.strip()

    if _P2:
        model_config = ConfigDict(extra="ignore")
    else:
        class Config:  # type: ignore
            extra = "ignore"

# =============================== LIST (mobile-first) =============================== #
@router.get(
    "/",
    response_model=List[SupportTicketOut],
    summary="Admin: List support tickets (filter/sort/paginate/cursor)",
)
def list_tickets(
    response: Response,
    db: Session = Depends(get_db),
    # --- Filters ---
    q: Optional[str] = Query(None, description="Search subject/body/category"),
    status_f: Optional[str] = Query(None, alias="status"),
    priority: Optional[str] = Query(None),
    user_id: Optional[int] = Query(None),
    assigned_to: Optional[int] = Query(None),
    created_from: Optional[str] = Query(None),
    created_to: Optional[str] = Query(None),
    # --- Sorting & Pagination ---
    sort_by: str = Query("created_at"),
    order: str = Query("desc"),
    limit: int = Query(20, ge=1, le=MAX_LIMIT),
    offset: int = Query(0, ge=0, description="Ignored if cursor provided"),
    cursor: Optional[int] = Query(None, description="Cursor for keyset pagination"),
    with_count: bool = Query(False, description="Include X-Total-Count header"),
):
    """
    - Default `limit=20`
    - Cursor-based pagination (`cursor=<last_id>`) — haraka kuliko offset kwenye data kubwa.
    - Headers: `X-Total-Count` (hiari), `X-Limit`, `X-Offset`, `X-Cursor-Next`
    """
    limit = _clamp_limit(limit)

    # normalize/validate order & sort_by
    order = (order or "desc").lower()
    if order not in ALLOWED_ORDER:
        order = "desc"
    sort_by = (sort_by or "created_at")
    if sort_by not in ALLOWED_SORT_TICKET:
        sort_by = "created_at"

    qy = db.query(SupportTicket)

    # Filters
    if status_f and status_f in ALLOWED_STATUS and hasattr(SupportTicket, "status"):
        qy = qy.filter(SupportTicket.status == status_f)

    if priority and hasattr(SupportTicket, "priority"):
        qy = qy.filter(SupportTicket.priority == priority)

    if user_id and hasattr(SupportTicket, "user_id"):
        qy = qy.filter(SupportTicket.user_id == user_id)

    if assigned_to and hasattr(SupportTicket, "assignee_id"):
        qy = qy.filter(SupportTicket.assignee_id == assigned_to)

    if q:
        like = f"%{q.strip()}%"
        conds = []
        for field in ("subject", "body", "category"):
            if hasattr(SupportTicket, field):
                conds.append(getattr(SupportTicket, field).ilike(like))
        if conds:
            qy = qy.filter(or_(*conds))

    dt_from = _parse_dt(created_from)
    dt_to = _parse_dt(created_to)
    if dt_from and hasattr(SupportTicket, "created_at"):
        qy = qy.filter(SupportTicket.created_at >= dt_from)
    if dt_to and hasattr(SupportTicket, "created_at"):
        qy = qy.filter(SupportTicket.created_at <= dt_to)

    # Sorting (whitelisted)
    qy = qy.order_by(_order_by_whitelist(SupportTicket, sort_by, order, ALLOWED_SORT_TICKET))

    total = None
    if with_count:
        total = qy.with_entities(func.count(SupportTicket.id)).scalar() or 0

    # Pagination: cursor (keyset) > offset
    if cursor and hasattr(SupportTicket, "id"):
        if order == "desc":
            qy = qy.filter(SupportTicket.id < cursor)
        else:
            qy = qy.filter(SupportTicket.id > cursor)
        items = qy.limit(limit).all()
        response.headers["X-Offset"] = "0"
    else:
        items = qy.offset(offset).limit(limit).all()
        response.headers["X-Offset"] = str(offset)

    # Headers
    if total is not None:
        response.headers["X-Total-Count"] = str(total)
    response.headers["X-Limit"] = str(limit)

    nxt = _next_cursor(items) if order == "desc" else None
    if nxt:
        response.headers["X-Cursor-Next"] = str(nxt)

    return items

# =============================== GET ONE =============================== #
@router.get(
    "/{ticket_id}",
    response_model=SupportTicketOut,
    summary="Admin: Get single support ticket",
)
def get_ticket(
    ticket_id: int = Path(..., ge=1),
    db: Session = Depends(get_db),
):
    ticket = (
        db.query(SupportTicket)
        .filter(SupportTicket.id == ticket_id)
        .first()
    )
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket not found")
    return ticket

# ============================= UPDATE/PATCH ============================== #
@router.patch(
    "/{ticket_id}",
    response_model=SupportTicketOut,
    summary="Admin: Update fields (status/priority/assignee/category)",
)
def update_ticket(
    ticket_id: int = Path(..., ge=1),
    data: TicketUpdatePayload = ...,
    db: Session = Depends(get_db),
):
    ticket = (
        db.query(SupportTicket)
        .filter(SupportTicket.id == ticket_id)
        .with_for_update(of=SupportTicket, nowait=False)
        .first()
    )
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket not found")

    data._norm()  # normalize lowercase/trim

    if data.status is not None and hasattr(ticket, "status"):
        if ALLOWED_STATUS and data.status not in ALLOWED_STATUS:
            raise HTTPException(status_code=422, detail=f"Invalid status. Allowed: {sorted(ALLOWED_STATUS)}")
        ticket.status = data.status

    if data.priority is not None and hasattr(ticket, "priority"):
        if ALLOWED_PRIORITY and data.priority not in ALLOWED_PRIORITY:
            raise HTTPException(status_code=422, detail=f"Invalid priority. Allowed: {sorted(ALLOWED_PRIORITY)}")
        ticket.priority = data.priority

    if data.assignee_id is not None and hasattr(ticket, "assignee_id"):
        aid = int(data.assignee_id)
        if not db.query(UserModel).filter(UserModel.id == aid).first():
            raise HTTPException(status_code=404, detail="Assignee not found")
        ticket.assignee_id = aid

    if data.category is not None and hasattr(ticket, "category"):
        ticket.category = data.category

    # (note field ikiwa ipo kwenye model yako, i-update pia)
    if hasattr(ticket, "note") and data.note is not None:
        ticket.note = data.note

    if hasattr(ticket, "updated_at"):
        ticket.updated_at = datetime.now(timezone.utc)

    try:
        db.commit()
        db.refresh(ticket)
    except Exception:
        db.rollback()
        raise HTTPException(status_code=500, detail="Failed to update ticket")

    return ticket

# =============================== CLOSE / REOPEN =============================== #
@router.put("/close/{ticket_id}", status_code=status.HTTP_200_OK, summary="Admin: Close ticket")
def close_ticket(ticket_id: int, db: Session = Depends(get_db)):
    ticket = (
        db.query(SupportTicket)
        .filter(SupportTicket.id == ticket_id)
        .with_for_update(of=SupportTicket)
        .first()
    )
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket not found")
    if hasattr(ticket, "status"):
        ticket.status = "closed"
    if hasattr(ticket, "updated_at"):
        ticket.updated_at = datetime.now(timezone.utc)
    try:
        db.commit()
    except Exception:
        db.rollback()
        raise HTTPException(status_code=500, detail="Failed to close ticket")
    return {"message": "Ticket closed"}

@router.put("/reopen/{ticket_id}", status_code=status.HTTP_200_OK, summary="Admin: Reopen ticket")
def reopen_ticket(ticket_id: int, db: Session = Depends(get_db)):
    ticket = (
        db.query(SupportTicket)
        .filter(SupportTicket.id == ticket_id)
        .with_for_update(of=SupportTicket)
        .first()
    )
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket not found")
    if hasattr(ticket, "status"):
        ticket.status = "open"
    if hasattr(ticket, "updated_at"):
        ticket.updated_at = datetime.now(timezone.utc)
    try:
        db.commit()
    except Exception:
        db.rollback()
        raise HTTPException(status_code=500, detail="Failed to reopen ticket")
    return {"message": "Ticket reopened"}

# =============================== BULK OPS =============================== #
@router.post("/bulk/close", summary="Admin: Close multiple tickets")
def bulk_close_tickets(ids: List[int], db: Session = Depends(get_db)):
    if not ids:
        return {"closed": 0}
    items = db.query(SupportTicket).filter(SupportTicket.id.in_([int(i) for i in ids])).all()
    count = 0
    now = datetime.now(timezone.utc)
    for t in items:
        if hasattr(t, "status"):
            t.status = "closed"
        if hasattr(t, "updated_at"):
            t.updated_at = now
        count += 1
    try:
        db.commit()
    except Exception:
        db.rollback()
        raise HTTPException(status_code=500, detail="Bulk close failed")
    return {"closed": count}

# =============================== STATS =============================== #
@router.get("/stats", summary="Admin: Ticket stats summary")
def tickets_stats(
    db: Session = Depends(get_db),
    created_from: Optional[str] = Query(None),
    created_to: Optional[str] = Query(None),
):
    qy = db.query(SupportTicket)
    df = _parse_dt(created_from)
    dt = _parse_dt(created_to)
    if df and hasattr(SupportTicket, "created_at"):
        qy = qy.filter(SupportTicket.created_at >= df)
    if dt and hasattr(SupportTicket, "created_at"):
        qy = qy.filter(SupportTicket.created_at <= dt)
    rows = (
        qy.with_entities(SupportTicket.status, func.count(SupportTicket.id))
          .group_by(SupportTicket.status)
          .all()
    )
    return [{"status": (s or "unknown"), "count": int(c or 0)} for (s, c) in rows]
