# -*- coding: utf-8 -*-
from __future__ import annotations
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Header, Query, status
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError
from sqlalchemy import and_

from pydantic import BaseModel, Field

from backend.db import get_db
from backend.auth import get_current_user
from backend.models.user import User
from backend.schemas.goal import GoalCreate, GoalOut
from backend.crud import goal_crud

# Optional: ikiwa una access ya model
try:
    from backend.models.goal import Goal  # type: ignore
except Exception:
    Goal = None  # type: ignore

router = APIRouter(prefix="/goals", tags=["Goals"])

# ===== Helpers =====
def _is_admin(user: User) -> bool:
    return getattr(user, "role", None) in {"admin", "owner"}

def _force_owner_on_create(payload: GoalCreate, user_id: int) -> GoalCreate:
    """Sukuma user_id/owner_id kwenye payload kama column ipo kwenye schema/model."""
    updates = {}
    for k in ("user_id", "owner_id", "host_id"):
        if hasattr(payload, k) or (Goal and hasattr(Goal, k)):
            updates[k] = user_id
    if not updates:
        return payload
    try:
        # Pydantic v2
        return payload.model_copy(update=updates)  # type: ignore[attr-defined]
    except AttributeError:
        # Pydantic v1
        return payload.copy(update=updates)  # type: ignore

def _ownership_filter(q, user: User):
    """Ikiwa model ina user_id/owner_id, fanya filter kwa goals za mtumiaji huyu."""
    if not Goal:
        return q
    if hasattr(Goal, "user_id"):
        return q.filter(Goal.user_id == user.id)
    if hasattr(Goal, "owner_id"):
        return q.filter(Goal.owner_id == user.id)
    return q

def _check_can_modify(db: Session, goal_id: int, current_user: User):
    """Admin/owner ok; vinginevyo lazima goal iwe yake (kama columns zipo)."""
    if _is_admin(current_user) or not Goal:
        return
    g = db.query(Goal).get(goal_id)  # type: ignore
    if not g:
        raise HTTPException(status_code=404, detail="Goal not found")
    if hasattr(g, "user_id") and g.user_id == current_user.id:
        return
    if hasattr(g, "owner_id") and g.owner_id == current_user.id:
        return
    raise HTTPException(status_code=403, detail="Not allowed to modify this goal")

# ===== Schemas for updates & pages =====
class GoalProgressUpdate(BaseModel):
    amount: float = Field(..., gt=0, description="Amount to apply")
    mode: str = Field("increment", pattern="^(increment|set)$")

class PageMeta(BaseModel):
    total: int
    limit: int
    offset: int

class GoalPage(BaseModel):
    items: List[GoalOut]
    meta: PageMeta

# ====== Create ======
@router.post("/", response_model=GoalOut, status_code=status.HTTP_201_CREATED)
def create_goal(
    goal: GoalCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
):
    """
    Unda goal mpya.
    - Owner anaforce kutoka session kama schema/model ina `user_id/owner_id`.
    - Admin/owner wanaweza kuunda popote; user wa kawaida atawekewa goal yake.
    - Idempotency-Key (optional) kama umeiweka upande wa DB/CRUD.
    """
    payload = goal if _is_admin(current_user) else _force_owner_on_create(goal, current_user.id)
    try:
        try:
            return goal_crud.create_goal(db, payload, idempotency_key=idempotency_key)  # type: ignore
        except TypeError:
            return goal_crud.create_goal(db, payload)
    except IntegrityError as ie:
        db.rollback()
        raise HTTPException(status_code=409, detail="Duplicate goal") from ie
    except HTTPException:
        db.rollback()
        raise
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=500, detail="Failed to create goal") from exc

# ====== List (legacy: List[GoalOut]) ======
@router.get("/", response_model=List[GoalOut])
def list_goals(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    q: Optional[str] = Query(None, description="Search by title/name"),
    is_active: Optional[bool] = Query(None),
    stream_id: Optional[int] = Query(None),
    mine: bool = Query(False, description="Rudisha goals zangu tu"),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    order: str = Query("created_at"),
    sort: str = Query("desc", pattern="^(asc|desc)$"),
):
    """
    Orodha ya goals (legacy). Ina filters + pagination lakini haina meta.
    - Ikiwa `mine=true` na model ina owner/user_id, zitarudishwa za current_user tu.
    """
    # Kama hatuna model, rudi kwenye CRUD yako ya zamani
    if not Goal:
        return goal_crud.get_all_goals(db)

    qset = db.query(Goal)
    if mine:
        qset = _ownership_filter(qset, current_user)

    conds = []
    if q and hasattr(Goal, "title"):
        like = f"%{q.strip()}%"
        conds.append(Goal.title.ilike(like))
    if is_active is not None and hasattr(Goal, "is_active"):
        conds.append(Goal.is_active == is_active)
    if stream_id is not None and hasattr(Goal, "stream_id"):
        conds.append(Goal.stream_id == stream_id)
    if conds:
        qset = qset.filter(and_(*conds))

    # ordering map
    colmap = {
        "created_at": getattr(Goal, "created_at", None),
        "updated_at": getattr(Goal, "updated_at", None),
        "current_amount": getattr(Goal, "current_amount", None),
        "target_amount": getattr(Goal, "target_amount", None),
        "title": getattr(Goal, "title", None),
    }
    col = colmap.get(order, getattr(Goal, "created_at", None))
    if col is not None:
        qset = qset.order_by(col.asc() if sort == "asc" else col.desc())

    return qset.offset(offset).limit(limit).all()

# ====== Page (items + meta) ======
@router.get("/page", response_model=GoalPage)
def list_goals_page(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    q: Optional[str] = Query(None),
    is_active: Optional[bool] = Query(None),
    stream_id: Optional[int] = Query(None),
    mine: bool = Query(False),
    limit: int = Query(30, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    if not Goal:
        # fallback simple: tumia CRUD + kata kwa mkono (si efficient lakini backward-compatible)
        items = goal_crud.get_all_goals(db)
        total = len(items)
        return GoalPage(items=items[offset:offset+limit], meta=PageMeta(total=total, limit=limit, offset=offset))

    qset = db.query(Goal)
    if mine:
        qset = _ownership_filter(qset, current_user)
    if q and hasattr(Goal, "title"):
        qset = qset.filter(Goal.title.ilike(f"%{q.strip()}%"))
    if is_active is not None and hasattr(Goal, "is_active"):
        qset = qset.filter(Goal.is_active == is_active)
    if stream_id is not None and hasattr(Goal, "stream_id"):
        qset = qset.filter(Goal.stream_id == stream_id)

    total = qset.count()
    items = qset.order_by(
        getattr(Goal, "created_at", None).desc() if hasattr(Goal, "created_at") else None
    ).offset(offset).limit(limit).all()
    return GoalPage(items=items, meta=PageMeta(total=total, limit=limit, offset=offset))

# ====== Update progress (atomic) ======
@router.put("/{goal_id}/update", response_model=GoalOut)
def update_goal(
    goal_id: int,
    amount: Optional[float] = Query(None, ge=0, description="(Legacy) amount to apply"),
    body: Optional[GoalProgressUpdate] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
):
    """
    Sasisha progress ya goal:
    - New: tumia body `{"amount": 100, "mode": "increment"|"set"}`
    - Legacy: bado inakubali `?amount=100` kama `increment`.
    - Ownership/Admin checks.
    - Idempotency-Key (optional) kupunguza double taps.
    """
    _check_can_modify(db, goal_id, current_user)

    # Chukua inputs
    mode = "increment"
    value: Optional[float] = None
    if body:
        value = body.amount
        mode = body.mode
    elif amount is not None:
        value = amount
        mode = "increment"

    if value is None or value < 0:
        raise HTTPException(status_code=422, detail="Amount is required and must be >= 0")

    # Jaribu kutumia CRUD yako ya sasa
    try:
        try:
            # Ikiwa umeongeza signature mpya upande wa CRUD
            updated = goal_crud.update_goal_progress(
                db, goal_id, value, mode=mode, idempotency_key=idempotency_key  # type: ignore
            )
        except TypeError:
            # Back-compat: CRUD ya awali (labda ilikuwa increment tu)
            if mode != "increment":
                raise HTTPException(status_code=400, detail="This server supports only increment in legacy mode")
            updated = goal_crud.update_goal_progress(db, goal_id, value)
    except HTTPException:
        db.rollback()
        raise
    except IntegrityError as ie:
        db.rollback()
        raise HTTPException(status_code=409, detail="Duplicate request (idempotency)") from ie
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=500, detail="Failed to update goal") from exc

    if not updated:
        raise HTTPException(status_code=404, detail="Goal not found")
    return updated

# ====== Delete ======
@router.delete("/{goal_id}", status_code=status.HTTP_200_OK)
def delete_goal(
    goal_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _check_can_modify(db, goal_id, current_user)
    try:
        deleted = goal_crud.delete_goal(db, goal_id)
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=500, detail="Failed to delete goal") from exc

    if not deleted:
        raise HTTPException(status_code=404, detail="Goal not found")
    return {"detail": "Goal deleted"}
