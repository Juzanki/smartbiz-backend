# backend/routes/withdraw.py
# -*- coding: utf-8 -*-
from __future__ import annotations
"""
Withdraw Requests API (mobile-first, international-ready)

Backwards compatible with your current CRUD:
- Uses withdraw_crud.create_withdraw_request/get_requests_by_user/get_all_requests/review_withdraw_request
- If richer helpers exist (list_paginated, get_request, cancel_request, mark_paid), they'll be used automatically.

Endpoints
- POST   /withdraw/                              -> create a withdraw request (idempotent)
- GET    /withdraw/mine                          -> list my requests (lightweight)
- GET    /withdraw/mine/page                     -> list my requests (cursor pagination + filters)
- GET    /withdraw/{request_id}                  -> get one request (owner or admin)
- PUT    /withdraw/{request_id}/review           -> admin approve/reject (optimistic-lock ready)
- POST   /withdraw/{request_id}/cancel           -> UserOut cancel if still pending
- POST   /withdraw/{request_id}/mark-paid        -> admin mark as paid, attach payout reference
- GET    /withdraw/admin                         -> admin list (lightweight; kept for backwards compatibility)
- GET    /withdraw/admin/page                    -> admin list (cursor pagination + filters)

Notes
- Mobile-first responses (small, paginated).
- Optional Idempotency-Key header to avoid duplicate submissions.
- Optional If-Match header to support optimistic concurrency (if your model/CRUD adds a version/etag).
- UTC ISO timestamps recommended in your schemas/models.
"""
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Literal

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Header,
    Query,
    status,
)
from pydantic import BaseModel, Field, conint, constr
from sqlalchemy.orm import Session

from backend.db import get_db
from backend.auth import get_current_user
from backend.models.user import User
from backend.schemas.withdraw import (
from backend.schemas.user import UserOut
    WithdrawRequestCreate,
    WithdrawRequestOut,
    WithdrawRequestReview,
)
from backend.crud import withdraw_crud

router = APIRouter(prefix="/withdraw", tags=["Withdraw Requests"])

# ---------- mobile-first defaults ----------
DEFAULT_PAGE_SIZE = 30
MAX_PAGE_SIZE = 100
MIN_AMOUNT = 1  # minor units or currency units per your schema

# ---------- helpers ----------
def _utcnow() -> datetime:
    return datetime.now(timezone.utc)

def _is_admin(user: User) -> bool:
    return str(getattr(user, "role", "")).lower() in {"admin", "owner"}

def _require_admin(user: User) -> None:
    if not _is_admin(user):
        raise HTTPException(status_code=403, detail="Admin only")

def _dump_partial(model: Any) -> Dict[str, Any]:
    try:
        return model.model_dump(exclude_unset=True)
    except AttributeError:
        return model.dict(exclude_unset=True)

# ---------- tiny local schemas for extra endpoints ----------
class PageMeta(BaseModel):
    next_cursor: Optional[int] = None
    count: int

class RequestPageOut(BaseModel):
    meta: PageMeta
    items: List[WithdrawRequestOut]

class CancelIn(BaseModel):
    reason: Optional[constr(max_length=200)] = None

class MarkPaidIn(BaseModel):
    payout_reference: constr(min_length=2, max_length=120)
    note: Optional[constr(max_length=200)] = None

# ---------- create (user) ----------
@router.post(
    "/",
    response_model=WithdrawRequestOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create a withdraw request (idempotent)",
)
def request_withdraw(
    request_data: WithdrawRequestCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    idempotency_key: Optional[str] = Header(
        None, convert_underscores=False, description="Optional key to prevent duplicate submissions"
    ),
):
    # Ownership check
    if current_user.id != request_data.user_id:
        raise HTTPException(status_code=403, detail="Unauthorized")
    # Basic validation
    data = _dump_partial(request_data)
    amount = int(data.get("amount", data.get("value", 0)) or 0)  # support either 'amount' or legacy 'value'
    if amount < MIN_AMOUNT:
        raise HTTPException(status_code=400, detail="Amount must be greater than zero")

    # Prefer extended CRUD signature if available
    try:
        return withdraw_crud.create_withdraw_request(db, request_data, idempotency_key=idempotency_key)  # type: ignore[misc]
    except TypeError:
        return withdraw_crud.create_withdraw_request(db, request_data)

# ---------- list mine (lightweight) ----------
@router.get(
    "/mine",
    response_model=List[WithdrawRequestOut],
    summary="List my withdraw requests (lightweight)",
)
def view_my_requests(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return withdraw_crud.get_requests_by_user(db, current_user.id)

# ---------- list mine (paginated + filters) ----------
@router.get(
    "/mine/page",
    response_model=RequestPageOut,
    summary="List my withdraw requests (cursor pagination + filters)",
)
def page_my_requests(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    cursor_id: Optional[int] = Query(None, description="Paginate backward: id < cursor_id"),
    limit: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    status_eq: Optional[str] = Query(None, description="Filter by status: pending|approved|rejected|paid|cancelled"),
):
    # Prefer a paginated CRUD if available
    if hasattr(withdraw_crud, "list_requests_by_user"):
        result = withdraw_crud.list_requests_by_user(
            db, user_id=current_user.id, cursor_id=cursor_id, limit=limit, status_eq=status_eq
        )
        items = result.get("items", [])
        next_cursor = result.get("next_cursor")
        return RequestPageOut(meta=PageMeta(next_cursor=next_cursor, count=len(items)), items=items)

    # Fallback: slice from full list
    items = withdraw_crud.get_requests_by_user(db, current_user.id)
    if status_eq:
        items = [x for x in items if getattr(x, "status", None) == status_eq]
    if cursor_id:
        items = [x for x in items if getattr(x, "id", 0) < cursor_id]
    items = items[:limit]
    next_cursor = getattr(items[-1], "id", None) if items else None
    return RequestPageOut(meta=PageMeta(next_cursor=next_cursor, count=len(items)), items=items)

# ---------- get one (owner or admin) ----------
@router.get(
    "/{request_id}",
    response_model=WithdrawRequestOut,
    summary="Get a single withdraw request",
)
def get_request(
    request_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if hasattr(withdraw_crud, "get_request"):
        req = withdraw_crud.get_request(db, request_id=request_id, viewer_id=current_user.id)
        if not req:
            raise HTTPException(status_code=404, detail="Request not found")
        # CRUD may already enforce viewer; if not, do a minimal check:
        if not _is_admin(current_user) and getattr(req, "user_id", None) != current_user.id:
            raise HTTPException(status_code=403, detail="Unauthorized")
        return req
    raise HTTPException(status_code=501, detail="get_request not supported by withdraw_crud")

# ---------- cancel (user) ----------
@router.post(
    "/{request_id}/cancel",
    response_model=WithdrawRequestOut,
    summary="Cancel a pending request (user only)",
)
def cancel_request(
    request_id: int,
    body: CancelIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if hasattr(withdraw_crud, "cancel_request"):
        req = withdraw_crud.cancel_request(db, request_id=request_id, user_id=current_user.id, reason=body.reason)
        if not req:
            raise HTTPException(status_code=404, detail="Request not found or not cancellable")
        return req
    raise HTTPException(status_code=501, detail="cancel_request not supported by withdraw_crud")

# ---------- admin list (lightweight - kept for compatibility) ----------
@router.get(
    "/admin",
    response_model=List[WithdrawRequestOut],
    summary="Admin: list all requests (lightweight)",
)
def admin_view_all_requests(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_admin(current_user)
    return withdraw_crud.get_all_requests(db)

# ---------- admin list (paginated + filters) ----------
@router.get(
    "/admin/page",
    response_model=RequestPageOut,
    summary="Admin: list requests (cursor pagination + filters)",
)
def admin_page_requests(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    cursor_id: Optional[int] = Query(None, description="Paginate backward: id < cursor_id"),
    limit: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    status_eq: Optional[str] = Query(None, description="pending|approved|rejected|paid|cancelled"),
    user_id: Optional[int] = Query(None, ge=1),
):
    _require_admin(current_user)
    if hasattr(withdraw_crud, "list_requests"):
        result = withdraw_crud.list_requests(
            db, cursor_id=cursor_id, limit=limit, status_eq=status_eq, user_id=user_id
        )
        items = result.get("items", [])
        next_cursor = result.get("next_cursor")
        return RequestPageOut(meta=PageMeta(next_cursor=next_cursor, count=len(items)), items=items)

    # Fallback via get_all_requests (no DB-level paging)
    rows = withdraw_crud.get_all_requests(db)
    if status_eq:
        rows = [r for r in rows if getattr(r, "status", None) == status_eq]
    if user_id:
        rows = [r for r in rows if getattr(r, "user_id", None) == user_id]
    if cursor_id:
        rows = [r for r in rows if getattr(r, "id", 0) < cursor_id]
    items = rows[:limit]
    next_cursor = getattr(items[-1], "id", None) if items else None
    return RequestPageOut(meta=PageMeta(next_cursor=next_cursor, count=len(items)), items=items)

# ---------- admin review ----------
@router.put(
    "/{request_id}/review",
    response_model=WithdrawRequestOut,
    summary="Admin: review (approve/reject)",
)
def review_request(
    request_id: int,
    review: WithdrawRequestReview,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    if_match: Optional[str] = Header(
        None, convert_underscores=False, description="Optional concurrency token (e.g., version/etag)"
    ),
    idempotency_key: Optional[str] = Header(
        None, convert_underscores=False, description="Optional key to prevent double reviews"
    ),
):
    _require_admin(current_user)

    # Prefer extended signature if CRUD supports it
    try:
        return withdraw_crud.review_withdraw_request(  # type: ignore[misc]
            db, request_id, review, actor_id=current_user.id, if_match=if_match, idempotency_key=idempotency_key
        )
    except TypeError:
        return withdraw_crud.review_withdraw_request(db, request_id, review)

# ---------- admin mark paid ----------
@router.post(
    "/{request_id}/mark-paid",
    response_model=WithdrawRequestOut,
    summary="Admin: mark a request as paid & attach payout reference",
)
def mark_paid(
    request_id: int,
    body: MarkPaidIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    if_match: Optional[str] = Header(None, convert_underscores=False),
):
    _require_admin(current_user)

    if hasattr(withdraw_crud, "mark_paid"):
        req = withdraw_crud.mark_paid(
            db,
            request_id=request_id,
            payout_reference=body.payout_reference,
            note=body.note,
            actor_id=current_user.id,
            if_match=if_match,
        )
        if not req:
            raise HTTPException(status_code=404, detail="Request not found or cannot be marked as paid")
        return req

    # If not implemented, guide clearly
    raise HTTPException(status_code=501, detail="mark_paid not supported by withdraw_crud")

