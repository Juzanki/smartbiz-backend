# backend/models/filter.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import re
import datetime as dt
import enum
from typing import Optional

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Enum as SQLEnum,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, validates
from sqlalchemy.ext.hybrid import hybrid_property

from backend.db import Base
# Portable JSON (PG: JSONB; others: JSON) – hutoka kwenye _types yako
from backend.models._types import JSON_VARIANT

# -------- Slug helpers --------
_SLUG_RE = re.compile(r"[^a-z0-9\-]+")

def _slugify(value: str) -> str:
    """Badilisha jina → slug salama (lowercase, a-z0-9, hyphen)."""
    v = (value or "").strip().lower()
    v = re.sub(r"\s+", "-", v)
    v = _SLUG_RE.sub("", v)
    v = re.sub(r"-{2,}", "-", v).strip("-")
    return v[:100]


# -------- Enums --------
class FilterCategory(str, enum.Enum):
    color   = "color"
    retro   = "retro"
    neon    = "neon"
    glitch  = "glitch"
    beauty  = "beauty"
    bw      = "bw"
    other   = "other"


class Filter(Base):
    """
    Design filter (CSS/FX preset) inayotumika kwenye media/blocks.

    Inatunza:
      - `css_class`  : class ya CSS/renderer (mf. "fx-neon")
      - `css_vars`   : variables za mtindo (JSON) { "--hue": "180deg", ... }
      - `intensity`  : 0..200 kwa kudhibiti nguvu ya athari
      - metadata/UX  : description, preview_url, order, active flag

    Inafanya kazi kwa Postgres (JSONB) na SQLite (JSON) kupitia JSON_VARIANT.
    """
    __tablename__ = "filters"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)

    # Identity
    name: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    slug: Mapped[str] = mapped_column(String(100), nullable=False, unique=True, index=True)

    # Classification (portable enum)
    category: Mapped[FilterCategory] = mapped_column(
        SQLEnum(FilterCategory, name="filter_category", native_enum=False, validate_strings=True),
        default=FilterCategory.other,
        nullable=False,
        index=True,
    )

    # Implementation
    css_class: Mapped[str] = mapped_column(String(255), nullable=False)  # e.g., "fx-neon"
    css_vars: Mapped[Optional[dict]] = mapped_column(JSON_VARIANT)       # {"--hue":"180deg","--blur":"2px"}

    # UX / Control
    description: Mapped[Optional[str]] = mapped_column(String(200))
    preview_url: Mapped[Optional[str]] = mapped_column(String(512))
    intensity: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("100"))  # 0..200
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, index=True)
    display_order: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))

    # Audit
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP"), nullable=False, index=True
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("CURRENT_TIMESTAMP"),
        onupdate=text("CURRENT_TIMESTAMP"),
        nullable=False,
    )

    # ------- Hybrids -------
    @hybrid_property
    def is_visual(self) -> bool:
        return self.category in {
            FilterCategory.color, FilterCategory.retro, FilterCategory.neon,
            FilterCategory.glitch, FilterCategory.bw, FilterCategory.beauty
        }

    # ------- Helpers -------
    def activate(self) -> None:
        self.is_active = True

    def deactivate(self) -> None:
        self.is_active = False

    def bump_order(self, step: int = 1) -> None:
        self.display_order = (self.display_order or 0) + max(0, int(step))

    def set_css_var(self, key: str, value: str | int | float | None) -> None:
        """Sasisha thamani moja ya CSS variable (huunda dict kama haipo)."""
        data = dict(self.css_vars or {})
        if value is None:
            data.pop(key, None)
        else:
            data[str(key)] = str(value)
        self.css_vars = data

    def merge_css_vars(self, updates: dict[str, str | int | float]) -> None:
        """Unganisha (overwrite) variables nyingi kwa mara moja."""
        data = dict(self.css_vars or {})
        for k, v in (updates or {}).items():
            data[str(k)] = str(v)
        self.css_vars = data

    def inline_style(self) -> str:
        """
        Tengeneza style attribute inayoendana na css_vars.
        Mfano: '--hue:180deg; --blur:2px'
        """
        if not self.css_vars:
            return ""
        return "; ".join(f"{k}:{v}" for k, v in self.css_vars.items())

    # ------- Validators / normalizers -------
    @validates("name")
    def _val_name(self, _k: str, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("Filter.name cannot be empty")
        # Auto-slug kama haijawekwa bado
        if not getattr(self, "slug", None):
            self.slug = _slugify(v)
        return v

    @validates("slug")
    def _val_slug(self, _k: str, v: str) -> str:
        s = _slugify(v)
        if not s:
            raise ValueError("Filter.slug invalid")
        return s

    @validates("css_class")
    def _val_css_class(self, _k: str, v: str) -> str:
        v = (v or "").strip()
        if not re.fullmatch(r"[A-Za-z][\w\-\:]*", v):
            raise ValueError("Filter.css_class must be a valid CSS class token")
        return v

    @validates("intensity")
    def _val_intensity(self, _k: str, value: int) -> int:
        iv = int(value)
        return max(0, min(200, iv))

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Filter id={self.id} slug={self.slug!r} class={self.css_class!r} active={self.is_active}>"

    # ------- Constraints & Indexes -------
    __table_args__ = (
        UniqueConstraint("slug", name="uq_filter_slug"),
        Index("ix_filters_active_order", "is_active", "display_order"),
        Index("ix_filters_category", "category"),
        CheckConstraint("length(name) >= 2", name="ck_filter_name_len"),              # portable (SQLite/PG)
        CheckConstraint("display_order >= 0", name="ck_filter_order_nonneg"),
        CheckConstraint("intensity >= 0 AND intensity <= 200", name="ck_filter_intensity_range"),
    )
