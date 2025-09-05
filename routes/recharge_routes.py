from __future__ import annotations
# backend/routes/recharge.py
import hmac
import os
import hashlib
from typing import Optional, List, Any
from contextlib import suppress
from datetime import datetime, timezone

from fastapi import (
    APIRouter, Depends, HTTPException, status, Header, Response, Query, Body
)
from sqlalchemy.orm import Session
from sqlalchemy import or_

from backend.db import get_db
from backend.dependencies import get_current_user, get_admin_user

# ===== Schemas =====
with suppress(Exception):
    from backend.schemas.recharge_schemas import (
        RechargeCreate, RechargeOut, RechargeStatusOut, RechargeFilter, RechargeUpdate
    )

# Fallback ndogo (kama schema haipo bado)
if "RechargeCreate" not in globals():
    from pydantic import BaseModel, condecimal
    class RechargeCreate(BaseModel):
        amount: condecimal(gt=0)  # type: ignore
        currency: str = "TZS"
        provider: str = "pesapal"
        meta: Optional[dict] = None

    class RechargeOut(RechargeCreate):
        reference: str
        status: str = "pending"
        created_at: Optional[datetime] = None
        completed_at: Optional[datetime] = None

        class Config:
            orm_mode = True
        model_config = {"from_attributes": True}

    class RechargeStatusOut(RechargeOut): ...
    class RechargeUpdate(BaseModel):
        status: Optional[str] = None
        external_txn_id: Optional[str] = None
        meta: Optional[dict] = None

# ===== Models / CRUD =====
RechargeModel = None
with suppress(Exception):
    from backend.models.recharge_transaction import RechargeTransaction as RechargeModel
with suppress(Exception):
    from backend.crud import recharge_crud as _crud

CRUD_CREATE   = getattr(_crud, "create_recharge", None) if "_crud" in globals() else None
CRUD_COMPLETE = getattr(_crud, "complete_recharge", None) if "_crud" in globals() else None
CRUD_GET_BY_REF = getattr(_crud, "get_by_reference", None) if "_crud" in globals() else None
CRUD_LIST = getattr(_crud, "list_recharges", None) if "_crud" in globals() else None
CRUD_UPSERT_IDEMP = getattr(_crud, "create_or_get_by_idempotency", None) if "_crud" in globals() else None
CRUD_UPDATE = getattr(_crud, "update_recharge", None) if "_crud" in globals() else None

router = APIRouter(prefix="/recharge", tags=["Wallet Recharge"])

# ===== Helpers =====
def _utc() -> datetime:
    return datetime.now(timezone.utc)

def _etag_of(rows: List[Any]) -> str:
    if not rows:
        return 'W/"empty"'
    last = max(
        getattr(r, "updated_at", None)
        or getattr(r, "completed_at", None)
        or getattr(r, "created_at", None)
        or datetime.min
        for r in rows
    )
    seed = ",".join(str(getattr(r, "id", getattr(r, "reference", ""))) for r in rows[:200]) + "|" + last.isoformat()
    return 'W/"' + hashlib.sha256(seed.encode()).hexdigest()[:16] + '"'

def _serialize(obj: Any):
    if hasattr(RechargeOut, "model_validate"):
        return RechargeOut.model_validate(obj, from_attributes=True)
    return RechargeOut.model_validate(obj)

# ------------------------------------------------------------------------------
# ðŸ” INITIATE (idempotent via Idempotency-Key)
# ------------------------------------------------------------------------------
@router.post(
    "",
    response_model=RechargeOut,
    status_code=status.HTTP_201_CREATED,
    summary="Anzisha recharge (idempotent)"
)
def initiate_recharge(
    data: RechargeCreate,
    response: Response,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
):
    """
    - Tumia `Idempotency-Key` (header) kuzuia duplicate charges.
    - Inarudisha transaction ile ile ukiomba tena na key sawa.
    """
    # Tumia CRUD yako ikiwa inasaidia idempotency
    if CRUD_UPSERT_IDEMP:
        row = CRUD_UPSERT_IDEMP(db, current_user.id, data, idempotency_key)
        response.headers["Cache-Control"] = "no-store"
        return _serialize(row)

    # Fallback rahisi
    if not RechargeModel:
        raise HTTPException(status_code=500, detail="Recharge storage not configured")

    if idempotency_key and hasattr(RechargeModel, "idempotency_key"):
        existing = (
            db.query(RechargeModel)
            .filter(RechargeModel.user_id == current_user.id,
                    RechargeModel.idempotency_key == idempotency_key)
            .first()
        )
        if existing:
            response.headers["Cache-Control"] = "no-store"
            return _serialize(existing)

    row = RechargeModel(
        user_id=current_user.id,
        amount=getattr(data, "amount", None),
        currency=getattr(data, "currency", "TZS"),
        provider=getattr(data, "provider", "pesapal"),
        status="pending",
        meta=getattr(data, "meta", None),
        created_at=_utc(),
    )
    if idempotency_key and hasattr(row, "idempotency_key"):
        row.idempotency_key = idempotency_key

    db.add(row)
    db.commit()
    db.refresh(row)
    response.headers["Cache-Control"] = "no-store"
    return _serialize(row)

# ------------------------------------------------------------------------------
# ðŸ”Ž GET STATUS (mobile-first: ETag/304)
# ------------------------------------------------------------------------------
@router.get(
    "/{reference}",
    response_model=RechargeStatusOut,
    summary="Status ya recharge kwa reference"
)
def get_recharge_status(
    reference: str,
    response: Response,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
    if_none_match: Optional[str] = Header(None, alias="If-None-Match"),
):
    row = None
    if CRUD_GET_BY_REF:
        row = CRUD_GET_BY_REF(db, reference)
    else:
        if not RechargeModel:
            raise HTTPException(status_code=500, detail="Recharge storage not configured")
        row = db.query(RechargeModel).filter(RechargeModel.reference == reference).first()

    if not row or getattr(row, "user_id", None) != current_user.id:
        raise HTTPException(status_code=404, detail="Transaction not found")

    etag = _etag_of([row])
    if if_none_match and if_none_match == etag:
        return Response(status_code=status.HTTP_304_NOT_MODIFIED)
    response.headers["ETag"] = etag
    response.headers["Cache-Control"] = "public, max-age=10"
    return _serialize(row)

# ------------------------------------------------------------------------------
# ðŸ“œ MY HISTORY (pagination + filters + ETag/304)
# ------------------------------------------------------------------------------
@router.get(
    "/me",
    response_model=List[RechargeOut],
    summary="Orodha ya recharges zangu (paged)"
)
def my_recharges(
    response: Response,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
    status_eq: Optional[str] = Query(None, description="pending|completed|failed"),
    provider: Optional[str] = Query(None),
    q: Optional[str] = Query(None, description="tafuta kwenye reference/external_txn_id"),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    if_none_match: Optional[str] = Header(None, alias="If-None-Match"),
):
    if CRUD_LIST and not RechargeModel:
        rows = CRUD_LIST(db, user_id=current_user.id) or []
        if status_eq: rows = [r for r in rows if getattr(r, "status", None) == status_eq]
        if provider:  rows = [r for r in rows if getattr(r, "provider", None) == provider]
        if q:
            ql = q.lower()
            rows = [r for r in rows if ql in str(getattr(r, "reference", "")).lower()
                                or ql in str(getattr(r, "external_txn_id", "")).lower()]
        total = len(rows)
        rows = rows[offset: offset + limit]
    else:
        if not RechargeModel:
            raise HTTPException(status_code=500, detail="Recharge storage not configured")
        qry = db.query(RechargeModel).filter(RechargeModel.user_id == current_user.id)
        if status_eq: qry = qry.filter(RechargeModel.status == status_eq)
        if provider:  qry = qry.filter(RechargeModel.provider == provider)
        if q:
            like = f"%{q}%"
            qry = qry.filter(or_(RechargeModel.reference.ilike(like),
                                 getattr(RechargeModel, "external_txn_id", "").ilike(like)
                                 if hasattr(RechargeModel, "external_txn_id") else False))
        qry = qry.order_by(getattr(RechargeModel, "updated_at",
                                   getattr(RechargeModel, "created_at")).desc())
        total = qry.count()
        rows = qry.offset(offset).limit(limit).all()

    etag = _etag_of(rows)
    if if_none_match and if_none_match == etag:
        return Response(status_code=status.HTTP_304_NOT_MODIFIED)
    response.headers["ETag"] = etag
    response.headers["Cache-Control"] = "public, max-age=10"
    response.headers["X-Total-Count"] = str(total)
    response.headers["X-Limit"] = str(limit)
    response.headers["X-Offset"] = str(offset)
    return [_serialize(r) for r in rows]

# ------------------------------------------------------------------------------
# âœ… ADMIN: COMPLETE (manual) â€” hukinga double-settle
# ------------------------------------------------------------------------------
@router.post(
    "/complete/{reference}",
    response_model=RechargeOut,
    summary="ðŸ” Admin: thibitisha/complete recharge kwa mkono"
)
def confirm_recharge_admin(
    reference: str,
    db: Session = Depends(get_db),
    admin = Depends(get_admin_user)
):
    # Tumia CRUD yako
    if CRUD_COMPLETE:
        row = CRUD_COMPLETE(db, reference)
        if not row:
            raise HTTPException(status_code=404, detail="Transaction not found or already completed")
        return _serialize(row)

    # Fallback
    if not RechargeModel:
        raise HTTPException(status_code=500, detail="Recharge storage not configured")
    row = db.query(RechargeModel).filter(RechargeModel.reference == reference).first()
    if not row:
        raise HTTPException(status_code=404, detail="Transaction not found")
    if getattr(row, "status", "") == "completed":
        raise HTTPException(status_code=409, detail="Already completed")

    row.status = "completed"
    if hasattr(row, "completed_at"):
        row.completed_at = _utc()
    if hasattr(row, "updated_at"):
        row.updated_at = _utc()
    db.commit()
    db.refresh(row)
    return _serialize(row)

# ------------------------------------------------------------------------------
# ðŸ”” WEBHOOK (HMAC) â€” provider callback (secure)
# ------------------------------------------------------------------------------
@router.post(
    "/webhook/{provider}",
    response_model=dict,
    summary="Webhook ya mtoa huduma (HMAC verified)"
)
def webhook_recharge(
    provider: str,
    payload: dict = Body(...),
    signature: Optional[str] = Header(None, alias="X-Signature"),
    db: Session = Depends(get_db),
):
    """
    Tumia env `RECHARGE_WEBHOOK_SECRET` kuverify `X-Signature` (HMAC-SHA256 ya body).
    payload inapaswa kuwa na angalau: `reference`, `status`, `external_txn_id` (ikijaa).
    """
    secret = os.getenv("RECHARGE_WEBHOOK_SECRET")
    if not secret:
        raise HTTPException(status_code=500, detail="Webhook secret not configured")

    body_bytes = (str(payload)).encode("utf-8")
    expected = hmac.new(secret.encode("utf-8"), body_bytes, hashlib.sha256).hexdigest()
    if not signature or not hmac.compare_digest(signature, expected):
        raise HTTPException(status_code=401, detail="Invalid webhook signature")

    reference = payload.get("reference")
    new_status = (payload.get("status") or "").lower()
    external_txn_id = payload.get("external_txn_id")

    if not reference or new_status not in {"completed", "failed"}:
        raise HTTPException(status_code=400, detail="Invalid payload")

    row = None
    if CRUD_GET_BY_REF:
        row = CRUD_GET_BY_REF(db, reference)
    else:
        if not RechargeModel:
            raise HTTPException(status_code=500, detail="Recharge storage not configured")
        row = db.query(RechargeModel).filter(RechargeModel.reference == reference).first()

    if not row:
        raise HTTPException(status_code=404, detail="Transaction not found")

    if getattr(row, "status", "") == "completed":
        return {"detail": "Already settled"}  # idempotent

    # Tumia CRUD_UPDATE kama ipo, vinginevyo direct update
    if CRUD_UPDATE:
        _ = CRUD_UPDATE(db, reference=reference, status=new_status, external_txn_id=external_txn_id, meta=payload)
    else:
        if hasattr(row, "status"): row.status = new_status
        if hasattr(row, "external_txn_id") and external_txn_id:
            row.external_txn_id = external_txn_id
        if hasattr(row, "updated_at"): row.updated_at = _utc()
        if new_status == "completed" and hasattr(row, "completed_at"):
            row.completed_at = _utc()
        if hasattr(row, "meta"):
            # weka metadata yote (hifadhi ya mwisho)
            try:
                row.meta = payload
            except Exception:
                pass
        db.commit()

    return {"detail": "ok", "reference": reference, "status": new_status}

# ------------------------------------------------------------------------------
# â™»ï¸ RETRY (user) â€” rudi pending â†’ recreate session kwa provider
# ------------------------------------------------------------------------------
@router.post(
    "/retry/{reference}",
    response_model=RechargeOut,
    summary="Jaribu tena transaction iliyo 'failed' (user)"
)
def retry_recharge(
    reference: str,
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user)
):
    row = None
    if CRUD_GET_BY_REF:
        row = CRUD_GET_BY_REF(db, reference)
    else:
        if not RechargeModel:
            raise HTTPException(status_code=500, detail="Recharge storage not configured")
        row = db.query(RechargeModel).filter(RechargeModel.reference == reference).first()
    if not row or getattr(row, "user_id", None) != current_user.id:
        raise HTTPException(status_code=404, detail="Transaction not found")

    if getattr(row, "status", "") not in {"failed"}:
        raise HTTPException(status_code=409, detail="Only failed transactions can be retried")

    if CRUD_UPDATE:
        row = CRUD_UPDATE(db, reference=reference, status="pending")
    else:
        if hasattr(row, "status"): row.status = "pending"
        if hasattr(row, "updated_at"): row.updated_at = _utc()
        db.commit(); db.refresh(row)
    return _serialize(row)




