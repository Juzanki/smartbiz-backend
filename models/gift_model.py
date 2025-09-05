# backend/models/gift.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import enum
import re
import datetime as dt
from decimal import Decimal
from typing import Optional, TYPE_CHECKING, Any

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
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship, validates
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.ext.mutable import MutableDict, MutableList

from backend.db import Base
from backend.models._types import JSON_VARIANT, DECIMAL_TYPE  # portable (PG: JSONB/NUMERIC)

# -------- Portable tags: PG -> ARRAY(VARCHAR); others -> JSON list[str] --------
from sqlalchemy import JSON as SA_JSON
try:
    from sqlalchemy.dialects.postgresql import ARRAY as PG_ARRAY  # type: ignore
    TAGS_TYPE = SA_JSON().with_variant(PG_ARRAY(String(40)), "postgresql")
except Exception:  # pragma: no cover
    TAGS_TYPE = SA_JSON()

if TYPE_CHECKING:
    from .gift_movement import GiftMovement  # ensure it back_populates="gift"

_slug_re = re.compile(r"[^a-z0-9\-]+")

def _slugify(v: str) -> str:
    v = (v or "").strip().lower()
    v = re.sub(r"\s+", "-", v)
    v = _slug_re.sub("", v)
    return re.sub(r"-{2,}", "-", v).strip("-")[:100]


class GiftRarity(str, enum.Enum):
    common    = "Common"
    uncommon  = "Uncommon"
    rare      = "Rare"
    epic      = "Epic"
    legendary = "Legendary"


class GiftCategory(str, enum.Enum):
    default = "default"
    festive = "festive"
    neon    = "neon"
    love    = "love"
    meme    = "meme"
    premium = "premium"
    other   = "other"


class Gift(Base):
    """
    Catalog ya “gift” (in-app).
    • Money-safe coins (DECIMAL/NUMERIC)
    • i18n + meta (mutable JSON)
    • Tags (PG: ARRAY, others: JSON list)
    • Availability window + purchase limits
    """
    __tablename__ = "gifts"
    __mapper_args__ = {"eager_defaults": True}

    # ---- Identity ----
    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    slug: Mapped[str] = mapped_column(String(100), nullable=False, unique=True, index=True)

    # ---- Classification ----
    rarity: Mapped[GiftRarity] = mapped_column(
        SQLEnum(GiftRarity, name="gift_rarity", native_enum=False, validate_strings=True),
        default=GiftRarity.common,
        nullable=False,
        index=True,
    )
    category: Mapped[GiftCategory] = mapped_column(
        SQLEnum(GiftCategory, name="gift_category", native_enum=False, validate_strings=True),
        default=GiftCategory.default,
        nullable=False,
        index=True,
    )

    # ---- Pricing (money-safe coin units) ----
    coins: Mapped[Decimal] = mapped_column(DECIMAL_TYPE, nullable=False, server_default=text("0"))

    # ---- Assets & content ----
    icon_path: Mapped[str] = mapped_column(String(512), nullable=False)      # e.g. /static/gifts/rose.svg
    animation_path: Mapped[Optional[str]] = mapped_column(String(512))
    description: Mapped[Optional[str]] = mapped_column(Text)

    # Mutable JSONs (detect in-place changes)
    i18n: Mapped[Optional[dict[str, Any]]] = mapped_column(MutableDict.as_mutable(JSON_VARIANT))
    meta: Mapped[Optional[dict[str, Any]]] = mapped_column(MutableDict.as_mutable(JSON_VARIANT))

    # Tags/labels for filtering & discovery (e.g., ["valentine","animated"])
    tags: Mapped[Optional[list[str]]] = mapped_column(MutableList.as_mutable(TAGS_TYPE))

    # UX / flags
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, index=True)
    display_order: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))

    # Availability window (optional)
    available_from: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True), index=True)
    available_until: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True), index=True)

    # Purchase limits (optional; 0 = unlimited)
    max_per_tx: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    max_per_day: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    min_app_version: Mapped[Optional[str]] = mapped_column(String(20))

    # Audit
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP"), nullable=False, index=True
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP"), onupdate=text("CURRENT_TIMESTAMP"), nullable=False
    )

    # ---- Relationships ----
    movements: Mapped[list["GiftMovement"]] = relationship(
        "GiftMovement",
        back_populates="gift",
        cascade="all, delete-orphan",
        lazy="selectin",
    )

    # ---- Hybrids ----
    @hybrid_property
    def is_available_now(self) -> bool:
        """Active + within availability window (if set)."""
        if not self.is_active:
            return False
        now = dt.datetime.now(dt.timezone.utc)
        if self.available_from and now < self.available_from:
            return False
        if self.available_until and now >= self.available_until:
            return False
        return True

    @hybrid_property
    def coins_int(self) -> int:
        """Convenience (round down to nearest int for UI badges)."""
        try:
            return int(Decimal(self.coins or 0))
        except Exception:
            return 0

    # ---- Helpers ----
    def set_price_coins(self, value: Decimal | int | float | str) -> None:
        v = value if isinstance(value, Decimal) else Decimal(str(value))
        self.coins = v if v >= 0 else Decimal("0")

    def activate(self) -> None:
        self.is_active = True

    def deactivate(self) -> None:
        self.is_active = False

    def set_availability(
        self,
        *,
        start: dt.datetime | None = None,
        end: dt.datetime | None = None,
    ) -> None:
        self.available_from = start
        self.available_until = end

    def add_tags(self, *items: str) -> None:
        cur = set(self.tags or [])
        for it in items:
            t = (it or "").strip().lower()
            if t:
                cur.add(t[:40])
        self.tags = sorted(cur) or None

    def translate(self, locale: str, *, name: Optional[str] = None, desc: Optional[str] = None) -> None:
        """Weka/boresha i18n kwa lugha husika (e.g. 'sw', 'en-US')."""
        loc = (locale or "").strip()[:20]
        if not loc:
            return
        data = dict(self.i18n or {})
        entry = dict(data.get(loc) or {})
        if name is not None:
            entry["name"] = str(name)[:120]
        if desc is not None:
            entry["desc"] = str(desc)[:500]
        data[loc] = entry
        self.i18n = data

    # ---- Validators ----
    @validates("name")
    def _val_name(self, _k: str, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("Gift.name cannot be empty")
        # Auto-slug only when not explicitly set yet
        if not getattr(self, "slug", None):
            self.slug = _slugify(v)
        return v[:120]

    @validates("slug")
    def _val_slug(self, _k: str, v: str) -> str:
        s = _slugify(v)
        if not s:
            raise ValueError("Gift.slug invalid")
        return s

    @validates("icon_path", "animation_path")
    def _val_paths(self, _k: str, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        v = v.strip()
        if not v:
            return None
        if len(v) > 512:
            raise ValueError("Asset path too long")
        return v

    @validates("display_order", "max_per_tx", "max_per_day")
    def _val_nonneg_int(self, _k: str, v: int) -> int:
        iv = int(v or 0)
        if iv < 0:
            raise ValueError(f"{_k} must be >= 0")
        return iv

    @validates("available_from", "available_until")
    def _val_tzs(self, _k: str, v: Optional[dt.datetime]) -> Optional[dt.datetime]:
        # accept naive -> coerce to UTC (for safety you can enforce tz-aware upstream)
        if v is None:
            return None
        return v if v.tzinfo else v.replace(tzinfo=dt.timezone.utc)

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<Gift id={self.id} slug={self.slug!r} rarity={self.rarity} "
            f"coins={self.coins} active={self.is_active} available_now={self.is_available_now}>"
        )

    # ---- Indexes & Constraints ----
    __table_args__ = (
        UniqueConstraint("slug", name="uq_gift_slug"),
        Index("ix_gift_active_order", "is_active", "display_order"),
        Index("ix_gift_category_rarity", "category", "rarity"),
        Index("ix_gift_name_lower", func.lower(name)),
        Index("ix_gift_window", "available_from", "available_until"),
        CheckConstraint("display_order >= 0", name="ck_gift_order_nonneg"),
        CheckConstraint("coins >= 0", name="ck_gift_coins_nonneg"),
        CheckConstraint(
            "(available_until IS NULL) OR (available_from IS NULL) OR (available_until > available_from)",
            name="ck_gift_window_order",
        ),
        CheckConstraint("max_per_tx >= 0 AND max_per_day >= 0", name="ck_gift_limits_nonneg"),
    )
