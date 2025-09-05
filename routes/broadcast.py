from __future__ import annotations
# backend/routes/broadcast.py
import os
import time
import asyncio
import logging
from datetime import datetime
from typing import Optional, List, Dict, Any, Iterable, Literal
from contextlib import suppress

from fastapi import APIRouter, Depends, HTTPException, status, Header, Query, Response
from sqlalchemy.orm import Session
from sqlalchemy import or_, and_, func

from backend.db import get_db
from backend.models.user import User
from backend.dependencies import check_admin
from backend.schemas import BroadcastMessage  # ukitumia yako ya awali bado itafanya kazi

# Telegram adapter (lazima ipo kwenye mradi wako)
from backend.utils.telegram_bot import send_telegram_message

# Hiari: adapters wengine kama watakuwepo
with suppress(Exception):
    from backend.utils.whatsapp_bot import send_whatsapp_message  # type: ignore
with suppress(Exception):
    from backend.utils.sms_gateway import send_sms  # type: ignore

# Hiari: audit logger kama uliweka ile route
with suppress(Exception):
    from backend.routes.audit_log import emit_audit  # type: ignore

logger = logging.getLogger("smartbiz.broadcast")

router = APIRouter(prefix="/broadcast", tags=["Broadcasts"])

# ----------------------------- Config ----------------------------- #
# Throttling ya jumla kwa maombi ya broadcast (per admin)
BROADCAST_RATE_PER_MIN = int(os.getenv("BROADCAST_RATE_PER_MIN", "5"))
_RATE: Dict[int, List[float]] = {}

# Idempotency (in-memory); unaweza kubadili kwenda Redis/DB
_IDEMP: Dict[tuple[int, str], float] = {}
_IDEMP_TTL = 10 * 60  # sekunde

# Limits za batching / concurrency (mobile-safe)
DEFAULT_BATCH_SIZE = int(os.getenv("BROADCAST_BATCH_SIZE", "200"))
DEFAULT_CONCURRENCY = int(os.getenv("BROADCAST_CONCURRENCY", "10"))
MAX_CONCURRENCY = 50

# Retry policy
MAX_RETRIES = 3
BASE_BACKOFF = 0.7  # sekunde


def _rate_ok(admin_id: int) -> None:
    now = time.time()
    q = _RATE.setdefault(admin_id, [])
    while q and (now - q[0]) > 60.0:
        q.pop(0)
    if len(q) >= BROADCAST_RATE_PER_MIN:
        raise HTTPException(status_code=429, detail="Too many broadcast attempts. Try again shortly.")
    q.append(now)


def _check_idempotency(admin_id: int, key: Optional[str]) -> None:
    if not key:
        return
    now = time.time()
    stale = [(uid, k) for (uid, k), ts in _IDEMP.items() if now - ts > _IDEMP_TTL]
    for s in stale:
        _IDEMP.pop(s, None)
    token = (admin_id, key.strip())
    if token in _IDEMP:
        raise HTTPException(status_code=409, detail="Duplicate request (Idempotency-Key)")
    _IDEMP[token] = now


# ----------------------------- Schemas ext ----------------------------- #
# Ukiwa na `backend.schemas.BroadcastMessage` ya zamani itaendelea kufanya kazi.
# Hii hapa ni schema iliyopanuliwa ikiwa unataka uwezo zaidi (hiari).
try:
    from pydantic import BaseModel, Field
    class BroadcastPayload(BaseModel):
        message: str = Field(..., min_length=1)
        channels: List[Literal["telegram", "whatsapp", "sms"]] = Field(default_factory=lambda: ["telegram"])
        # personalization options
        variables: Dict[str, Any] = Field(default_factory=dict, description="Global template variables")
        # audience filters
        roles: Optional[List[str]] = None           # ["admin","owner","user"]
        plans: Optional[List[str]] = None           # ["free","pro","business"]
        language: Optional[str] = None
        has_telegram: Optional[bool] = None
        has_phone: Optional[bool] = None
        user_ids: Optional[List[int]] = None
        created_from: Optional[datetime] = None
        created_to: Optional[datetime] = None
        # control
        dry_run: bool = False
        schedule_at: Optional[datetime] = None
        batch_size: int = Field(DEFAULT_BATCH_SIZE, ge=1, le=5000)
        concurrency: int = Field(DEFAULT_CONCURRENCY, ge=1, le=MAX_CONCURRENCY)
except Exception:
    BroadcastPayload = BroadcastMessage  # fallback kwa schema yako ya awali


# ----------------------------- Utils ----------------------------- #
class SafeDict(dict):
    def __missing__(self, key):
        return "{" + key + "}"  # acha placeholder badala ya ku-fail

def _ctx_for_user(u: User, extra: Dict[str, Any]) -> Dict[str, Any]:
    first_name = ""
    if getattr(u, "full_name", None):
        first_name = str(u.full_name).split()[0]
    return {
        "user_id": u.id,
        "email": u.email,
        "username": u.username,
        "full_name": u.full_name,
        "first_name": first_name,
        "phone_number": u.phone_number,
        "language": u.language,
        "plan": u.subscription_status,
        "business_name": getattr(u, "business_name", None),
        **(extra or {}),
    }

def _render_message(tpl: str, user: User, variables: Dict[str, Any]) -> str:
    ctx = _ctx_for_user(user, variables)
    # str.format_map bila kuvunjika
    return str(tpl).format_map(SafeDict(ctx))

async def _send_one_channel(channel: str, user: User, text: str) -> dict:
    """
    Rudisha dict yenye status kwa channel moja: {"ok": bool, "err": str|None}
    """
    try:
        if channel == "telegram":
            tid = getattr(user, "telegram_id", None)
            if not tid:
                return {"ok": False, "err": "no_telegram"}
            await send_telegram_message(tid, text)
            return {"ok": True, "err": None}

        if channel == "whatsapp":
            if "send_whatsapp_message" not in globals():
                return {"ok": False, "err": "wa_adapter_missing"}
            phone = getattr(user, "phone_number", None)
            if not phone:
                return {"ok": False, "err": "no_phone"}
            with suppress(Exception):
                await send_whatsapp_message(phone, text)  # type: ignore
            return {"ok": True, "err": None}

        if channel == "sms":
            if "send_sms" not in globals():
                return {"ok": False, "err": "sms_adapter_missing"}
            phone = getattr(user, "phone_number", None)
            if not phone:
                return {"ok": False, "err": "no_phone"}
            with suppress(Exception):
                await send_sms(phone, text)  # type: ignore
            return {"ok": True, "err": None}

        return {"ok": False, "err": "unsupported_channel"}
    except Exception as e:
        return {"ok": False, "err": str(e)}

async def _send_user(user: User, channels: List[str], text: str, sem: asyncio.Semaphore) -> Dict[str, Any]:
    """
    Tuma kwa user mmoja kwenye channels kadhaa, na retries.
    """
    out: Dict[str, Any] = {"user_id": user.id, "results": {}}
    async with sem:
        for ch in channels:
            retries = 0
            backoff = BASE_BACKOFF
            while True:
                res = await _send_one_channel(ch, user, text)
                if res["ok"] or retries >= MAX_RETRIES:
                    out["results"][ch] = res
                    break
                await asyncio.sleep(backoff)
                retries += 1
                backoff *= 2.0
    return out


def _query_audience(db: Session, p: BroadcastPayload) -> List[User]:
    q = db.query(User)

    # filters
    if p.user_ids:
        q = q.filter(User.id.in_(p.user_ids))
    if p.roles:
        q = q.filter(User.role.in_(p.roles))
    if p.plans:
        q = q.filter(User.subscription_status.in_(p.plans))
    if p.language:
        q = q.filter(func.lower(User.language) == p.language.strip().lower())
    if p.has_telegram is True:
        q = q.filter(User.telegram_id.isnot(None))
    if p.has_telegram is False:
        q = q.filter(User.telegram_id.is_(None))
    if p.has_phone is True:
        q = q.filter(User.phone_number.isnot(None))
    if p.has_phone is False:
        q = q.filter(or_(User.phone_number.is_(None), User.phone_number == ""))
    if p.created_from and hasattr(User, "created_at"):
        q = q.filter(User.created_at >= p.created_from)
    if p.created_to and hasattr(User, "created_at"):
        q = q.filter(User.created_at <= p.created_to)

    # order kwa id ya juu â†’ rahisi kwa cursor/infinite scroll
    q = q.order_by(User.id.desc())
    return q.all()


def _maybe_schedule(db: Session, payload: BroadcastPayload, admin_id: int) -> Optional[dict]:
    """
    Ukipeleka schedule_at ya siku/masaa yajayo:
    - Jaribu kuandika kwenye model/CRUD iliyopo (ScheduledMessage/ScheduledTask n.k.)
    - Ukikosa miundombinu, rudisha taarifa kuwa "scheduling not available".
    """
    if not payload.schedule_at:
        return None

    # 1) ScheduledMessage (kama upo)
    with suppress(Exception):
from backend.models.scheduled_message import ScheduledMessage
        from backend.schemas.scheduled import ScheduledMessageCreate  # type: ignore
        from backend.crud.schedule_crud import create_scheduled_message  # type: ignore

        # Hapa tunatengeneza moja kwa moja ujumbe wa "broadcast" kwa admin_id
        msg = ScheduledMessageCreate(
            content=payload.message,
            scheduled_time=payload.schedule_at,
            channel="telegram",   # unaweza kuboresha kwa kuhifadhi list ya channels
            metadata={"channels": payload.channels, "variables": payload.variables},
        )
        created = create_scheduled_message(db, user_id=admin_id, msg=msg)
        return {"scheduled_via": "ScheduledMessage", "id": getattr(created, "id", None)}

    # 2) ScheduledTask (kama ipo)
    with suppress(Exception):
        from backend.schemas.scheduled_task import ScheduledTaskCreate  # type: ignore
        from backend.crud.scheduled_task_crud import create_scheduled_task  # type: ignore

        task = ScheduledTaskCreate(
            user_id=admin_id,
            type="broadcast",
            content={
                "message": payload.message,
                "channels": payload.channels,
                "variables": payload.variables,
                "filters": {
                    "roles": payload.roles,
                    "plans": payload.plans,
                    "language": payload.language,
                    "has_telegram": payload.has_telegram,
                    "has_phone": payload.has_phone,
                    "user_ids": payload.user_ids,
                    "created_from": str(payload.created_from) if payload.created_from else None,
                    "created_to": str(payload.created_to) if payload.created_to else None,
                },
            },
            scheduled_time=payload.schedule_at,
        )
        created = create_scheduled_task(db, task)
        return {"scheduled_via": "ScheduledTask", "id": getattr(created, "id", None)}

    # Hakuna miundombinu ya scheduling
    return {"scheduled_via": None, "note": "Scheduling infra not available"}


# ========================== ENDPOINTS ========================== #
@router.post(
    "",
    summary="ðŸ“¢ Tuma broadcast (filters, templates, batching, retries)",
    status_code=status.HTTP_200_OK,
    dependencies=[Depends(check_admin)],
)
async def broadcast_message(
    payload: BroadcastPayload,  # au BroadcastMessage yako ya zamani
    response: Response,
    db: Session = Depends(get_db),
    current_admin: User = Depends(check_admin),
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
):
    # Throttling ya maombi ya admin huyu + idempotency
    _rate_ok(current_admin.id)
    _check_idempotency(current_admin.id, idempotency_key)

    # Scheduling?
    sched_info = _maybe_schedule(db, payload, admin_id=current_admin.id)
    if sched_info:
        response.headers["Cache-Control"] = "no-store"
        with suppress(Exception):
            if "emit_audit" in globals():
                emit_audit(
                    db,
                    action="broadcast.schedule",
                    status="success",
                    severity="info",
                    actor_id=current_admin.id,
                    actor_email=current_admin.email,
                    resource_type="broadcast",
                    resource_id=str(sched_info.get("id")),
                    meta={"schedule": str(payload.schedule_at), "via": sched_info.get("scheduled_via")},
                )
        return {
            "scheduled": True,
            "when": payload.schedule_at,
            "via": sched_info.get("scheduled_via"),
            "id": sched_info.get("id"),
        }

    # Tanguliza selection ya audience
    users = _query_audience(db, payload)
    total = len(users)

    # Dry-run?
    if getattr(payload, "dry_run", False):
        sample_text = None
        if users:
            sample_text = _render_message(payload.message, users[0], payload.variables or {})
        response.headers["Cache-Control"] = "no-store"
        return {
            "dry_run": True,
            "total_recipients": total,
            "sample_user_id": users[0].id if users else None,
            "sample_message": sample_text,
            "filters": {
                "roles": payload.roles, "plans": payload.plans,
                "language": payload.language, "has_telegram": payload.has_telegram,
                "has_phone": payload.has_phone,
            },
        }

    # Real send: batch + concurrency
    batch_size = max(1, min(int(getattr(payload, "batch_size", DEFAULT_BATCH_SIZE)), 5000))
    concurrency = max(1, min(int(getattr(payload, "concurrency", DEFAULT_CONCURRENCY)), MAX_CONCURRENCY))
    sem = asyncio.Semaphore(concurrency)

    per_channel = {ch: {"ok": 0, "fail": 0} for ch in payload.channels}
    skipped = 0
    tasks: List[asyncio.Task] = []
    sent_results: List[Dict[str, Any]] = []

    async def _process_user(u: User):
        text = _render_message(payload.message, u, payload.variables or {})
        res = await _send_user(u, payload.channels, text, sem)
        sent_results.append(res)

    # batch loop
    for i in range(0, total, batch_size):
        chunk = users[i : i + batch_size]
        tasks = [asyncio.create_task(_process_user(u)) for u in chunk]
        await asyncio.gather(*tasks)

        # (Optional) kidogo kupumzika kati ya batches kwa heshima ya provider limits
        await asyncio.sleep(0.1)

    # Aggregate results
    for r in sent_results:
        for ch, info in r["results"].items():
            if info.get("ok"):
                per_channel[ch]["ok"] += 1
            else:
                err = info.get("err")
                if err in {"no_telegram", "no_phone"}:
                    skipped += 1
                per_channel[ch]["fail"] += 1

    # Audit (hiari)
    with suppress(Exception):
        if "emit_audit" in globals():
            emit_audit(
                db,
                action="broadcast.send",
                status="success",
                severity="info",
                actor_id=current_admin.id,
                actor_email=current_admin.email,
                resource_type="broadcast",
                resource_id=None,
                meta={"total": total, "channels": payload.channels, "per_channel": per_channel, "skipped": skipped},
            )

    response.headers["Cache-Control"] = "no-store"
    return {
        "message": "âœ… Broadcast processed",
        "total_recipients": total,
        "skipped_missing_contact": skipped,
        "per_channel": per_channel,
        "batch_size": batch_size,
        "concurrency": concurrency,
    }

