# backend/schemas/settings.py
from __future__ import annotations

from enum import Enum
from typing import Optional, Dict, Any, List
from datetime import datetime, timezone
import re

# --- Pydantic v2 kwanza, v1 fallback (shim fupi & nyepesi) -------------------
_V2 = True
try:
    from pydantic import BaseModel, Field, ConfigDict, field_validator
except Exception:  # Pydantic v1
    _V2 = False
    from pydantic import BaseModel, Field  # type: ignore
    from pydantic import validator as _v  # type: ignore

    class ConfigDict(dict):  # type: ignore
        pass
    def field_validator(field_name: str, *, mode: str = "after"):  # type: ignore
        pre = (mode == "before")
        def deco(fn):
            return _v(field_name, pre=pre, allow_reuse=True)(fn)  # type: ignore
        return deco

# zoneinfo ya hiari kwa uhakiki wa timezone
try:
    from zoneinfo import ZoneInfo  # py>=3.9
except Exception:  # pragma: no cover
    ZoneInfo = None  # type: ignore


# ------------------------------- Enums --------------------------------------
class Theme(str, Enum):
    light = "light"
    dark = "dark"
    system = "system"

class Channel(str, Enum):
    email = "email"
    sms = "sms"
    push = "push"
    whatsapp = "whatsapp"
    telegram = "telegram"
    inapp = "inapp"

class TimeFormat(str, Enum):
    h12 = "12h"
    h24 = "24h"

class DateFormat(str, Enum):
    ymd = "YYYY-MM-DD"
    dmy = "DD/MM/YYYY"
    mdy = "MM/DD/YYYY"


# ------------------------------- Helpers ------------------------------------
_hex_re = re.compile(r"^#(?:[0-9A-Fa-f]{6}|[0-9A-Fa-f]{3})$")
_key_re = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
_url_re = re.compile(r"^https?://", re.IGNORECASE)

def _trim(v: Optional[str]) -> Optional[str]:
    if v is None:
        return None
    s = str(v).strip()
    return s or None

def _norm_lang(v: Optional[str]) -> Optional[str]:
    if v is None:
        return None
    s = str(v).strip().replace("_", "-")
    if not s:
        return None
    parts = s.split("-")
    if not parts or not parts[0].isalpha() or not (2 <= len(parts[0]) <= 3):
        raise ValueError("invalid language code")
    parts[0] = parts[0].lower()
    if len(parts) >= 2 and parts[1].isalpha() and len(parts[1]) in (2, 3):
        parts[1] = parts[1].upper()
    s = "-".join(p for p in parts if p)
    return s if len(s) <= 15 else s[:15]

def _norm_tz(v: Optional[str]) -> Optional[str]:
    s = _trim(v)
    if not s:
        return None
    if ZoneInfo:
        try:
            ZoneInfo(s)
        except Exception:
            raise ValueError("invalid timezone")
    return s

def _norm_meta(d: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if d is None:
        return None
    out: Dict[str, Any] = {}
    for k, val in dict(d).items():
        k2 = str(k).strip()
        if not _key_re.match(k2):
            raise ValueError(f"invalid key: {k}")
        out[k2] = val
    return out

def _to_utc(v):
    if v is None:
        return None
    if isinstance(v, str):
        v = datetime.fromisoformat(v.replace("Z", "+00:00"))
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    raise ValueError("invalid datetime")


# ------------------------------- Submodels ----------------------------------
class Branding(BaseModel):
    """Uwekaji wa chapa (nyepesi)."""
    logo_url: Optional[str] = Field(default=None, max_length=2048)
    primary_color: Optional[str] = Field(default=None, description="#RRGGBB au #RGB")
    secondary_color: Optional[str] = Field(default=None, description="#RRGGBB au #RGB")

    if _V2:
        model_config = ConfigDict(extra="forbid")

    @field_validator("logo_url", mode="before")
    def _url(cls, v):
        s = _trim(v)
        if s and not _url_re.match(s):
            raise ValueError("logo_url must start with http(s)://")
        return s

    @field_validator("primary_color", "secondary_color", mode="before")
    def _hex(cls, v):
        s = _trim(v)
        if s and not _hex_re.match(s):
            raise ValueError("color must be #RRGGBB or #RGB")
        return s


class NotificationPrefs(BaseModel):
    """Mipendeleo ya arifa (mobile-first)."""
    enabled: Optional[bool] = True
    channels: Optional[List[Channel]] = Field(
        default=None, description="Ikiwa None, tumia chaguo-msingi za app."
    )
    quiet_hours_start: Optional[str] = Field(
        default=None, description="HH:MM (24h), hiari", pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$"
    )
    quiet_hours_end: Optional[str] = Field(
        default=None, description="HH:MM (24h), hiari", pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$"
    )

    if _V2:
        model_config = ConfigDict(extra="forbid")

    @field_validator("channels", mode="before")
    def _norm_channels(cls, v):
        if v is None:
            return None
        out: List[Channel] = []
        for x in v:
            sx = str(x).strip().lower()
            try:
                ch = Channel(sx)
            except Exception:
                continue
            if ch not in out:
                out.append(ch)
        return out or None


# ------------------------------- Base Model ---------------------------------
class SettingsBase(BaseModel):
    company_name: Optional[str] = Field(default=None, max_length=120)
    domain: Optional[str] = Field(default=None, max_length=120, description="ex: example.com")
    timezone: Optional[str] = Field(default=None, description="ex: Africa/Dar_es_Salaam")
    language: Optional[str] = Field(default=None, description="IETF tag: 'sw', 'en-US'")
    theme: Optional[Theme] = None

    # Arifa & chapa
    notifications: Optional[NotificationPrefs] = None
    branding: Optional[Branding] = None

    # AI & bot
    bot_voice: Optional[str] = Field(default=None, max_length=64, pattern=r"^[A-Za-z0-9._-]+$")
    ai_provider: Optional[str] = Field(default=None, max_length=64)
    ai_model: Optional[str] = Field(default=None, max_length=64)
    ai_temperature: Optional[float] = Field(default=None, ge=0.0, le=2.0)

    # Fedha & tarehe
    currency: Optional[str] = Field(default="TZS", pattern=r"^[A-Z]{3}$")
    date_format: Optional[DateFormat] = None
    time_format: Optional[TimeFormat] = None

    # Faragha & hifadhi
    analytics_opt_in: Optional[bool] = None
    mask_pii: Optional[bool] = None
    store_conversations: Optional[bool] = None
    data_retention_days: Optional[int] = Field(default=None, ge=1, le=3650)

    # Ziada
    extra: Optional[Dict[str, Any]] = None

    if _V2:
        model_config = ConfigDict(
            extra="forbid",
            populate_by_name=True,
            json_schema_extra={
                "example": {
                    "company_name": "SmartBiz",
                    "domain": "smartbiz.co.tz",
                    "timezone": "Africa/Dar_es_Salaam",
                    "language": "sw",
                    "theme": "system",
                    "notifications": {"enabled": True, "channels": ["push", "email"]},
                    "branding": {"logo_url": "https://cdn.example.com/logo.png", "primary_color": "#0ea5e9"},
                    "bot_voice": "female_01",
                    "ai_provider": "openai",
                    "ai_model": "gpt-4o-mini",
                    "ai_temperature": 0.3,
                    "currency": "TZS",
                    "date_format": "YYYY-MM-DD",
                    "time_format": "24h",
                    "analytics_opt_in": True,
                    "mask_pii": True,
                    "store_conversations": False,
                    "data_retention_days": 365,
                    "extra": {"beta": True}
                }
            },
        )
    else:
        class Config:  # type: ignore
            extra = "forbid"
            allow_population_by_field_name = True
            schema_extra = {
                "example": {
                    "company_name": "SmartBiz",
                    "domain": "smartbiz.co.tz",
                    "timezone": "Africa/Dar_es_Salaam",
                    "language": "sw",
                    "theme": "system",
                    "notifications": {"enabled": True, "channels": ["push", "email"]},
                    "branding": {"logo_url": "https://cdn.example.com/logo.png", "primary_color": "#0ea5e9"},
                    "bot_voice": "female_01",
                    "ai_provider": "openai",
                    "ai_model": "gpt-4o-mini",
                    "ai_temperature": 0.3,
                    "currency": "TZS",
                    "date_format": "YYYY-MM-DD",
                    "time_format": "24h",
                    "analytics_opt_in": True,
                    "mask_pii": True,
                    "store_conversations": False,
                    "data_retention_days": 365,
                    "extra": {"beta": True}
                }
            }

    # -------------------------- Validators (nyepesi) --------------------------
    @field_validator("company_name", mode="before")
    def _name(cls, v):
        s = _trim(v)
        return " ".join(s.split()) if s else None

    @field_validator("domain", mode="before")
    def _domain(cls, v):
        s = _trim(v)
        if s and (" " in s or "/" in s):
            raise ValueError("invalid domain")
        return s

    @field_validator("timezone", mode="before")
    def _tz(cls, v):
        return _norm_tz(v)

    @field_validator("language", mode="before")
    def _lang(cls, v):
        return _norm_lang(v)

    @field_validator("ai_provider", "ai_model", "bot_voice", mode="before")
    def _trim_small(cls, v):
        return _trim(v)

    @field_validator("currency", mode="before")
    def _curr(cls, v):
        if v is None:
            return "TZS"
        s = str(v).strip().upper()
        if len(s) != 3:
            raise ValueError("currency must be 3 letters")
        return s

    @field_validator("extra", mode="before")
    def _extra_ok(cls, v):
        return _norm_meta(v)


class SettingsCreate(SettingsBase):
    """Kila kitu ni hiari kwa urahisi wa mobile; weka lazima upande wa service kama utahitaji."""
    pass


class SettingsOut(SettingsBase):
    id: int
    owner_id: Optional[int] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    if _V2:
        model_config = ConfigDict(
            extra="forbid",
            from_attributes=True,  # v2
        )
    else:
        class Config:  # type: ignore
            extra = "forbid"  # hakuna neno lililokatazwa

    @field_validator("created_at", "updated_at", mode="before")
    def _dt(cls, v):
        return _to_utc(v)
