# backend/routes/subscriptions.py
# -*- coding: utf-8 -*-
from __future__ import annotations
"""
Subscriptions API (mobile-first, international-ready)

Endpoints
- GET    /subscriptions/plans                    -> list all plans
- GET    /subscriptions/plans/{plan}             -> get a single plan by name/slug
- GET    /subscriptions/status                   -> current user's subscription status
- POST   /subscriptions/purchase                 -> purchase/renew/upgrade (replace or extend)
- POST   /subscriptions/cancel                   -> cancel (set to free)

Features
- Clean, English-only code & docs
- Small mobile payloads, clear response models
- Case-insensitive plan lookup; supports names or slugs (e.g., "pro", "Pro")
- Concurrency-safe user row locking to prevent race conditions
- UTC, ISO8601 timestamps for robust clients
- Optional: carry over remaining days when upgrading (carryover=true)
- Optional: idempotency_key support if your User model has last_purchase_key
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import List, Optional, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, conint
from sqlalchemy.orm import Session

from backend.db import get_db
from backend.auth import get_current_user
from backend.models.user import User
router = APIRouter(prefix="/subscriptions", tags=["Subscriptions"])

# ==================== SCHEMAS ====================

class PlanOut(BaseModel):
    id: int
    name: str
    slug: str
    price: conint(ge=0)  # in minor currency units or whole currency (TZS is 0-decimal)
    currency: str = Field(default="TZS")
    duration_days: conint(ge=1)
    features: List[str]
    recommended: bool = False


class PurchaseRequest(BaseModel):
    plan: str = Field(..., description="Plan name or slug (e.g., 'Pro' or 'pro')")
    idempotency_key: Optional[str] = Field(
        None,
        description="Optional key to prevent duplicate purchases (if supported by User model)",
    )


class PurchaseResponse(BaseModel):
    message: str
    plan: str
    expires_at: str  # ISO8601 UTC string
    mode: Literal["replace", "extend"]
    carried_over_days: int = 0


class StatusOut(BaseModel):
    active: bool
    plan: Optional[str] = None
    expires_at: Optional[str] = None  # ISO8601 UTC
    days_left: int = 0


class CancelResponse(BaseModel):
    message: str
    previous_plan: Optional[str] = None


# ==================== STATIC PLAN DATA ====================
# Note: TZS has no fractional minor units; `price` is in TZS.
_PLANS: List[PlanOut] = [
    PlanOut(
        id=1,
        name="Pro",
        slug="pro",
        price=30000,
        duration_days=30,
        features=[
            "AI Bot Access",
            "Smart Scheduling",
            "Premium Support",
        ],
        recommended=True,
    ),
    PlanOut(
        id=2,
        name="Business",
        slug="business",
        price=65000,
        duration_days=30,
        features=[
            "Everything in Pro",
            "Team Access",
            "Advanced Insights",
        ],
    ),
    PlanOut(
        id=3,
        name="Enterprise",
        slug="enterprise",
        price=125000,
        duration_days=30,
        features=[
            "Unlimited Access",
            "White-labeling",
            "Enterprise Integrations",
        ],
    ),
]

_PLANS_BY_SLUG = {p.slug.casefold(): p for p in _PLANS}
_PLANS_BY_NAME = {p.name.casefold(): p for p in _PLANS}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _find_plan(plan_key: str) -> PlanOut | None:
    k = (plan_key or "").strip().casefold()
    return _PLANS_BY_SLUG.get(k) or _PLANS_BY_NAME.get(k)


# ==================== ROUTES ====================

@router.get("/plans", response_model=List[PlanOut], summary="View all subscription plans")
def get_plans() -> List[PlanOut]:
    # Keep order stable: recommended first, then by price asc
    return sorted(_PLANS, key=lambda p: (not p.recommended, p.price))


@router.get("/plans/{plan}", response_model=PlanOut, summary="Get a single subscription plan")
def get_plan(plan: str) -> PlanOut:
    found = _find_plan(plan)
    if not found:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Plan not found")
    return found


@router.get("/status", response_model=StatusOut, summary="Get current user subscription status")
def get_status(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> StatusOut:
    # Merge the user instance into the current session
    user = db.merge(current_user)
    plan = getattr(user, "subscription_status", None)
    expiry = getattr(user, "subscription_expiry", None)

    now = _utcnow()
    active = bool(plan and expiry and expiry > now)
    days_left = max(0, (expiry - now).days) if active else 0

    return StatusOut(
        active=active,
        plan=plan if active else None,
        expires_at=expiry.astimezone(timezone.utc).isoformat() if isinstance(expiry, datetime) else None,
        days_left=days_left,
    )


@router.post(
    "/purchase",
    response_model=PurchaseResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Purchase, renew, or upgrade a plan",
)
def purchase_plan(
    payload: PurchaseRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    mode: Literal["replace", "extend"] = Query(
        "replace",
        description=(
            "replace = start a fresh period from now; "
            "extend = add duration to current expiry if same plan and still active"
        ),
    ),
    carryover: bool = Query(
        False,
        description="If true, carry over remaining days when switching plans (upgrade)",
    ),
):
    """
    Concurrency-safe purchase flow:
    - Re-fetch & lock the user row to avoid race conditions
    - Case-insensitive plan lookup (name or slug)
    - `extend` only applies when renewing the same active plan
    - `carryover` adds remaining days from the previous plan when switching (optional)
    - `idempotency_key` is supported if your User model has `last_purchase_key` column
    """
    plan = _find_plan(payload.plan)
    if not plan:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Plan not found")

    # Lock the user row for the transaction
    user: User | None = (
        db.query(User)
        .filter(User.id == current_user.id)
        .with_for_update()
        .one_or_none()
    )
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    # Optional idempotency support
    if payload.idempotency_key and hasattr(user, "last_purchase_key"):
        if getattr(user, "last_purchase_key") == payload.idempotency_key:
            # Return current status; treat as idempotent success
            expiry = getattr(user, "subscription_expiry", None)
            return PurchaseResponse(
                message=f"Already processed for {plan.name}",
                plan=getattr(user, "subscription_status", plan.name) or plan.name,
                expires_at=expiry.astimezone(timezone.utc).isoformat() if isinstance(expiry, datetime) else _utcnow().isoformat(),
                mode=mode,
                carried_over_days=0,
            )

    now = _utcnow()
    current_plan = getattr(user, "subscription_status", None)
    current_expiry: Optional[datetime] = getattr(user, "subscription_expiry", None)
    active = bool(current_plan and current_expiry and current_expiry > now)

    carried_over_days = 0
    new_expiry: datetime

    if mode == "extend" and active and (current_plan or "").casefold() == plan.name.casefold():
        # Extend the same active plan
        new_expiry = current_expiry + timedelta(days=plan.duration_days)  # type: ignore[operator]
    else:
        # Replace flow: start from now
        new_expiry = now + timedelta(days=plan.duration_days)
        # Optionally carry over remaining days if switching plans mid-cycle
        if carryover and active and current_expiry:
            leftover = (current_expiry - now).days
            if leftover > 0:
                new_expiry += timedelta(days=leftover)
                carried_over_days = leftover

    # Apply changes
    user.subscription_status = plan.name
    user.subscription_expiry = new_expiry
    if payload.idempotency_key and hasattr(user, "last_purchase_key"):
        user.last_purchase_key = payload.idempotency_key  # type: ignore[attr-defined]

    db.commit()
    db.refresh(user)

    return PurchaseResponse(
        message=f"âœ… Successfully subscribed to {plan.name}",
        plan=plan.name,
        expires_at=new_expiry.astimezone(timezone.utc).isoformat(),
        mode=mode,
        carried_over_days=carried_over_days,
    )


@router.post(
    "/cancel",
    response_model=CancelResponse,
    summary="Cancel current subscription (ends immediately)",
)
def cancel_subscription(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    # Lock the user row to avoid concurrent modifications
    user: User | None = (
        db.query(User)
        .filter(User.id == current_user.id)
        .with_for_update()
        .one_or_none()
    )
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    prev = getattr(user, "subscription_status", None)
    user.subscription_status = None
    user.subscription_expiry = None
    db.commit()
    db.refresh(user)

    return CancelResponse(message="Subscription cancelled", previous_plan=prev)

