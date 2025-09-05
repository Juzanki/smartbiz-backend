# backend/schemas/campaigns.py
from __future__ import annotations

from enum import Enum
from typing import Optional, List, Dict, Any
from datetime import datetime, timezone, timedelta
import re

# -------- Pydantic v2 first, v1 fallback (compact shims) --------------------
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

# -------- Optional zoneinfo for timezone validation -------------------------
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


class CampaignStatus(str, Enum):
    draft = "draft"
    scheduled = "scheduled"
    running = "running"
    paused = "paused"
    sent = "sent"
    failed = "failed"
    canceled = "canceled"


# ------------------------------ MODELS --------------------------------------
class Attachment(BaseModel):
    """Optional file/media for non-SMS channels."""
    filename: str = Field(..., min_length=1, max_length=200)
    url: Optional[str] = Field(default=None)
    mime_type: Optional[str] = Field(default=None, max_length=100)
    size_bytes: Optional[int] = Field(default=None, ge=0)

    if _V2:
        model_config = ConfigDict(extra="forbid")


class CampaignBase(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    message: str = Field(..., min_length=1, max_length=20000, description="Body or template body.")
    subject: Optional[str] = Field(default=None, max_length=255, description="Required for email.")
    channel: Optional[Channel] = Field(default=None)

    # Targeting
    audience_ids: Optional[List[str]] = Field(
        default=None, description="User IDs / phones / emails depending on channel."
    )
    segments: Optional[List[str]] = Field(
        default=None, description="Named audience segments (optional)."
    )
    send_to_all: bool = False

    # Scheduling
    send_at: Optional[datetime] = Field(
        default=None, description="UTC time to send (future). If naive, uses `timezone`."
    )
    timezone: Optional[str] = Field(
        default=None, description="IANA TZ like 'Africa/Dar_es_Salaam' if send_at is naive."
    )
    rrule: Optional[str] = Field(
        default=None,
        description="Optional iCal RRULE for recurrence (e.g., 'FREQ=DAILY;COUNT=5')."
    )

    # Templating / Extras
    variables: Optional[Dict[str, Any]] = None
    attachments: Optional[List[Attachment]] = None  # not for SMS
    metadata: Optional[Dict[str, Any]] = None

    if _V2:
        model_config = ConfigDict(
            extra="forbid",
            populate_by_name=True,
            json_schema_extra={
                "example": {
                    "name": "August Promo",
                    "channel": "email",
                    "subject": "ðŸ”¥ Weekend Offer",
                    "message": "Hello {{first_name}}, enjoy -20% this weekend!",
                    "audience_ids": ["user1@example.com", "user2@example.com"],
                    "segments": ["loyal", "recent-buyers"],
                    "send_at": "2025-08-21T12:00:00Z",
                    "timezone": "Africa/Dar_es_Salaam",
                    "variables": {"first_name": "Julius"},
                    "attachments": [{"filename": "flyer.pdf", "url": "https://.../flyer.pdf"}],
                    "metadata": {"campaign": "wknd-34"},
                    "rrule": None,
                    "send_to_all": False,
                }
            },
        )
    else:
        class Config:  # type: ignore
            extra = "forbid"
            allow_population_by_field_name = True
            schema_extra = {
                "example": {
                    "name": "August Promo",
                    "channel": "email",
                    "subject": "ðŸ”¥ Weekend Offer",
                    "message": "Hello {{first_name}}, enjoy -20% this weekend!",
                    "audience_ids": ["user1@example.com", "user2@example.com"],
                    "segments": ["loyal", "recent-buyers"],
                    "send_at": "2025-08-21T12:00:00Z",
                    "timezone": "Africa/Dar_es_Salaam",
                    "variables": {"first_name": "Julius"},
                    "attachments": [{"filename": "flyer.pdf", "url": "https://.../flyer.pdf"}],
                    "metadata": {"campaign": "wknd-34"},
                    "rrule": None,
                    "send_to_all": False,
                }
            }

    # -------------------------- FIELD VALIDATORS -----------------------------

    @field_validator("name", mode="before")
    def _strip_name(cls, v):
        s = str(v or "").strip()
        if not s:
            raise ValueError("name is required")
        return s

    @field_validator("subject", mode="before")
    def _strip_subject(cls, v):
        if v is None:
            return None
        s = str(v).strip()
        return s or None

    @field_validator("message", mode="before")
    def _strip_message(cls, v):
        s = str(v or "").strip()
        if not s:
            raise ValueError("message is required")
        return s

    @field_validator("audience_ids", mode="before")
    def _normalize_audience(cls, v):
        if v is None:
            return None
        if isinstance(v, str):
            v = [p.strip() for p in v.split(",")]
        cleaned: List[str] = []
        for x in v:
            s = str(x).strip()
            if s:
                cleaned.append(s)
        # dedupe while preserving order
        seen = set()
        uniq: List[str] = []
        for s in cleaned:
            if s not in seen:
                seen.add(s)
                uniq.append(s)
        return uniq or None

    @field_validator("segments", mode="before")
    def _normalize_segments(cls, v):
        if v is None:
            return None
        out: List[str] = []
        for x in v:
            s = str(x).strip().lower()
            if s:
                out.append(s)
        return out or None

    @field_validator("variables", mode="before")
    def _validate_vars(cls, v):
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
    def _to_utc(cls, v):
        if v is None:
            return None
        if isinstance(v, str):
            v = datetime.fromisoformat(v.replace("Z", "+00:00"))
        if not isinstance(v, datetime):
            raise ValueError("send_at must be a datetime or ISO string")
        return v

    # ------------------------ MODEL-LEVEL VALIDATION -------------------------
    if _V2:
        @model_validator(mode="after")
        def _check(self):
            # Target rule
            if not (self.send_to_all or self.audience_ids or self.segments):
                raise ValueError("Provide at least one of: audience_ids, segments, or send_to_all=True")

            # Email requires subject
            if self.channel == Channel.email and not self.subject:
                raise ValueError("subject is required for email channel")

            # Attachments cannot be used with SMS
            if self.attachments and self.channel == Channel.sms:
                raise ValueError("attachments are not allowed for SMS channel")

            # SMS subject (if given) must be short
            if self.subject and self.channel == Channel.sms and len(self.subject) > 160:
                raise ValueError("subject too long for SMS (max 160)")

            # Timezone handling for naive send_at
            if self.send_at:
                dt = self.send_at
                if dt.tzinfo is None:
                    if self.timezone and ZoneInfo:
                        try:
                            dt = dt.replace(tzinfo=ZoneInfo(self.timezone))
                        except Exception:
                            raise ValueError(f"Invalid timezone: {self.timezone}")
                    else:
                        dt = dt.replace(tzinfo=timezone.utc)
                # must be in the future (allow small skew)
                if dt.astimezone(timezone.utc) < datetime.now(timezone.utc) - timedelta(seconds=2):
                    raise ValueError("send_at must be in the future")
                # normalize to UTC for storage/output
                self.send_at = dt.astimezone(timezone.utc)

            return self
    else:
        @model_validator(mode="after")  # mapped to root_validator in v1
        def _check(cls, values):  # type: ignore
            audience_ids = values.get("audience_ids")
            segments = values.get("segments")
            send_to_all = values.get("send_to_all")
            channel = values.get("channel")
            subject = values.get("subject")
            attachments = values.get("attachments")
            send_at = values.get("send_at")
            tz = values.get("timezone")

            if not (send_to_all or audience_ids or segments):
                raise ValueError("Provide at least one of: audience_ids, segments, or send_to_all=True")

            if channel == Channel.email and not subject:
                raise ValueError("subject is required for email channel")

            if attachments and channel == Channel.sms:
                raise ValueError("attachments are not allowed for SMS channel")

            if subject and channel == Channel.sms and len(subject) > 160:
                raise ValueError("subject too long for SMS (max 160)")

            if send_at:
                dt = send_at
                if dt.tzinfo is None:
                    if tz and ZoneInfo:
                        try:
                            dt = dt.replace(tzinfo=ZoneInfo(tz))
                        except Exception:
                            raise ValueError(f"Invalid timezone: {tz}")
                    else:
                        dt = dt.replace(tzinfo=timezone.utc)
                if dt.astimezone(timezone.utc) < datetime.now(timezone.utc) - timedelta(seconds=2):
                    raise ValueError("send_at must be in the future")
                values["send_at"] = dt.astimezone(timezone.utc)

            return values


class CampaignCreate(CampaignBase):
    idempotency_key: Optional[str] = Field(
        default=None, description="Use to de-duplicate client retries."
    )


class CampaignOut(CampaignBase):
    id: int
    status: CampaignStatus = CampaignStatus.draft
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    total_recipients: Optional[int] = Field(
        default=None, description="Resolved count at send time (optional)."
    )
    model_config = ConfigDict(from_attributes=True, extra="forbid")

