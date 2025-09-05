from __future__ import annotations
# backend/routes/admin_routes.py
from datetime import datetime
from typing import Optional, Iterable, List, Dict

from fastapi import (
    APIRouter, Depends, HTTPException, status, Query, Path, Response
)
from sqlalchemy.orm import Session
from sqlalchemy import or_, func

from backend.db import get_db
from backend.schemas import PaymentResponse  # endelea kutumia aggregator kwa payment
from backend.schemas.user import UserOut, UserUpdate  # import moja kwa moja ili kuepuka ImportError
from backend.models.payment import Payment
from backend.models.user import User as UserModel
from backend.dependencies import check_admin

# ---------------------------------- Router ---------------------------------- #
# Kuweka check_admin hapa hufanya kila route chini ya /admin kulindwa moja kwa moja
router = APIRouter(prefix="/admin", tags=["Admin"], dependencies=[Depends(check_admin)])

# --------------------------------- Helpers ---------------------------------- #
MAX_LIMIT = 200

def _clamp_limit(limit: Optional[int], default: int = 50) -> int:
    if not limit:
        return default
    return max(1, min(int(limit), MAX_LIMIT))

def _order_by_whitelist(model, sort_by: str, order: str, allow: Iterable[str]):
    """Whitelists sorting fields ili kuepuka SQL injection kwenye order_by."""
    key = sort_by if sort_by in allow else next(iter(allow))
    col = getattr(model, key)
    return col.asc() if order == "asc" else col.desc()

def _apply_pagination_headers(
    response: Response, *, total: Optional[int], limit: int, offset: int
) -> None:
    if total is not None:
        response.headers["X-Total-Count"] = str(total)
    response.headers["X-Limit"] = str(limit)
    response.headers["X-Offset"] = str(offset)

def _parse_date(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    # Ruhusu ISO 8601 au "YYYY-MM-DD"
    try:
        return datetime.fromisoformat(s)
    except Exception:
        try:
            return datetime.strptime(s, "%Y-%m-%d")
        except Exception:
            return None

# ========================== PAYMENTS: list & summary ========================= #
@router.get(
    "/payments",
    response_model=List[PaymentResponse],
    summary="🔐 Admin: List payments (filter, sort, paginate)",
)
def admin_get_all_payments(
    response: Response,
    db: Session = Depends(get_db),
    # Filters
    user_id: Optional[int] = Query(None),
    status_f: Optional[str] = Query(None, alias="status"),
    method: Optional[str] = Query(None, description="payment method / provider"),
    q: Optional[str] = Query(None, description="search ref/txid/notes"),
    created_from: Optional[str] = Query(None),
    created_to: Optional[str] = Query(None),
    # Sort & paginate
    sort_by: str = Query("created_at", pattern="^(id|amount|created_at)$"),
    order: str = Query("desc", pattern="^(asc|desc)$"),
    limit: int = Query(50, ge=1, le=MAX_LIMIT),
    offset: int = Query(0, ge=0),
    # Count toggle (avoid heavy COUNT(*) unless requested)
    with_count: bool = Query(False, description="include X-Total-Count header"),
):
    limit = _clamp_limit(limit)
    qy = db.query(Payment)

    # Filters
    if user_id:
        qy = qy.filter(Payment.user_id == user_id)
    if status_f:
        qy = qy.filter(Payment.status == status_f)
    if method:
        # adjust field name if your model uses provider/gateway
        if hasattr(Payment, "method"):
            qy = qy.filter(Payment.method == method)
        elif hasattr(Payment, "provider"):
            qy = qy.filter(Payment.provider == method)
    if q:
        like = f"%{q.strip()}%"
        conds = []
        for field in ("reference", "txid", "notes", "external_id"):
            if hasattr(Payment, field):
                conds.append(getattr(Payment, field).ilike(like))
        if conds:
            qy = qy.filter(or_(*conds))

    dt_from = _parse_date(created_from)
    dt_to   = _parse_date(created_to)
    if dt_from:
        qy = qy.filter(Payment.created_at >= dt_from)
    if dt_to:
        qy = qy.filter(Payment.created_at <= dt_to)

    # Sorting
    qy = qy.order_by(_order_by_whitelist(Payment, sort_by, order, ("created_at", "amount", "id")))

    total = None
    if with_count:
        total = qy.with_entities(func.count(Payment.id)).scalar() or 0

    # Pagination
    items = qy.offset(offset).limit(limit).all()
    _apply_pagination_headers(response, total=total, limit=limit, offset=offset)
    return items


@router.get(
    "/payments/summary",
    summary="🔐 Admin: Payments summary (counts & sums by status)",
)
def admin_payments_summary(
    db: Session = Depends(get_db),
    created_from: Optional[str] = Query(None),
    created_to: Optional[str] = Query(None),
):
    qy = db.query(Payment)
    dt_from = _parse_date(created_from)
    dt_to   = _parse_date(created_to)
    if dt_from:
        qy = qy.filter(Payment.created_at >= dt_from)
    if dt_to:
        qy = qy.filter(Payment.created_at <= dt_to)

    # Aggregate by status (adjust statuses to your domain if needed)
    rows = (
        qy.with_entities(Payment.status, func.count(Payment.id), func.coalesce(func.sum(Payment.amount), 0))
          .group_by(Payment.status)
          .all()
    )
    return [
        {"status": s or "unknown", "count": int(c or 0), "amount_sum": float(a or 0.0)}
        for (s, c, a) in rows
    ]


# ================================ USERS (CRUD) =============================== #
@router.get(
    "/users",
    response_model=List[UserOut],
    summary="🔐 Admin: List users (filter, sort, paginate)",
)
def admin_view_users(
    response: Response,
    db: Session = Depends(get_db),
    # Filters
    q: Optional[str] = Query(None, description="search username/email/phone"),
    plan: Optional[str] = Query(None, alias="subscription_status"),
    # Dates
    created_from: Optional[str] = Query(None),
    created_to: Optional[str] = Query(None),
    # Sort & paginate
    sort_by: str = Query("created_at", pattern="^(id|username|email|created_at)$"),
    order: str = Query("desc", pattern="^(asc|desc)$"),
    limit: int = Query(50, ge=1, le=MAX_LIMIT),
    offset: int = Query(0, ge=0),
    with_count: bool = Query(False),
):
    limit = _clamp_limit(limit)
    qy = db.query(UserModel)

    if q:
        like = f"%{q.strip()}%"
        conds = []
        for field in ("username", "email", "phone_number", "full_name", "business_name"):
            if hasattr(UserModel, field):
                conds.append(getattr(UserModel, field).ilike(like))
        if conds:
            qy = qy.filter(or_(*conds))

    if plan and hasattr(UserModel, "subscription_status"):
        qy = qy.filter(UserModel.subscription_status == plan)

    dt_from = _parse_date(created_from)
    dt_to   = _parse_date(created_to)
    if dt_from:
        qy = qy.filter(UserModel.created_at >= dt_from)
    if dt_to:
        qy = qy.filter(UserModel.created_at <= dt_to)

    qy = qy.order_by(_order_by_whitelist(UserModel, sort_by, order, ("created_at", "id", "username", "email")))

    total = None
    if with_count:
        total = qy.with_entities(func.count(UserModel.id)).scalar() or 0

    items = qy.offset(offset).limit(limit).all()
    _apply_pagination_headers(response, total=total, limit=limit, offset=offset)
    return items


@router.get(
    "/users/{user_id}",
    response_model=UserOut,
    summary="🔐 Admin: Get single user",
)
def admin_get_user(
    user_id: int = Path(..., ge=1),
    db: Session = Depends(get_db),
):
    user = db.query(UserModel).filter(UserModel.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return user


@router.patch(
    "/users/{user_id}",
    response_model=UserOut,
    summary="🔐 Admin: Update user (partial)",
)
def admin_update_user(
    user_id: int = Path(..., ge=1),
    data: UserUpdate = ...,
    db: Session = Depends(get_db),
):
    user = db.query(UserModel).filter(UserModel.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    changes: Dict = (
        data.model_dump(exclude_unset=True)
        if hasattr(data, "model_dump") else
        data.dict(exclude_unset=True)  # Pydantic v1 fallback
    )

    # Mfano: unaweza kuzuia baadhi ya fields hapa
    # for banned in ("password", "hashed_password"):
    #     changes.pop(banned, None)

    for key, value in changes.items():
        if hasattr(user, key):
            setattr(user, key, value)

    try:
        db.commit()
        db.refresh(user)
    except Exception:
        db.rollback()
        raise HTTPException(status_code=500, detail="Failed to update user")
    return user


@router.delete(
    "/users/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="🔐 Admin: Delete user",
)
def admin_delete_user(
    user_id: int = Path(..., ge=1),
    db: Session = Depends(get_db),
):
    user = db.query(UserModel).filter(UserModel.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    try:
        db.delete(user)
        db.commit()
    except Exception:
        db.rollback()
        raise HTTPException(status_code=500, detail="Failed to delete user")
    return None  # 204

