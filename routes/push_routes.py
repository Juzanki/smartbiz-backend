from __future__ import annotations
# backend/routes/push.py
import hashlib
from contextlib import suppress
from datetime import datetime, timezone
from typing import Optional, List, Any, Dict

from fastapi import (
    APIRouter, Depends, HTTPException, status, Query, Header, Response, Path
)
from sqlalchemy.orm import Session
from sqlalchemy import func

from backend.db import get_db
from backend.auth import get_current_user

# ====== Schemas (tumia zako; hizi ni fallback kama hazipo) ====================
with suppress(Exception):
    from backend.schemas.push_subscription import (
        PushSubscriptionCreate, PushSubscriptionOut, PushSubscriptionUpdate
    )

if "PushSubscriptionCreate" not in globals():
    from pydantic import BaseModel, Field

    class Keys(BaseModel):
        p256dh: str
        auth: str

    class PushSubscriptionCreate(BaseModel):
        user_id: int
        endpoint: str
        keys: Keys
        device_id: Optional[str] = Field(None, description="Unique device fingerprint/ID")
        platform: Optional[str] = Field(None, description="ios|android|web|desktop")
        user_agent: Optional[str] = None
        language: Optional[str] = None
        topics: Optional[List[str]] = None

    class PushSubscriptionUpdate(BaseModel):
        # partial update
        endpoint: Optional[str] = None
        keys: Optional[Keys] = None
        device_id: Optional[str] = None
        platform: Optional[str] = None
        user_agent: Optional[str] = None
        language: Optional[str] = None
        topics: Optional[List[str]] = None
        active: Optional[bool] = None

    class PushSubscriptionOut(PushSubscriptionCreate):
        id: int
        active: bool = True
        created_at: Optional[datetime] = None
        updated_at: Optional[datetime] = None
        last_seen: Optional[datetime] = None
        revoked_at: Optional[datetime] = None

        class Config:  # pyd v1
            orm_mode = True
        model_config = {"from_attributes": True}  # pyd v2

# ====== Model / CRUD fallbacks ===============================================
SubModel = None
with suppress(Exception):
    from backend.models.push_subscription import PushSubscription as SubModel  # type: ignore

with suppress(Exception):
    from backend.crud import push_crud as _crud

CRUD_UPSERT = getattr(_crud, "create_or_update_subscription", None) if "_crud" in globals() else None
CRUD_LIST   = getattr(_crud, "list_user_subscriptions", None) if "_crud" in globals() else None
CRUD_GET    = getattr(_crud, "get_subscription", None) if "_crud" in globals() else None
CRUD_PATCH  = getattr(_crud, "update_subscription", None) if "_crud" in globals() else None
CRUD_DELETE = getattr(_crud, "delete_subscription", None) if "_crud" in globals() else None

# (Hiari) webpush sender utility ukishaweka
with suppress(Exception):
    from backend.utils.webpush import send_webpush  # type: ignore

router = APIRouter(prefix="/push", tags=["Push Notifications"])

# ================= Helpers =================
def _utcnow() -> datetime:
    return datetime.now(timezone.utc)

def _etag_rows(rows: List[Any]) -> str:
    if not rows:
        return 'W/"empty"'
    last = max(
        getattr(r, "updated_at", None)
        or getattr(r, "last_seen", None)
        or getattr(r, "created_at", None)
        or datetime.min
        for r in rows
    )
    ids = ",".join(str(getattr(r, "id", 0)) for r in rows[:200])
    base = f"{ids}|{last.isoformat()}"
    return 'W/"' + hashlib.sha256(base.encode()).hexdigest()[:16] + '"'

def _serialize(row: Any) -> PushSubscriptionOut:
    if hasattr(PushSubscriptionOut, "model_validate"):
        return PushSubscriptionOut.model_validate(row, from_attributes=True)
    return PushSubscriptionOut.model_validate(row)

def _assert_owner(row: Any, user_id: int):
    if getattr(row, "user_id", None) != user_id:
        raise HTTPException(status_code=403, detail="Unauthorized")

# ================= Subscribe (idempotent upsert) =================
@router.post(
    "/subscribe",
    response_model=PushSubscriptionOut,
    status_code=status.HTTP_201_CREATED,
    summary="Jiandikishe kwa push (idempotent kwa user+device au user+endpoint)"
)
def subscribe_push(
    subscription: PushSubscriptionCreate,
    response: Response,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
):
    if subscription.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Unauthorized")

    # Tumia CRUD yako ikipo tayari
    if CRUD_UPSERT:
        row = CRUD_UPSERT(db, subscription)
        response.headers["Cache-Control"] = "no-store"
        return _serialize(row)

    if not SubModel:
        raise HTTPException(status_code=500, detail="Push storage not configured")

    # Idempotent key: (user_id, device_id) kisha (user_id, endpoint)
    q = db.query(SubModel).filter(SubModel.user_id == current_user.id)
    if subscription.device_id and hasattr(SubModel, "device_id"):
        existing = q.filter(SubModel.device_id == subscription.device_id).first()
    else:
        existing = q.filter(SubModel.endpoint == subscription.endpoint).first()

    if existing:
        # no-op if same
        same_endpoint = (existing.endpoint == subscription.endpoint)
        same_keys = (
            getattr(existing, "p256dh", None) == subscription.keys.p256dh and
            getattr(existing, "auth", None) == subscription.keys.auth
        )
        if same_endpoint and same_keys:
            if hasattr(existing, "last_seen"): existing.last_seen = _utcnow()
            if hasattr(existing, "active"): existing.active = True
            db.commit()
            db.refresh(existing)
            response.headers["Cache-Control"] = "no-store"
            return _serialize(existing)

        # update existing
        existing.endpoint = subscription.endpoint
        if hasattr(existing, "p256dh"): existing.p256dh = subscription.keys.p256dh
        if hasattr(existing, "auth"):   existing.auth   = subscription.keys.auth
        if hasattr(existing, "device_id") and subscription.device_id:
            existing.device_id = subscription.device_id
        if hasattr(existing, "platform") and subscription.platform:
            existing.platform = subscription.platform
        if hasattr(existing, "user_agent") and subscription.user_agent:
            existing.user_agent = subscription.user_agent
        if hasattr(existing, "language") and subscription.language:
            existing.language = subscription.language
        if hasattr(existing, "topics") and subscription.topics is not None:
            existing.topics = subscription.topics
        if hasattr(existing, "active"):     existing.active = True
        if hasattr(existing, "updated_at"): existing.updated_at = _utcnow()
        if hasattr(existing, "last_seen"):  existing.last_seen = _utcnow()
        db.commit()
        db.refresh(existing)
        response.headers["Cache-Control"] = "no-store"
        return _serialize(existing)

    # create new
    row = SubModel(
        user_id=current_user.id,
        endpoint=subscription.endpoint,
        device_id=getattr(subscription, "device_id", None),
        platform=getattr(subscription, "platform", None),
        user_agent=getattr(subscription, "user_agent", None),
        language=getattr(subscription, "language", None),
        topics=getattr(subscription, "topics", None),
    )
    # keys columns (common)
    if hasattr(row, "p256dh"): row.p256dh = subscription.keys.p256dh
    if hasattr(row, "auth"):   row.auth   = subscription.keys.auth
    if hasattr(row, "active"): row.active = True
    if hasattr(row, "created_at"): row.created_at = _utcnow()
    if hasattr(row, "updated_at"): row.updated_at = _utcnow()
    if hasattr(row, "last_seen"):  row.last_seen  = _utcnow()

    db.add(row)
    db.commit()
    db.refresh(row)

    response.headers["Cache-Control"] = "no-store"
    return _serialize(row)

# ================= List (pagination + ETag) =================
@router.get(
    "/subscriptions",
    response_model=List[PushSubscriptionOut],
    summary="Orodha ya subscriptions zangu (pagination + ETag/304)"
)
def list_my_subscriptions(
    response: Response,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
    active: Optional[bool] = Query(None),
    q: Optional[str] = Query(None, description="tafuta kwa platform/device/UA"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    if_none_match: Optional[str] = Header(None, alias="If-None-Match"),
):
    if CRUD_LIST and not SubModel:
        rows = CRUD_LIST(db, current_user.id) or []
        # in-memory minimal filters
        if active is not None:
            rows = [r for r in rows if bool(getattr(r, "active", True)) == active]
        if q:
            ql = q.lower()
            rows = [r for r in rows if ql in (getattr(r, "platform", "") or "").lower()
                                 or ql in (getattr(r, "device_id", "") or "").lower()
                                 or ql in (getattr(r, "user_agent", "") or "").lower()]
        total = len(rows)
        rows = rows[offset: offset + limit]
    else:
        if not SubModel:
            raise HTTPException(status_code=500, detail="Push storage not configured")
        qry = db.query(SubModel).filter(SubModel.user_id == current_user.id)
        if active is not None and hasattr(SubModel, "active"):
            qry = qry.filter(SubModel.active.is_(active))
        if q:
            like = f"%{q}%"
            orx = []
            for col in ("platform", "device_id", "user_agent"):
                if hasattr(SubModel, col):
                    orx.append(getattr(SubModel, col).ilike(like))
            if orx:
                from sqlalchemy import or_
                qry = qry.filter(or_(*orx))
        # soft-delete?
        if hasattr(SubModel, "revoked_at"):
            qry = qry.filter(SubModel.revoked_at.is_(None))
        qry = qry.order_by(
            getattr(SubModel, "updated_at", getattr(SubModel, "id")).desc()
        )
        total = qry.count()
        rows = qry.offset(offset).limit(limit).all()

    etag = _etag_rows(rows)
    if if_none_match and if_none_match == etag:
        return Response(status_code=status.HTTP_304_NOT_MODIFIED)

    response.headers["ETag"] = etag
    response.headers["Cache-Control"] = "public, max-age=15"
    response.headers["X-Total-Count"] = str(total)
    response.headers["X-Limit"] = str(limit)
    response.headers["X-Offset"] = str(offset)
    return [_serialize(r) for r in rows]

# ================= Get one =================
@router.get(
    "/subscriptions/{sub_id}",
    response_model=PushSubscriptionOut,
    summary="Pata subscription moja"
)
def get_subscription(
    sub_id: int = Path(..., ge=1),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    row = None
    if CRUD_GET and not SubModel:
        row = CRUD_GET(db, sub_id)
    else:
        if not SubModel:
            raise HTTPException(status_code=500, detail="Push storage not configured")
        row = db.query(SubModel).filter(SubModel.id == sub_id).first()

    if not row:
        raise HTTPException(status_code=404, detail="Subscription not found")
    _assert_owner(row, current_user.id)
    return _serialize(row)

# ================= PATCH update (topics/UA/active/keys) =================
@router.patch(
    "/subscriptions/{sub_id}",
    response_model=PushSubscriptionOut,
    summary="Sasisha subscription (partial)"
)
def patch_subscription(
    sub_id: int,
    payload: PushSubscriptionUpdate,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if CRUD_PATCH and not SubModel:
        row = CRUD_PATCH(db, sub_id, payload)
        if not row:
            raise HTTPException(status_code=404, detail="Subscription not found")
        _assert_owner(row, current_user.id)
        return _serialize(row)

    if not SubModel:
        raise HTTPException(status_code=500, detail="Push storage not configured")

    row = db.query(SubModel).filter(SubModel.id == sub_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Subscription not found")
    _assert_owner(row, current_user.id)

    data = payload.dict(exclude_unset=True)
    if "keys" in data and data["keys"] is not None:
        keys = data.pop("keys")
        if hasattr(row, "p256dh"): row.p256dh = keys.p256dh
        if hasattr(row, "auth"):   row.auth   = keys.auth

    for k, v in data.items():
        if hasattr(row, k):
            setattr(row, k, v)

    if hasattr(row, "updated_at"): row.updated_at = _utcnow()
    db.commit()
    db.refresh(row)
    return _serialize(row)

# ================= Unsubscribe (soft/hard) =================
@router.delete(
    "/subscriptions/{sub_id}",
    response_model=dict,
    summary="Ondoa subscription (soft delete ikiwa revoked_at ipo)"
)
def unsubscribe(
    sub_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if CRUD_DELETE and not SubModel:
        ok = CRUD_DELETE(db, sub_id, current_user.id)
        if not ok:
            raise HTTPException(status_code=404, detail="Subscription not found")
        return {"detail": "Unsubscribed"}

    if not SubModel:
        raise HTTPException(status_code=500, detail="Push storage not configured")

    row = db.query(SubModel).filter(SubModel.id == sub_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Subscription not found")
    _assert_owner(row, current_user.id)

    if hasattr(row, "revoked_at"):
        row.revoked_at = _utcnow()
        if hasattr(row, "active"):
            row.active = False
        if hasattr(row, "updated_at"):
            row.updated_at = _utcnow()
        db.commit()
    else:
        db.delete(row)
        db.commit()
    return {"detail": "Unsubscribed"}

# ================= Test push (optional; hutumia util yako) =================
@router.post(
    "/subscriptions/{sub_id}/test",
    response_model=dict,
    summary="Tuma test notification kwa subscription moja"
)
def test_push(
    sub_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not SubModel:
        raise HTTPException(status_code=500, detail="Push storage not configured")
    row = db.query(SubModel).filter(SubModel.id == sub_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Subscription not found")
    _assert_owner(row, current_user.id)

    # Tumia webpush sender yako ukipo
    if "send_webpush" in globals():
        payload = {"title": "SmartBiz", "body": "Test push successful âœ…"}
        send_webpush(row, payload)  # andika util yako ipokee endpoint/keys
        return {"detail": "Sent"}
    # fallback (bila kutuma halisi)
    return {"detail": "Simulated send (configure backend.utils.webpush.send_webpush)"}  # noqa: E501

