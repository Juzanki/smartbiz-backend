# backend/schemas/platforms.py
from __future__ import annotations

from enum import Enum
from typing import Optional, Dict, Any, List
import re
from datetime import datetime, timezone

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


# ------------------------------ Base (forbid extras) ------------------------
class _Base(BaseModel):
    if _V2:
        model_config = ConfigDict(extra="forbid")
    else:
        class Config:  # type: ignore
            extra = "forbid"


# ------------------------------ Enums & Aliases -----------------------------
class Platform(str, Enum):
    facebook = "facebook"
    instagram = "instagram"
    whatsapp = "whatsapp"
    x = "x"              # (ex-Twitter)
    tiktok = "tiktok"
    youtube = "youtube"
    telegram = "telegram"
    linkedin = "linkedin"
    threads = "threads"
    web = "web"
    custom = "custom"

# Aliases nyepesi (inputâ†’enum)
_PLATFORM_ALIAS = {
    "fb": "facebook",
    "ig": "instagram",
    "wa": "whatsapp",
    "twitter": "x",
    "yt": "youtube",
    "li": "linkedin",
}


# ------------------------------ Helpers -------------------------------------
_key_re = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
_id_re = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")
_url_re = re.compile(r"^https?://", re.IGNORECASE)

def _norm_platform(v) -> Platform:
    s = str(v or "").strip().lower()
    s = _PLATFORM_ALIAS.get(s, s)
    try:
        return Platform(s)
    except Exception:
        raise ValueError("unsupported platform")

def _trim(v: Optional[str]) -> Optional[str]:
    if v is None:
        return None
    s = str(v).strip()
    return s or None

def _norm_meta(v: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for k, val in dict(v).items():
        k2 = str(k).strip()
        if not _key_re.match(k2):
            raise ValueError(f"invalid metadata key: {k}")
        out[k2] = val
    return out

def _to_dt(v):
    if v is None:
        return None
    if isinstance(v, str):
        v = datetime.fromisoformat(v.replace("Z", "+00:00"))
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    raise ValueError("invalid datetime value")


# ------------------------------ Schemas -------------------------------------
class PlatformConnectRequest(_Base):
    """
    Ombi la kuunganisha akaunti/ukurasa na jukwaa fulani.
    Angalau mojawapo kati ya: access_token | api_key | webhook_url.
    """
    platform: Platform
    access_token: Optional[str] = Field(default=None, max_length=4096)
    api_key: Optional[str] = Field(default=None, max_length=2048)
    page_id: Optional[str] = Field(default=None, max_length=64)
    account_id: Optional[str] = Field(default=None, max_length=64)
    webhook_url: Optional[str] = Field(default=None, max_length=2048)
    # tumia hili kwa kusaini webhook (usiweke siri yenyewe kwenye response)
    secret: Optional[str] = Field(default=None, max_length=4096)
    metadata: Optional[Dict[str, Any]] = None
    idempotency_key: Optional[str] = Field(default=None, max_length=64)

    if _V2:
        model_config = ConfigDict(
            extra="forbid",
            json_schema_extra={
                "example": {
                    "platform": "facebook",
                    "access_token": "EAAB...snip",
                    "page_id": "1234567890",
                    "webhook_url": "https://api.example.com/webhooks/fb",
                    "metadata": {"app_ver": "1.0.0"},
                    "idempotency_key": "2b1d9b9c-d4c8-4cfa-9a7e-2b88a9e0a111"
                }
            },
        )
    else:
        class Config(_Base.Config):  # type: ignore
            schema_extra = {
                "example": {
                    "platform": "facebook",
                    "access_token": "EAAB...snip",
                    "page_id": "1234567890",
                    "webhook_url": "https://api.example.com/webhooks/fb",
                    "metadata": {"app_ver": "1.0.0"},
                    "idempotency_key": "2b1d9b9c-d4c8-4cfa-9a7e-2b88a9e0a111"
                }
            }

    # ---------- validators (nyepesi) ----------
    @field_validator("platform", mode="before")
    def _plat(cls, v):
        return _norm_platform(v)

    @field_validator("access_token", "api_key", "secret", mode="before")
    def _trim_big(cls, v):
        return _trim(v)

    @field_validator("page_id", "account_id", mode="before")
    def _id_ok(cls, v):
        s = _trim(v)
        if s is None:
            return None
        if not _id_re.match(s):
            raise ValueError("invalid id format")
        return s

    @field_validator("webhook_url", mode="before")
    def _url_ok(cls, v):
        s = _trim(v)
        if s is None:
            return None
        if not _url_re.match(s):
            raise ValueError("webhook_url must start with http:// or https://")
        return s

    @field_validator("metadata", mode="before")
    def _meta_ok(cls, v):
        if v is None:
            return None
        return _norm_meta(v)

    @field_validator("idempotency_key", mode="before")
    def _idemp(cls, v):
        return _trim(v)

    if _V2:
        @model_validator(mode="after")
        def _one_credential(self):
            # lazima kuwe na angalau mojawapo
            if not (self.access_token or self.api_key or self.webhook_url):
                raise ValueError("provide at least one of: access_token, api_key, webhook_url")
            # ukitumia webhook_url, ni vyema kuwe na 'secret' (signature verification)
            if self.webhook_url and not self.secret:
                # bado tunaruhusu, lakini tunasisitiza usalama
                # To keep it strict & secure, unaweza kubadilisha kuwa error:
                # raise ValueError("secret is required when webhook_url is provided")
                pass
            return self
    else:
        @model_validator(mode="after")  # mapped to root_validator in v1
        def _one_credential(cls, values):  # type: ignore
            if not (values.get("access_token") or values.get("api_key") or values.get("webhook_url")):
                raise ValueError("provide at least one of: access_token, api_key, webhook_url")
            return values


class PlatformOut(_Base):
    id: Optional[int] = None
    platform: Platform
    is_connected: bool = True
    account_name: Optional[str] = Field(default=None, max_length=120)
    account_id: Optional[str] = Field(default=None, max_length=64)
    # taarifa salama za tathmini ya muunganiko
    scopes: Optional[List[str]] = None
    connected_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None
    webhook_url: Optional[str] = None
    webhook_secret_set: bool = False
    metadata: Optional[Dict[str, Any]] = None

    @field_validator("platform", mode="before")
    def _plat(cls, v):
        return _norm_platform(v)

    @field_validator("account_name", mode="before")
    def _name_trim(cls, v):
        return _trim(v)

    @field_validator("account_id", mode="before")
    def _acc_ok(cls, v):
        s = _trim(v)
        if s is None:
            return None
        if not _id_re.match(s):
            raise ValueError("invalid account_id format")
        return s

    @field_validator("scopes", mode="before")
    def _scopes_norm(cls, v):
        if v is None:
            return None
        out: List[str] = []
        for s in v:
            s2 = str(s).strip().lower()
            if s2 and s2 not in out:
                out.append(s2)
        return out or None

    @field_validator("connected_at", "expires_at", mode="before")
    def _dt_norm(cls, v):
        return _to_dt(v)

    @field_validator("webhook_url", mode="before")
    def _url_ok(cls, v):
        s = _trim(v)
        if s is None:
            return None
        if not _url_re.match(s):
            raise ValueError("webhook_url must start with http:// or https://")
        return s

    @field_validator("metadata", mode="before")
    def _meta_ok(cls, v):
        if v is None:
            return None
        return _norm_meta(v)
    model_config = ConfigDict(from_attributes=True, extra="forbid")

