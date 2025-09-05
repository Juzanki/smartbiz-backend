from __future__ import annotations
# backend/routes/replies.py
import asyncio
import inspect
import os
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional, Callable, Awaitable, Union

from fastapi import APIRouter, Depends, HTTPException, Header, Query, status
from pydantic import BaseModel, Field, validator
from sqlalchemy.orm import Session

from backend.db import get_db
from backend.auth import get_current_user
from backend.models.user import User
from backend.utils.telegram_bot import send_telegram_message
from backend.utils.whatsapp import send_whatsapp_message
from backend.utils.sms import send_sms_message

router = APIRouter(prefix="/replies", tags=["Replies"])

# --------------------------- Env Toggles ---------------------------
ENABLE_WHATSAPP = (os.getenv("ENABLE_WHATSAPP") or "1").strip().lower() in {"1","true","yes","on"}
ENABLE_SMS      = (os.getenv("ENABLE_SMS") or "1").strip().lower() in {"1","true","yes","on"}

# --------------------------- Pydantic Schemas ---------------------------
class Platform(str):
    TELEGRAM = "telegram"
    WHATSAPP = "whatsapp"
    SMS = "sms"

PHONE_RE = re.compile(r"^\+?[1-9]\d{6,14}$")  # E.164 lenient

class ReplyRequest(BaseModel):
    chat_id: str = Field(..., min_length=1)
    platform: str = Field(..., description="telegram | whatsapp | sms")
    message: str = Field(..., min_length=1, max_length=4000)

    @validator("platform")
    def _platform_ok(cls, v: str) -> str:
        v2 = v.strip().lower()
        if v2 not in {Platform.TELEGRAM, Platform.WHATSAPP, Platform.SMS}:
            raise ValueError("platform must be one of: telegram, whatsapp, sms")
        return v2

    @validator("chat_id")
    def _chat_id_ok(cls, v: str, values: Dict[str, Any]) -> str:
        platform = (values.get("platform") or "").lower()
        if platform in {Platform.WHATSAPP, Platform.SMS}:
            vv = v.strip()
            # Ruhusu 0-kuongoza maeneo flani? Bora E.164 â€” mtumiaji alete +255...
            if not PHONE_RE.match(vv):
                raise ValueError("chat_id must be E.164 phone e.g. +2557XXXXXXXX for whatsapp/sms")
        return v.strip()

class ReplyResult(BaseModel):
    status: str
    platform: str
    chat_id: str
    message: str
    idempotent: bool = False
    queued: bool = False
    provider_response: Optional[Dict[str, Any]] = None

# --------------------------- Idempotency (fallback) ---------------------------
# Kipaumbele: DB column `idempotency_key` kwenye jedwali la ReplyLog (ikiguswa chini).
# Fallback ya in-memory (per process) â€” TTL ~ 5 min.
_IDEMP_CACHE: Dict[str, float] = {}
_IDEMP_TTL = 300.0  # sekunde

def _idem_key(user_id: int, payload: ReplyRequest, idem_hdr: Optional[str]) -> str:
    # Ikiwa header ipo, itumike kama key kuu (salama kwa retries).
    base = idem_hdr or f"{user_id}:{payload.platform}:{payload.chat_id}:{hash(payload.message)}"
    return base

def _idem_seen(key: str) -> bool:
    now = time.time()
    # safisha harakaharaka
    for k, t in list(_IDEMP_CACHE.items()):
        if now - t > _IDEMP_TTL:
            _IDEMP_CACHE.pop(k, None)
    if key in _IDEMP_CACHE:
        return True
    _IDEMP_CACHE[key] = now
    return False

# --------------------------- Dispatch Helpers ---------------------------
@dataclass
class Sender:
    fn: Callable[..., Union[Dict[str, Any], str, None, Awaitable[Any]]]
    enabled: bool

SENDER_MAP: Dict[str, Sender] = {
    Platform.TELEGRAM: Sender(send_telegram_message, True),
    Platform.WHATSAPP: Sender(send_whatsapp_message, ENABLE_WHATSAPP),
    Platform.SMS:      Sender(send_sms_message, ENABLE_SMS),
}

async def _call_sender(sender: Sender, **kwargs) -> Dict[str, Any]:
    fn = sender.fn
    try:
        if inspect.iscoroutinefunction(fn):
            res = await fn(**kwargs)
        else:
            # run sync in thread to avoid blocking event loop
            res = await asyncio.to_thread(fn, **kwargs)
        # Normalize to dict
        if isinstance(res, dict):
            return res
        if res is None:
            return {}
        return {"result": str(res)}
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Provider error: {e}")

# --------------------------- Route ---------------------------
@router.post(
    "/send-reply",
    response_model=ReplyResult,
    summary="Tuma jibu kwa Telegram/WhatsApp/SMS (na idempotency + retries)"
)
async def send_reply(
    payload: ReplyRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
    dry_run: bool = Query(False, description="Ikiwa True, haitatuma; itarudisha tu preview"),
    max_retries: int = Query(2, ge=0, le=5, description="Retries kwenye makosa ya muda mfupi"),
):
    # 1) Feature flags
    s = SENDER_MAP[payload.platform]
    if not s.enabled:
        raise HTTPException(status_code=503, detail=f"{payload.platform} currently disabled")

    # 2) Idempotency (fallback in-memory). Ikiwa una ReplyLog na column idempotency_key
    # unaweza kusogeza ulinzi huu DB-level.
    key = _idem_key(current_user.id, payload, idempotency_key)
    seen = _idem_seen(key)
    if seen and idempotency_key:
        # Ikiwa header ilikuwepo, chukulia kwamba ni retry halali â†’ usitume tena
        return ReplyResult(
            status="ok", platform=payload.platform, chat_id=payload.chat_id,
            message=payload.message, idempotent=True, queued=False, provider_response={"note": "duplicate suppressed"}
        )

    # 3) Dry run (hakuna kutuma)
    if dry_run:
        return ReplyResult(
            status="ok",
            platform=payload.platform,
            chat_id=payload.chat_id,
            message=payload.message,
            idempotent=False,
            queued=False,
            provider_response={"dry_run": True}
        )

    # 4) Tuma na retries ndogo (exponential backoff)
    attempt = 0
    backoff = 0.6
    last_exc: Optional[HTTPException] = None
    while attempt <= max_retries:
        try:
            if payload.platform == Platform.TELEGRAM:
                res = await _call_sender(s, chat_id=payload.chat_id, message=payload.message)
            elif payload.platform == Platform.WHATSAPP:
                res = await _call_sender(s, phone=payload.chat_id, message=payload.message)
            else:  # SMS
                res = await _call_sender(s, phone=payload.chat_id, message=payload.message)

            # 5) Audit log (ikibahatika kuwepo model ReplyLog)
            with suppress(Exception):
                from backend.models.reply_log import ReplyLog  # type: ignore
                row = ReplyLog(
                    user_id=current_user.id,
                    platform=payload.platform,
                    recipient=payload.chat_id,
                    message=payload.message,
                    idempotency_key=idempotency_key,
                )
                db.add(row)
                db.commit()

            return ReplyResult(
                status="ok",
                platform=payload.platform,
                chat_id=payload.chat_id,
                message=payload.message,
                idempotent=bool(idempotency_key and seen),
                queued=False,
                provider_response=res,
            )
        except HTTPException as e:
            last_exc = e
            attempt += 1
            if attempt > max_retries:
                break
            await asyncio.sleep(backoff)
            backoff *= 2

    # 6) Failure
    raise last_exc or HTTPException(status_code=502, detail="Failed to send message")


