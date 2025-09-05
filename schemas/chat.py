# backend/schemas/chat.py
from __future__ import annotations

from enum import Enum
from typing import Optional, List, Dict, Any
from datetime import datetime, timezone, timedelta
import re

# ---------- Pydantic v2 first, fallback to v1 (compact shims) ---------------
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


# ------------------------------- ENUMS --------------------------------------
class Role(str, Enum):
    user = "user"
    assistant = "assistant"
    system = "system"


class Channel(str, Enum):
    web = "web"
    whatsapp = "whatsapp"
    sms = "sms"
    email = "email"
    telegram = "telegram"


# ------------------------------ MODELS --------------------------------------
class Attachment(BaseModel):
    """Rich attachment (file/media)."""
    # Allow simple URL-only attachments by setting filename optional
    url: str = Field(..., min_length=1, description="Public/signed URL of the attachment.")
    filename: Optional[str] = Field(default=None, max_length=200)
    mime_type: Optional[str] = Field(default=None, max_length=100)
    size_bytes: Optional[int] = Field(default=None, ge=0)

    if _V2:
        model_config = ConfigDict(extra="forbid")


class ChatBase(BaseModel):
    message: str = Field(..., min_length=1, max_length=8000)
    conversation_id: Optional[int] = Field(
        default=None, description="Conversation/thread numeric id."
    )
    role: Role = Role.user
    channel: Channel = Channel.web
    attachments: Optional[List[Attachment]] = None
    metadata: Optional[Dict[str, Any]] = None  # arbitrary context (keys validated)

    if _V2:
        model_config = ConfigDict(
            extra="forbid",
            populate_by_name=True,
            json_schema_extra={
                "example": {
                    "message": "Hello ðŸ‘‹",
                    "conversation_id": 123,
                    "role": "user",
                    "channel": "web",
                    "attachments": [{"url": "https://cdn.example.com/file.png", "mime_type": "image/png"}],
                    "metadata": {"app_ver": "1.2.3", "lang": "sw"},
                }
            },
        )
    else:
        class Config:  # type: ignore
            extra = "forbid"
            allow_population_by_field_name = True
            schema_extra = {
                "example": {
                    "message": "Hello ðŸ‘‹",
                    "conversation_id": 123,
                    "role": "user",
                    "channel": "web",
                    "attachments": [{"url": "https://cdn.example.com/file.png", "mime_type": "image/png"}],
                    "metadata": {"app_ver": "1.2.3", "lang": "sw"},
                }
            }

    # -------------------------- FIELD VALIDATORS -----------------------------

    @field_validator("message", mode="before")
    def _clean_message(cls, v):
        if v is None:
            raise ValueError("message is required")
        s = str(v).strip()
        if not s:
            raise ValueError("message cannot be empty")
        return s

    @field_validator("conversation_id", mode="before")
    def _coerce_conversation_id(cls, v):
        if v in (None, ""):
            return None
        try:
            iv = int(v)
        except Exception:
            raise ValueError("conversation_id must be an integer")
        if iv <= 0:
            raise ValueError("conversation_id must be positive")
        return iv

    @field_validator("attachments", mode="before")
    def _normalize_attachments(cls, v):
        if v is None:
            return None
        # accept list of strings -> treat as url-only attachments
        out: List[Attachment] = []
        if isinstance(v, (list, tuple)):
            for item in v:
                if isinstance(item, str):
                    url = item.strip()
                    if url:
                        out.append({"url": url})  # Attachment will parse it
                elif isinstance(item, dict):
                    out.append(item)
        # dedupe by (url, filename)
        seen = set()
        uniq = []
        for a in out:
            key = (a.get("url") if isinstance(a, dict) else a.url, a.get("filename") if isinstance(a, dict) else a.filename)
            if key not in seen:
                seen.add(key)
                uniq.append(a)
        return uniq or None

    @field_validator("metadata", mode="before")
    def _validate_metadata(cls, v):
        if v is None:
            return None
        out: Dict[str, Any] = {}
        key_re = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
        for k, val in dict(v).items():
            k2 = str(k).strip()
            if not key_re.match(k2):
                raise ValueError(f"Invalid metadata key: {k}")
            out[k2] = val
        return out

    # ------------------------ MODEL-LEVEL VALIDATION -------------------------
    if _V2:
        @model_validator(mode="after")
        def _check(self):
            # SMS: no attachments
            if self.channel == Channel.sms and self.attachments:
                raise ValueError("attachments are not allowed for SMS channel")
            return self
    else:
        @model_validator(mode="after")  # mapped to root_validator in v1
        def _check(cls, values):  # type: ignore
            if values.get("channel") == Channel.sms and values.get("attachments"):
                raise ValueError("attachments are not allowed for SMS channel")
            return values


class ChatCreate(ChatBase):
    """Client â†’ Server payload when creating a message."""
    idempotency_key: Optional[str] = Field(
        default=None, description="Use to de-duplicate client retries."
    )


class ChatOut(ChatBase):
    """Server â†’ Client response."""
    id: int
    created_at: Optional[datetime] = None

    # normalize created_at to UTC if provided
    @field_validator("created_at", mode="before")
    def _to_utc(cls, v):
        if v is None:
            return None
        if isinstance(v, str):
            v = datetime.fromisoformat(v.replace("Z", "+00:00"))
        if not isinstance(v, datetime):
            raise ValueError("created_at must be a datetime or ISO string")
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    model_config = ConfigDict(from_attributes=True, extra="forbid")

