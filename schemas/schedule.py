# backend/schemas/schedule.py
from __future__ import annotations

from enum import Enum
from typing import Optional, List, Dict, Any, Literal
from datetime import datetime, timezone, timedelta
import re

# --- Pydantic v2 kwanza, v1 fallback (shim fupi & nyepesi) -------------------
_V2 = True
try:
    from pydantic import BaseModel, Field, ConfigDict, field_validator, model_validator
except Exception:  # Pydantic v1
    _V2 = False
    from pydantic import BaseModel, Field  # type: ignore
    from pydantic import validator as _v, root_validator as _rv  # type: ignore

    class ConfigDict(dict):  # type: ignore
        pass
    def field_validator(field_name: str, *, mode: str = "after"):  # type: ignore
        pre = (mode == "before")
        def deco(fn):
            return _v(field_name, pre=pre, allow_reuse=True)(fn)  # type: ignore
        return deco
    def model_validator(*, mode: str = "after"):  # type: ignore
        def deco(fn):
            return _rv(pre=(mode == "before"), allow_reuse=True)(fn)  # type: ignore
        return deco

# zoneinfo ya hiari kwa timezone strings (si lazima)
try:
    from zoneinfo import ZoneInfo  # py>=3.9
except Exception:  # pragma: no cover
    ZoneInfo = None  # type: ignore


# ------------------------------- ENUMS --------------------------------------
class Channel(str, Enum):
    sms = "sms"
    email = "email"
    push = "push"
    whatsapp = "whatsapp"
    telegram = "telegram"
    inapp = "inapp"


class ScheduleStatus(str, Enum):
    scheduled = "scheduled"
    queued = "queued"
    sending = "sending"
    sent = "sent"
    failed = "failed"
    canceled = "canceled"
    paused = "paused"


# ------------------------------ MODELS --------------------------------------
class Attachment(BaseModel):
    """Faili/Media (kwa email/push/inapp; si kwa SMS)."""
    filename: str = Field(..., min_length=1, max_length=200)
    url: Optional[str] = Field(default=None, max_length=2048)
    mime_type: Optional[str] = Field(default=None, max_length=100)
    size_bytes: Optional[int] = Field(default=None, ge=0)

    if _V2:
        model_config = ConfigDict(extra="forbid")


class ScheduledMessageBase(BaseModel):
    # Content
    message: str = Field(..., min_length=1, max_length=20000)
    subject: Optional[str] = Field(default=None, max_length=255, description="Lazima kwa email")
    channel: Optional[Channel] = Field(default=None)

    # Targeting
    recipients: Optional[List[str]] = Field(
        default=None, description="namba za simu / emails / user ids"
    )
    segments: Optional[List[str]] = Field(default=None, description="vikundi/majina ya hadhira")
    send_to_all: bool = False

    # Scheduling
    send_at: datetime  # tarehe/saa ya kutuma
    timezone: Optional[str] = Field(
        default=None, description="mfano: 'Africa/Dar_es_Salaam' ikiwa send_at ni naive"
    )
    rrule: Optional[str] = Field(
        default=None, description="iCal RRULE hiari, mfano 'FREQ=DAILY;COUNT=5'"
    )
    priority: Literal["low", "normal", "high"] = "normal"
    idempotency_key: Optional[str] = Field(default=None, max_length=64)

    # Template/Extras
    variables: Optional[Dict[str, Any]] = None
    attachments: Optional[List[Attachment]] = None  # si kwa SMS
    metadata: Optional[Dict[str, Any]] = None

    if _V2:
        model_config = ConfigDict(
            extra="forbid",
            populate_by_name=True,
            json_schema_extra={
                "example": {
                    "message": "Hello {{first_name}}! Tukutane kesho saa 3.",
                    "subject": "Tangazo",
                    "channel": "email",
                    "recipients": ["user@example.com", "user2@example.com"],
                    "send_at": "2025-08-22T12:00:00Z",
                    "timezone": "Africa/Dar_es_Salaam",
                    "priority": "normal",
                    "variables": {"first_name": "Julius"},
                    "attachments": [{"filename": "flyer.pdf", "url": "https://.../flyer.pdf"}],
                    "metadata": {"campaign": "wknd-35"},
                    "idempotency_key": "5a1d3a8e-a0b1-4f7d-9f63-2e0f1a2b3c4d"
                }
            },
        )
    else:
        class Config:  # type: ignore
            extra = "forbid"
            allow_population_by_field_name = True
            schema_extra = {
                "example": {
                    "message": "Hello {{first_name}}! Tukutane kesho saa 3.",
                    "subject": "Tangazo",
                    "channel": "email",
                    "recipients": ["user@example.com", "user2@example.com"],
                    "send_at": "2025-08-22T12:00:00Z",
                    "timezone": "Africa/Dar_es_Salaam",
                    "priority": "normal",
                    "variables": {"first_name": "Julius"},
                    "attachments": [{"filename": "flyer.pdf", "url": "https://.../flyer.pdf"}],
                    "metadata": {"campaign": "wknd-35"},
                    "idempotency_key": "5a1d3a8e-a0b1-4f7d-9f63-2e0f1a2b3c4d"
                }
            }

    # -------------------------- FIELD VALIDATORS -----------------------------

    @field_validator("message", mode="before")
    def _msg_clean(cls, v):
        s = str(v or "").strip()
        if not s:
            raise ValueError("message is required")
        return s

    @field_validator("subject", mode="before")
    def _subj_trim(cls, v):
        if v is None:
            return None
        s = str(v).strip()
        return s or None

    @field_validator("recipients", mode="before")
    def _recipients_norm(cls, v):
        if v is None:
            return None
        if isinstance(v, str):
            v = [p.strip() for p in v.split(",")]
        cleaned: List[str] = []
        for x in v:
            s = str(x).strip()
            if s:
                cleaned.append(s)
        # dedupe while keeping order
        seen = set()
        uniq: List[str] = []
        for s in cleaned:
            if s not in seen:
                seen.add(s)
                uniq.append(s)
        return uniq or None

    @field_validator("segments", mode="before")
    def _segments_norm(cls, v):
        if v is None:
            return None
        out: List[str] = []
        for x in v:
            s = str(x).strip().lower()
            if s and s not in out:
                out.append(s)
        return out or None

    @field_validator("variables", mode="before")
    def _vars_ok(cls, v):
        if v is None:
            return None
        out: Dict[str, Any] = {}
        key_re = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
        for k, val in dict(v).items():
            k2 = str(k).strip()
            if not key_re.match(k2):
                raise ValueError(f"Invalid variable key: {k}")
            out[k2] = val
        return out

    @field_validator("send_at", mode="before")
    def _send_at_parse(cls, v):
        if isinstance(v, str):
            v = datetime.fromisoformat(v.replace("Z", "+00:00"))
        if not isinstance(v, datetime):
            raise ValueError("send_at must be a datetime or ISO string")
        return v

    @field_validator("timezone", mode="before")
    def _tz_trim(cls, v):
        if v is None:
            return None
        s = str(v).strip()
        return s or None

    # ------------------------ MODEL-LEVEL VALIDATION -------------------------
    if _V2:
        @model_validator(mode="after")
        def _check(self):
            # Target requirement
            if not (self.send_to_all or self.recipients or self.segments):
                raise ValueError("Provide at least one of: recipients, segments, or send_to_all=True")

            # Channel constraints
            if self.channel == Channel.email and not self.subject:
                raise ValueError("subject is required for email channel")
            if self.attachments and self.channel == Channel.sms:
                raise ValueError("attachments are not allowed for SMS channel")

            # Time normalization: if naive and timezone provided, apply it
            dt = self.send_at
            if dt.tzinfo is None:
                if self.timezone and ZoneInfo:
                    try:
                        dt = dt.replace(tzinfo=ZoneInfo(self.timezone))
                    except Exception:
                        raise ValueError(f"Invalid timezone: {self.timezone}")
                else:
                    dt = dt.replace(tzinfo=timezone.utc)
            # must be in the future (allow slight skew)
            if dt.astimezone(timezone.utc) < datetime.now(timezone.utc) - timedelta(seconds=2):
                raise ValueError("send_at must be in the future")
            # normalize to UTC
            self.send_at = dt.astimezone(timezone.utc)

            return self
    else:
        @model_validator(mode="after")  # mapped to root_validator in v1
        def _check(cls, values):  # type: ignore
            if not (values.get("send_to_all") or values.get("recipients") or values.get("segments")):
                raise ValueError("Provide at least one of: recipients, segments, or send_to_all=True")

            channel = values.get("channel")
            if channel == Channel.email and not values.get("subject"):
                raise ValueError("subject is required for email channel")
            if values.get("attachments") and channel == Channel.sms:
                raise ValueError("attachments are not allowed for SMS channel")

            dt = values.get("send_at")
            tz = values.get("timezone")
            if dt and dt.tzinfo is None:
                if tz and ZoneInfo:
                    try:
                        dt = dt.replace(tzinfo=ZoneInfo(tz))
                    except Exception:
                        raise ValueError(f"Invalid timezone: {tz}")
                else:
                    dt = dt.replace(tzinfo=timezone.utc)
            if dt and dt.astimezone(timezone.utc) < datetime.now(timezone.utc) - timedelta(seconds=2):
                raise ValueError("send_at must be in the future")
            values["send_at"] = dt.astimezone(timezone.utc) if dt else dt
            return values


class ScheduledMessageCreate(ScheduledMessageBase):
    """Payload ya kuunda ratiba mpya."""
    pass


class ScheduledMessageOut(ScheduledMessageBase):
    id: int
    status: ScheduleStatus = ScheduleStatus.scheduled
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    last_error: Optional[str] = None

    @field_validator("created_at", "updated_at", mode="before")
    def _dt_norm(cls, v):
        if v is None:
            return None
        if isinstance(v, str):
            v = datetime.fromisoformat(v.replace("Z", "+00:00"))
        if not isinstance(v, datetime):
            raise ValueError("invalid datetime")
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    model_config = ConfigDict(from_attributes=True, extra="forbid")

