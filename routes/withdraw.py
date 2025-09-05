# backend/routes/withdraw_routes.py
# -*- coding: utf-8 -*-
from __future__ import annotations
"""
Withdraw Requests API (mobile-first, international-ready)

Backwards compatible routes kept:
- POST /withdraw
- GET  /withdrawals/pending
- POST /withdrawals/{request_id}/approve
- POST /withdrawals/{request_id}/reject

New, optional routes:
- GET  /withdraw/mine                      -> my requests (lightweight)
- GET  /withdraw/mine/page                 -> my requests (cursor pagination + filter)
- GET  /withdraw/{request_id}              -> request details (owner or admin)
- POST /withdraw/{request_id}/cancel       -> UserOut cancel (if pending)
- POST /withdrawals/{request_id}/mark-paid -> admin mark as paid (attach payout reference)
- GET  /withdrawals/admin/page             -> admin list (cursor pagination + filters)

Notes:
- Supports optional headers: Idempotency-Key, If-Match (CRUD may use them if implemented).
- English-only code/docs. Designed to keep payloads small for mobile clients.
"""
from typing import List, Optional

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Header,
    Query,
    status,
)
from pydantic import BaseModel, Field, constr, conint
from sqlalchemy.orm import Session

from backend.db import get_db
from backend.crud import withdraw_crud
from backend.schemas import withdraw_schemas
from backend.schemas.user import UserOut
from backend.dependencies import get_current_user, get_admin_user

router = APIRouter(tags=["Withdraw Requests"])

# ---------- small helper models for new endpoints ----------
class PageMeta(BaseModel):
    next_cursor: Optional[int] = None
    count: int

class RequestPageOut(BaseModel):
    meta: PageMeta
    items: List[withdraw_schemas.WithdrawRequestOut]

class CancelIn(BaseModel):
    reason: Optional[constr(max_length=200)] = None

class MarkPaidIn(BaseModel):
    payout_reference: constr(min_length=2, max_length=120)
    note: Optional[constr(max_length=200)] = None

# ---------- User: create request (kept path) ----------
@router.post("/withdraw", response_model=withdraw_schemas.WithdrawRequestOut, status_code=status.HTTP_201_CREATED)
def request_withdrawal(
    data: withdraw_schemas.WithdrawRequestCreate,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
    idempotency_key: Optional[str] = Header(
        None, convert_underscores=False, description="Optional key to avoid duplicate submissions"
    ),
):
    # Enforce ownership
    user_id = getattr(current_user, "id", None) or current_user["id"]
    if getattr(data, "user_id", user_id) != user_id:
        raise HTTPException(status_code=403, detail="Unauthorized")

    # Prefer extended CRUD signature if supported
    try:
        return withdraw_crud.create_withdraw_request(db, user_id, data, idempotency_key=idempotency_key)  # type: ignore[misc]
    except TypeError:
        return withdraw_crud.create_withdraw_request(db, user_id, data)

# ---------- User: my requests (lightweight) ----------
@router.get("/withdraw/mine", response_model=List[withdraw_schemas.WithdrawRequestOut])
def view_my_requests(
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    user_id = getattr(current_user, "id", None) or current_user["id"]
    # Prefer CRUD helper if it accepts user id directly
    if hasattr(withdraw_crud, "get_requests_by_user"):
        return withdraw_crud.get_requests_by_user(db, user_id)
    # Fallback: if you only have list-all, filter here (rare)
    if hasattr(withdraw_crud, "get_all_requests"):
        return [r for r in withdraw_crud.get_all_requests(db) if getattr(r, "user_id", None) == user_id]
    raise HTTPException(status_code=501, detail="Listing user requests not supported by withdraw_crud")

# ---------- User: my requests (cursor pagination + filter) ----------
@router.get("/withdraw/mine/page", response_model=RequestPageOut)
def page_my_requests(
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
    cursor_id: Optional[int] = Query(None, description="Paginate backward: id < cursor_id"),
    limit: int = Query(30, ge=1, le=100),
    status_eq: Optional[str] = Query(None, description="pending|approved|rejected|paid|cancelled"),
):
    user_id = getattr(current_user, "id", None) or current_user["id"]
    if hasattr(withdraw_crud, "list_requests_by_user"):
        result = withdraw_crud.list_requests_by_user(
            db, user_id=user_id, cursor_id=cursor_id, limit=limit, status_eq=status_eq
        )
        items = result.get("items", [])
        next_cursor = result.get("next_cursor")
        return RequestPageOut(meta=PageMeta(next_cursor=next_cursor, count=len(items)), items=items)

    # Fallback: slice from full list
    rows = withdraw_crud.get_requests_by_user(db, user_id)
    if status_eq:
        rows = [r for r in rows if getattr(r, "status", None) == status_eq]
    if cursor_id:
        rows = [r for r in rows if getattr(r, "id", 0) < cursor_id]
    items = rows[:limit]
    next_cursor = getattr(items[-1], "id", None) if items else None
    return RequestPageOut(meta=PageMeta(next_cursor=next_cursor, count=len(items)), items=items)

# ---------- User: cancel (new) ----------
@router.post("/withdraw/{request_id}/cancel", response_model=withdraw_schemas.WithdrawRequestOut)
def cancel_withdrawal(
    request_id: int,
    body: CancelIn,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    user_id = getattr(current_user, "id", None) or current_user["id"]
    if hasattr(withdraw_crud, "cancel_request"):
        req = withdraw_crud.cancel_request(db, request_id=request_id, user_id=user_id, reason=body.reason)
        if not req:
            raise HTTPException(status_code=404, detail="Request not found or cannot be cancelled")
        return req
    raise HTTPException(status_code=501, detail="Cancel not supported by withdraw_crud")

# ---------- Detail (new) ----------
@router.get("/withdraw/{request_id}", response_model=withdraw_schemas.WithdrawRequestOut)
def get_withdraw_request(
    request_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if hasattr(withdraw_crud, "get_request"):
        req = withdraw_crud.get_request(db, request_id=request_id, viewer_id=(getattr(current_user, "id", None) or current_user["id"]))
        if not req:
            raise HTTPException(status_code=404, detail="Request not found")
        return req
    raise HTTPException(status_code=501, detail="Get one not supported by withdraw_crud")

# ---------- Admin: list pending (kept path) ----------
@router.get("/withdrawals/pending", response_model=List[withdraw_schemas.WithdrawRequestOut])
def list_pending_withdrawals(
    db: Session = Depends(get_db),
    admin=Depends(get_admin_user),
):
    return withdraw_crud.get_pending_withdrawals(db)

# ---------- Admin: list (cursor pagination + filters) (new) ----------
@router.get("/withdrawals/admin/page", response_model=RequestPageOut)
def admin_page_requests(
    db: Session = Depends(get_db),
    admin=Depends(get_admin_user),
    cursor_id: Optional[int] = Query(None, description="Paginate backward: id < cursor_id"),
    limit: int = Query(30, ge=1, le=100),
    status_eq: Optional[str] = Query(None, description="pending|approved|rejected|paid|cancelled"),
    user_id: Optional[int] = Query(None, ge=1),
):
    if hasattr(withdraw_crud, "list_requests"):
        result = withdraw_crud.list_requests(
            db, cursor_id=cursor_id, limit=limit, status_eq=status_eq, user_id=user_id
        )
        items = result.get("items", [])
        next_cursor = result.get("next_cursor")
        return RequestPageOut(meta=PageMeta(next_cursor=next_cursor, count=len(items)), items=items)

    # Fallback via pending + approve/reject lists if you only expose partial CRUD:
    if hasattr(withdraw_crud, "get_all_requests"):
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

    raise HTTPException(status_code=501, detail="Admin listing not supported by withdraw_crud")

# ---------- Admin: approve (kept path) ----------
@router.post("/withdrawals/{request_id}/approve", response_model=withdraw_schemas.WithdrawRequestOut)
def approve_withdrawal(
    request_id: int,
    db: Session = Depends(get_db),
    admin=Depends(get_admin_user),
    if_match: Optional[str] = Header(None, convert_underscores=False),
    idempotency_key: Optional[str] = Header(None, convert_underscores=False),
):
    try:
        return withdraw_crud.approve_withdrawal(  # type: ignore[misc]
            db, request_id, if_match=if_match, idempotency_key=idempotency_key
        )
    except TypeError:
        return withdraw_crud.approve_withdrawal(db, request_id)

# ---------- Admin: reject (kept path) ----------
@router.post("/withdrawals/{request_id}/reject", response_model=withdraw_schemas.WithdrawRequestOut)
def reject_withdrawal(
    request_id: int,
    db: Session = Depends(get_db),
    admin=Depends(get_admin_user),
    reason: Optional[str] = Query(None, max_length=200),
    if_match: Optional[str] = Header(None, convert_underscores=False),
    idempotency_key: Optional[str] = Header(None, convert_underscores=False),
):
    try:
        return withdraw_crud.reject_withdrawal(  # type: ignore[misc]
            db, request_id, reason=reason, if_match=if_match, idempotency_key=idempotency_key
        )
    except TypeError:
        return withdraw_crud.reject_withdrawal(db, request_id)

# ---------- Admin: mark as paid (new) ----------
@router.post("/withdrawals/{request_id}/mark-paid", response_model=withdraw_schemas.WithdrawRequestOut)
def mark_paid(
    request_id: int,
    body: MarkPaidIn,
    db: Session = Depends(get_db),
    admin=Depends(get_admin_user),
    if_match: Optional[str] = Header(None, convert_underscores=False),
):
    if hasattr(withdraw_crud, "mark_paid"):
        req = withdraw_crud.mark_paid(
            db,
            request_id=request_id,
            payout_reference=body.payout_reference,
            note=body.note,
            if_match=if_match,
        )
        if not req:
            raise HTTPException(status_code=404, detail="Request not found or cannot be marked as paid")
        return req
    raise HTTPException(status_code=501, detail="mark_paid not supported by withdraw_crud")

