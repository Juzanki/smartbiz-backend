from __future__ import annotations
# backend/routes/billing.py
import hashlib
from datetime import datetime, timezone
from enum import Enum
from typing import Optional, Dict, Any
from contextlib import suppress

from fastapi import (
    APIRouter, Depends, HTTPException, status, Header, Response
)
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from backend.db import get_db
from backend.auth import get_current_user

# ====== Models (fail if missing so ujue kutengeneza) ======
try:
    from backend.models.api_key import APIKey  # API key + plan field
except Exception:
    raise RuntimeError("Model 'APIKey' haijapatikana. Tengeneza backend/models/api_key.py yenye user_id, plan, updated_at.")

try:
    from backend.models.user import User
except Exception:
    raise RuntimeError("Model 'User' haijapatikana. Tengeneza backend/models/user.py")

# ====== (Best-effort) Audit helper ======
def _audit(db: Session, **kw: Any) -> None:
    with suppress(Exception):
        from backend.routes.audit_log import emit_audit  # optional
        emit_audit(db, **kw)

router = APIRouter(prefix="/billing", tags=["Billing & Plans"])

# ====== Schemas ======
class PlanEnum(str, Enum):
    free = "free"
    pro = "pro"
    enterprise = "enterprise"

class PlanChangeRequest(BaseModel):
    plan: PlanEnum = Field(..., description="free | pro | enterprise")
    reason: Optional[str] = Field(None, max_length=200)

class PlanOut(BaseModel):
    user_id: int
    plan: PlanEnum
    previous_plan: Optional[PlanEnum] = None
    changed: bool
    updated_at: Optional[datetime] = None

# ====== Helpers ======
def _utcnow() -> datetime:
    return datetime.now(timezone.utc)

def _etag(api_key: APIKey) -> str:
    base = f"{api_key.user_id}-{api_key.plan}-{getattr(api_key, 'updated_at', '')}"
    return 'W/"' + hashlib.sha256(base.encode("utf-8")).hexdigest()[:16] + '"'

# ====== Get current plan ======
@router.get("/me/plan", response_model=PlanOut, summary="Pata mpango wa sasa (me)")
def get_my_plan(
    response: Response,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    rec = db.query(APIKey).filter(APIKey.user_id == current_user.id).first()
    if not rec:
        raise HTTPException(status_code=404, detail="No API key/plan record found")

    response.headers["Cache-Control"] = "no-store"
    response.headers["ETag"] = _etag(rec)

    return PlanOut(
        user_id=current_user.id,
        plan=PlanEnum(rec.plan),
        previous_plan=None,
        changed=False,
        updated_at=getattr(rec, "updated_at", None),
    )

# ====== Set/upgrade plan (idempotent, locked) ======
@router.post(
    "/me/upgrade-plan",
    response_model=PlanOut,
    summary="Badilisha (upgrade/downgrade) mpango wa sasa salama"
)
def upgrade_plan(
    payload: PlanChangeRequest,
    response: Response,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
    if_match: Optional[str] = Header(None, alias="If-Match"),  # hiari: optimistic
):
    # Pata rekodi kwa lock ya safu ili kuepuka race condition
    rec = (
        db.query(APIKey)
        .filter(APIKey.user_id == current_user.id)
        .with_for_update(of=APIKey)
        .first()
    )
    if not rec:
        raise HTTPException(status_code=404, detail="No API key found")

    # Idempotency (rahisi): kama plan inayoombwa = ya sasa → no-op
    if str(rec.plan) == payload.plan.value:
        response.headers["Cache-Control"] = "no-store"
        response.headers["ETag"] = _etag(rec)
        return PlanOut(
            user_id=current_user.id,
            plan=PlanEnum(rec.plan),
            previous_plan=PlanEnum(rec.plan),
            changed=False,
            updated_at=getattr(rec, "updated_at", None),
        )

    # (Hiari) optimistic check
    current_etag = _etag(rec)
    if if_match and if_match != current_etag:
        raise HTTPException(status_code=412, detail="ETag mismatch, record has changed")

    previous = PlanEnum(rec.plan)

    # (Mahali pa kuweka uthibitisho wa malipo kama pro/enterprise) ---------
    # Mfano:
    # if payload.plan in {PlanEnum.pro, PlanEnum.enterprise} and not has_active_subscription(current_user):
    #     raise HTTPException(status_code=402, detail="Payment required or subscription inactive")

    # Sasisha plan
    rec.plan = payload.plan.value
    if hasattr(rec, "updated_at"):
        rec.updated_at = _utcnow()
    db.commit()
    db.refresh(rec)

    # Headers za UX (mobile-friendly)
    response.headers["Cache-Control"] = "no-store"
    response.headers["ETag"] = _etag(rec)

    # Audit (best-effort)
    _audit(
        db,
        action="billing.plan.change",
        status="success",
        severity="info",
        actor_id=current_user.id,
        actor_email=getattr(current_user, "email", None),
        resource_type="plan",
        resource_id=str(current_user.id),
        meta={"from": previous.value, "to": payload.plan.value, "reason": payload.reason},
    )

    return PlanOut(
        user_id=current_user.id,
        plan=PlanEnum(rec.plan),
        previous_plan=previous,
        changed=True,
        updated_at=getattr(rec, "updated_at", None),
    )
