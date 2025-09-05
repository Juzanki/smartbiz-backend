# backend/schemas/post.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import re
from typing import Optional, List, Dict, Any
from datetime import datetime, timezone, timedelta

# --- Pydantic v2 kwanza; v1 fallback (shim fupi & nyepesi) -------------------
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


# ------------------------------- Enums ---------------------------------------
from enum import Enum

class Visibility(str, Enum):
    public = "public"
    unlisted = "unlisted"
    private = "private"


# ------------------------------ Helpers --------------------------------------
_slug_re = re.compile(r"[^a-z0-9-]+")
_tag_re  = re.compile(r"^[a-z0-9._-]{1,32}$")

def _slugify(s: str) -> str:
    s = s.strip().lower()
    s = re.sub(r"\s+", "-", s)
    s = _slug_re.sub("", s)
    s = s.strip("-")
    return s[:96] or "post"

def _to_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


# ------------------------------- Models --------------------------------------
class PostBase(BaseModel):
    title: str = Field(..., min_length=1, max_length=160)
    content: str = Field("", max_length=200000)  # nyepesi lakini inatosha makala ndefu
    is_published: bool = True

    # vipengele vya hiari lakini muhimu kwa upana mzuri bila kuwa nzito
    summary: Optional[str] = Field(default=None, max_length=300)
    slug: Optional[str] = Field(default=None, max_length=96, description="url-friendly")
    tags: Optional[List[str]] = Field(default=None, description="lowercase, deduped")
    language: Optional[str] = Field(default=None, max_length=15, description="IETF tag e.g. 'sw', 'en-US'")
    cover_url: Optional[str] = Field(default=None, max_length=2048)
    visibility: Visibility = Visibility.public
    metadata: Optional[Dict[str, Any]] = None  # key filter ipo chini

    if _V2:
        model_config = ConfigDict(
            extra="forbid",
            populate_by_name=True,
            json_schema_extra={
                "example": {
                    "title": "Habari Njema za SmartBiz",
                    "content": "Yaliyomo ya makala...",
                    "is_published": True,
                    "summary": "Muhtasari mfupi wa makala.",
                    "slug": "habari-njema-za-smartbiz",
                    "tags": ["smartbiz", "live", "ai"],
                    "language": "sw",
                    "cover_url": "https://cdn.example.com/covers/abc.png",
                    "visibility": "public",
                }
            },
        )
    else:
        class Config:  # type: ignore
            extra = "forbid"
            allow_population_by_field_name = True
            schema_extra = {
                "example": {
                    "title": "Habari Njema za SmartBiz",
                    "content": "Yaliyomo ya makala...",
                    "is_published": True,
                    "summary": "Muhtasari mfupi wa makala.",
                    "slug": "habari-njema-za-smartbiz",
                    "tags": ["smartbiz", "live", "ai"],
                    "language": "sw",
                    "cover_url": "https://cdn.example.com/covers/abc.png",
                    "visibility": "public",
                }
            }

    # ------------------------- Field validators -------------------------------
    @field_validator("title", mode="before")
    def _title_clean(cls, v):
        s = str(v or "").strip()
        if not s:
            raise ValueError("title is required")
        return s

    @field_validator("content", mode="before")
    def _content_norm(cls, v):
        return str(v or "").rstrip()

    @field_validator("summary", mode="before")
    def _summary_trim(cls, v):
        if v is None:
            return None
        s = " ".join(str(v).split())
        return s or None

    @field_validator("slug", mode="before")
    def _slug_norm(cls, v, info):
        if v is None:
            # auto from title if available
            src = info.data.get("title") or ""
            return _slugify(str(src))
        s = str(v)
        return _slugify(s)

    @field_validator("tags", mode="before")
    def _tags_norm(cls, v):
        if v is None:
            return None
        out: List[str] = []
        for t in (v if isinstance(v, (list, tuple)) else [v]):
            s = str(t).strip().lower()
            if not s:
                continue
            if not _tag_re.match(s):
                raise ValueError(f"invalid tag: {s}")
            if s not in out:
                out.append(s)
        return out or None

    @field_validator("language", mode="before")
    def _lang_norm(cls, v):
        if v is None:
            return None
        s = str(v).strip().replace("_", "-")
        parts = s.split("-")
        if not parts or not parts[0].isalpha() or not (2 <= len(parts[0]) <= 3):
            raise ValueError("invalid language code")
        parts[0] = parts[0].lower()
        if len(parts) >= 2 and parts[1].isalpha() and len(parts[1]) in (2, 3):
            parts[1] = parts[1].upper()
        s = "-".join(p for p in parts if p)
        return s if len(s) <= 15 else s[:15]

    @field_validator("cover_url", mode="before")
    def _cover_trim(cls, v):
        if v is None:
            return None
        s = str(v).strip()
        return s or None

    @field_validator("metadata", mode="before")
    def _meta_filter(cls, v):
        if v is None:
            return None
        out: Dict[str, Any] = {}
        key_re = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
        for k, val in dict(v).items():
            k2 = str(k).strip()
            if not key_re.match(k2):
                raise ValueError(f"invalid metadata key: {k}")
            out[k2] = val
        return out


class PostCreate(PostBase):
    owner_id: Optional[int] = Field(default=None, ge=1)
    scheduled_at: Optional[datetime] = None  # ukiweka, lazima iwe siku zijazo

    @field_validator("owner_id", mode="before")
    def _owner_id_ok(cls, v):
        if v in (None, ""):
            return None
        try:
            iv = int(v)
        except Exception:
            raise ValueError("owner_id must be an integer")
        if iv < 1:
            raise ValueError("owner_id must be >= 1")
        return iv

    @field_validator("scheduled_at", mode="before")
    def _sched_norm(cls, v):
        if v is None:
            return None
        if isinstance(v, str):
            v = datetime.fromisoformat(v.replace("Z", "+00:00"))
        if not isinstance(v, datetime):
            raise ValueError("scheduled_at must be a datetime or ISO string")
        if _to_utc(v) < datetime.now(timezone.utc) - timedelta(seconds=2):
            raise ValueError("scheduled_at must be in the future")
        return _to_utc(v)


class PostUpdate(BaseModel):
    # toleo la patch — kila kitu ni hiari
    title: Optional[str] = Field(default=None, min_length=1, max_length=160)
    content: Optional[str] = Field(default=None, max_length=200000)
    is_published: Optional[bool] = None
    summary: Optional[str] = Field(default=None, max_length=300)
    slug: Optional[str] = Field(default=None, max_length=96)
    tags: Optional[List[str]] = None
    language: Optional[str] = Field(default=None, max_length=15)
    cover_url: Optional[str] = Field(default=None, max_length=2048)
    visibility: Optional[Visibility] = None
    metadata: Optional[Dict[str, Any]] = None
    scheduled_at: Optional[datetime] = None

    if _V2:
        model_config = ConfigDict(extra="forbid")
    else:
        class Config:  # type: ignore
            extra = "forbid"

    # tumia validators zilezile nyepesi
    _title = PostBase.__validators__["title"] if not _V2 else None  # type: ignore[attr-defined]
    _content = PostBase.__validators__["content"] if not _V2 else None  # type: ignore
    _summary = PostBase.__validators__["summary"] if not _V2 else None  # type: ignore
    _slug = PostBase.__validators__["slug"] if not _V2 else None  # type: ignore
    _tags = PostBase.__validators__["tags"] if not _V2 else None  # type: ignore
    _lang = PostBase.__validators__["language"] if not _V2 else None  # type: ignore
    _cover = PostBase.__validators__["cover_url"] if not _V2 else None  # type: ignore
    _meta = PostBase.__validators__["metadata"] if not _V2 else None  # type: ignore

    @field_validator("title", mode="before")
    def _title_clean(cls, v):  # v2 path
        if v is None: return None
        s = str(v).strip()
        if not s: return None
        return s

    @field_validator("slug", mode="before")
    def _slug_norm(cls, v):
        if v is None: return None
        return _slugify(str(v))

    @field_validator("tags", mode="before")
    def _tags_norm(cls, v):
        if v is None: return None
        out: List[str] = []
        for t in (v if isinstance(v, (list, tuple)) else [v]):
            s = str(t).strip().lower()
            if s and _tag_re.match(s) and s not in out:
                out.append(s)
        return out or None

    @field_validator("language", mode="before")
    def _lang_norm(cls, v):
        if v is None: return None
        s = str(v).strip().replace("_", "-")
        parts = s.split("-")
        if not parts or not parts[0].isalpha() or not (2 <= len(parts[0]) <= 3):
            raise ValueError("invalid language code")
        parts[0] = parts[0].lower()
        if len(parts) >= 2 and parts[1].isalpha() and len(parts[1]) in (2, 3):
            parts[1] = parts[1].upper()
        s = "-".join(p for p in parts if p)
        return s if len(s) <= 15 else s[:15]

    @field_validator("scheduled_at", mode="before")
    def _sched_norm(cls, v):
        if v is None: return None
        if isinstance(v, str):
            v = datetime.fromisoformat(v.replace("Z", "+00:00"))
        if not isinstance(v, datetime):
            raise ValueError("scheduled_at must be a datetime or ISO string")
        if _to_utc(v) < datetime.now(timezone.utc) - timedelta(seconds=2):
            raise ValueError("scheduled_at must be in the future")
        return _to_utc(v)


class PostOut(PostBase):
    id: int
    owner_id: Optional[int] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    published_at: Optional[datetime] = None
    likes_count: Optional[int] = Field(default=0, ge=0)
    comments_count: Optional[int] = Field(default=0, ge=0)
    reading_time_seconds: Optional[int] = Field(default=None, ge=0)

    if _V2:
        model_config = ConfigDict(
            extra="forbid",
            from_attributes=True,  # v2: ruhusu kujenga kutoka kwa object zenye attributes
        )
    else:
        class Config:  # type: ignore
            extra = "forbid"  # hakuna matumizi ya neno ulilokataza

    @field_validator("created_at", "updated_at", "published_at", mode="before")
    def _dt_norm(cls, v):
        if v is None:
            return None
        if isinstance(v, str):
            v = datetime.fromisoformat(v.replace("Z", "+00:00"))
        if not isinstance(v, datetime):
            raise ValueError("invalid datetime")
        return _to_utc(v)
