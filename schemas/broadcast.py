# backend/schemas/broadcast.py
from __future__ import annotations

from enum import Enum
from typing import Optional, List, Dict, Any, Literal
from datetime import datetime, timezone, timedelta
import re

# --- Pydantic v2 first, fallback to v1 (compact shims) ----------------------
_V2 = True
try:
    from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
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


# ------------------------------- ENUMS --------------------------------------
class Channel(str, Enum):
    email = "email"
    sms = "sms"
    push = "push"
    inapp = "inapp"
    whatsapp = "whatsapp"


# ------------------------------ MODELS --------------------------------------
class Attachment(BaseModel):
    """File attachment for email/push/inapp channels."""
    filename: str = Field(..., min_length=1, max_length=200)
    url: Optional[str] = Field(
        default=None,
        description="Public or signed URL of the attachment."
    )
    mime_type: Optional[str] = Field(default=None, max_length=100)
    size_bytes: Optional[int] = Field(default=None, ge=0)

    if _V2:
        model_config = ConfigDict(extra="forbid")


class BroadcastMessage(BaseModel):
    """
    A single broadcast request to one or more recipients, segments or everyone.
    """
    subject: Optional[str] = Field(
        default=None,
        description="Optional subject/headline (required for email)."
    )
    message: str = Field(..., min_length=1, description="Body text or template body.")
    channel: Optional[Channel] = Field(default=None)  # allow auto-routing if None

    # Targeting
    recipients: Optional[List[str]] = Field(
        default=None, description="User IDs/emails/phones (depends on channel)."
    )
    segments: Optional[List[str]] = Field(
        default=None, description="Named audience segments/tags."
    )
    send_to_all: bool = False

    # Delivery
    schedule_at: Optional[datetime] = Field(
        default=None, description="UTC schedule time (must be in the future)."
    )
    priority: Literal["low", "normal", "high"] = "normal"
    idempotency_key: Optional[str] = Field(
        default=None,
        description="Use to de-duplicate client retries."
    )

    # Extras
    variables: Optional[Dict[str, Any]] = Field(
        default=None, description="Template variables for rendering."
    )
    attachments: Optional[List[Attachment]] = None
    metadata: Optional[Dict[str, Any]] = None

    if _V2:
        model_config = ConfigDict(
            extra="forbid",
            populate_by_name=True,
            json_schema_extra={
                "example": {
                    "subject": "Weekend Offer",
                    "message": "Hello {{first_name}}, enjoy -20% this weekend!",
                    "channel": "email",
                    "recipients": ["user1@example.com", "user2@example.com"],
                    "schedule_at": "2025-08-21T12:00:00Z",
                    "priority": "normal",
                    "variables": {"first_name": "Julius"},
                    "attachments": [{"filename": "flyer.pdf", "url": "https://.../flyer.pdf"}],
                    "metadata": {"campaign": "wknd-2025-34"},
                    "idempotency_key": "bdc7a9c6-6e4a-4e1c-9b7c-03da2b9b1b1a",
                }
            },
        )
    else:
        class Config:  # type: ignore
            extra = "forbid"
            allow_population_by_field_name = True
            schema_extra = {
                "example": {
                    "subject": "Weekend Offer",
                    "message": "Hello {{first_name}}, enjoy -20% this weekend!",
                    "channel": "email",
                    "recipients": ["user1@example.com", "user2@example.com"],
                    "schedule_at": "2025-08-21T12:00:00Z",
                    "priority": "normal",
                    "variables": {"first_name": "Julius"},
                    "attachments": [{"filename": "flyer.pdf", "url": "https://.../flyer.pdf"}],
                    "metadata": {"campaign": "wknd-2025-34"},
                    "idempotency_key": "bdc7a9c6-6e4a-4e1c-9b7c-03da2b9b1b1a",
                }
            }

    # -------------------------- FIELD VALIDATORS -----------------------------

    @field_validator("subject", mode="before")
    def _strip_subject(cls, v):
        if v is None:
            return v
        s = str(v).strip()
        return s or None

    @field_validator("message", mode="before")
    def _strip_message(cls, v):
        if v is None:
            raise ValueError("message is required")
        s = str(v).strip()
        if not s:
            raise ValueError("message cannot be empty")
        return s

    @field_validator("recipients", mode="before")
    def _normalize_recipients(cls, v):
        if v is None:
            return None
        # allow string "a,b,c" or list
        if isinstance(v, str):
            v = [p.strip() for p in v.split(",")]
        cleaned: List[str] = []
        for x in v:
            s = str(x).strip()
            if not s:
                continue
            cleaned.append(s)
        # dedupe, keep order
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
        cleaned = []
        for x in v:
            s = str(x).strip()
            if s:
                cleaned.append(s.lower())
        return cleaned or None

    @field_validator("variables", mode="before")
    def _normalize_vars(cls, v):
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

    @field_validator("schedule_at", mode="before")
    def _to_utc(cls, v):
        if v is None:
            return None
        if isinstance(v, str):
            # allow 'Z' timezone strings
            v = datetime.fromisoformat(v.replace("Z", "+00:00"))
        if not isinstance(v, datetime):
            raise ValueError("schedule_at must be a datetime or ISO string")
        if v.tzinfo is None:
            v = v.replace(tzinfo=timezone.utc)
        return v.astimezone(timezone.utc)

    # ------------------------ MODEL-LEVEL VALIDATION -------------------------
    if _V2:
        @model_validator(mode="after")
        def _check(self):
            # Target requirement
            if not (self.send_to_all or (self.recipients) or (self.segments)):
                raise ValueError("Provide at least one of: recipients, segments, or send_to_all=True")

            # Subject rules
            if self.channel == Channel.email and not self.subject:
                raise ValueError("subject is required for email channel")

            if self.subject:
                max_len = 160 if self.channel == Channel.sms else 255
                if len(self.subject) > max_len:
                    raise ValueError(f"subject too long for {self.channel or 'channel'} (max {max_len})")

            # Attachments only for non-SMS
            if self.attachments and self.channel == Channel.sms:
                raise ValueError("attachments are not allowed for SMS channel")

            # schedule must be in the future (allow 2s skew)
            if self.schedule_at and self.schedule_at < datetime.now(timezone.utc) - timedelta(seconds=2):
                raise ValueError("schedule_at must be in the future")

            return self
    else:
        @model_validator(mode="after")  # mapped to root_validator in v1
        def _check(cls, values):  # type: ignore
            send_to_all = values.get("send_to_all")
            recipients = values.get("recipients")
            segments = values.get("segments")
            channel = values.get("channel")
            subject = values.get("subject")
            attachments = values.get("attachments")
            schedule_at = values.get("schedule_at")

            if not (send_to_all or recipients or segments):
                raise ValueError("Provide at least one of: recipients, segments, or send_to_all=True")

            if channel == Channel.email and not subject:
                raise ValueError("subject is required for email channel")

            if subject:
                max_len = 160 if channel == Channel.sms else 255
                if len(subject) > max_len:
                    raise ValueError(f"subject too long for {channel or 'channel'} (max {max_len})")

            if attachments and channel == Channel.sms:
                raise ValueError("attachments are not allowed for SMS channel")

            if schedule_at and schedule_at < datetime.now(timezone.utc) - timedelta(seconds=2):
                raise ValueError("schedule_at must be in the future")

            return values
