# backend/models/bot_package.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import enum
import datetime as dt
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, TYPE_CHECKING, Dict, Any, List, Iterable

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Enum as SQLEnum,
    Index,
    Integer,
    String,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship, validates
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.event import listens_for

from backend.db import Base
# Portable types (PG -> JSONB/NUMERIC, others -> JSON/NUMERIC)
from backend.models._types import JSON_VARIANT, DECIMAL_TYPE

# JSON list default (portable); on PG we’ll store tags as ARRAY(VARCHAR(40))
from sqlalchemy import JSON as SA_JSON
try:
    from sqlalchemy.dialects.postgresql import ARRAY as PG_ARRAY  # type: ignore
    FEATURE_TAGS_TYPE = SA_JSON().with_variant(PG_ARRAY(String(40)), "postgresql")
except Exception:  # pragma: no cover
    FEATURE_TAGS_TYPE = SA_JSON()

if TYPE_CHECKING:
    from .user_bot import UserBot


# --------- Enums ---------
class PackageStatus(str, enum.Enum):
    active = "active"
    hidden = "hidden"
    deprecated = "deprecated"


class PackageTier(str, enum.Enum):
    starter = "starter"
    pro = "pro"
    business = "business"
    enterprise = "enterprise"


def _money(x: Decimal | int | float | str) -> Decimal:
    d = x if isinstance(x, Decimal) else Decimal(str(x))
    return d.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


# --------- Model ---------
class BotPackage(Base):
    """
    Subscription packages for UserBot.

    Money fields use DECIMAL_TYPE (PG: NUMERIC(18,2); others: Numeric(18,2))
    features: JSON (PG JSONB) via JSON_VARIANT
    feature_tags: JSON list (PG ARRAY(String(40)))
    """
    __tablename__ = "bot_packages"
    __mapper_args__ = {"eager_defaults": True}

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    # Identity
    slug: Mapped[str] = mapped_column(String(80), unique=True, index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(80), unique=True, nullable=False, index=True)
    description: Mapped[Optional[str]] = mapped_column(String(400))

    tier: Mapped[PackageTier] = mapped_column(
        SQLEnum(PackageTier, name="botpkg_tier", native_enum=False, validate_strings=True),
        default=PackageTier.starter,
        nullable=False,
        index=True,
    )
    status: Mapped[PackageStatus] = mapped_column(
        SQLEnum(PackageStatus, name="botpkg_status", native_enum=False, validate_strings=True),
        default=PackageStatus.active,
        nullable=False,
        index=True,
    )
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)

    # Prices (per-seat per month/year)
    price_monthly: Mapped[Decimal] = mapped_column(DECIMAL_TYPE, nullable=False, server_default=text("0"))
    price_yearly:  Mapped[Decimal] = mapped_column(DECIMAL_TYPE, nullable=False, server_default=text("0"))

    # Trial
    trial_days: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))

    # Feature flags / metadata (portable JSON)
    # e.g. {"voice_clone": true, "max_voice_minutes": 120}
    features: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON_VARIANT)

    # Tags (portable list[str])
    feature_tags: Mapped[Optional[List[str]]] = mapped_column(FEATURE_TAGS_TYPE)

    # Quotas & limits (examples)
    max_bots: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    max_concurrent_sessions: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    monthly_message_quota: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("5000"))
    monthly_minutes_quota: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("600"))
    overage_per_1000_msgs: Mapped[Decimal] = mapped_column(DECIMAL_TYPE, nullable=False, server_default=text("0"))
    overage_per_60_min:   Mapped[Decimal] = mapped_column(DECIMAL_TYPE, nullable=False, server_default=text("0"))

    # Audit
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    # Relations
    bots: Mapped[List["UserBot"]]= relationship(
        "UserBot",
        back_populates="package",
        cascade="all, delete-orphan",
        passive_deletes=True,
        lazy="selectin",
    )

    # --------- Hybrids / helpers ---------
    @hybrid_property
    def yearly_discount_pct(self) -> float:
        """% discount vs 12 * monthly (0..100)."""
        m = float(self.price_monthly or 0)
        y = float(self.price_yearly or 0)
        if m <= 0:
            return 0.0
        base = m * 12.0
        return max(0.0, (base - y) / base * 100.0)

    # Validation / normalization
    @validates("slug", "name")
    def _trim80(self, key: str, v: str) -> str:
        t = (v or "").strip()
        if not t:
            raise ValueError(f"{key} is required")
        return t[:80]

    # Business helpers (no DB commit here)
    def price_for_cycle(self, cycle: str = "monthly") -> Decimal:
        """
        Rudisha bei kwa mzunguko: 'monthly' au 'yearly'.
        """
        if cycle not in {"monthly", "yearly"}:
            raise ValueError("cycle must be 'monthly' or 'yearly'")
        return _money(self.price_monthly if cycle == "monthly" else self.price_yearly)

    def monthly_effective_from_yearly(self) -> Decimal:
        """
        Bei ya mwezi ikiwa mnunuzi amelipia mwaka mzima (for display).
        """
        y = _money(self.price_yearly or 0)
        return _money((y / Decimal(12)) if y > 0 else 0)

    def set_prices(self, *, monthly: Decimal | int | float, yearly: Decimal | int | float) -> None:
        self.price_monthly = _money(monthly)
        self.price_yearly  = _money(yearly)

    def set_overages(self, *, per_1000_msgs: Decimal | int | float, per_60_min: Decimal | int | float) -> None:
        self.overage_per_1000_msgs = _money(per_1000_msgs)
        self.overage_per_60_min    = _money(per_60_min)

    def overage_cost_for(self, *, extra_msgs: int = 0, extra_minutes: int = 0) -> Decimal:
        """
        Hesabu makato ya overage kulingana na matumizi ya ziada.
        """
        msgs_blocks = (max(0, int(extra_msgs)) + 999) // 1000 if extra_msgs > 0 else 0
        mins_blocks = (max(0, int(extra_minutes)) + 59) // 60 if extra_minutes > 0 else 0
        total = (self.overage_per_1000_msgs or Decimal("0")) * msgs_blocks \
                + (self.overage_per_60_min or Decimal("0")) * mins_blocks
        return _money(total)

    def has_feature(self, key: str, default: bool = False) -> bool:
        """Soma bendera ya kipengele kwa usalama kutoka JSON features."""
        try:
            return bool((self.features or {}).get(key, default))
        except Exception:
            return default

    def feature_value(self, key: str, default: Any = None) -> Any:
        return (self.features or {}).get(key, default)

    def set_feature(self, key: str, value: Any) -> None:
        data = dict(self.features or {})
        data[key] = value
        self.features = data

    def add_tags(self, items: Iterable[str]) -> None:
        cur = list(self.feature_tags or [])
        seen = {t.lower() for t in cur}
        for it in items or []:
            t = (str(it).strip())[:40]
            if t and t.lower() not in seen:
                cur.append(t)
                seen.add(t.lower())
        # sort for stability
        self.feature_tags = sorted(cur, key=lambda s: s.lower())

    def remove_tags(self, items: Iterable[str]) -> None:
        rm = {str(x).strip().lower() for x in (items or []) if x}
        self.feature_tags = [t for t in (self.feature_tags or []) if t.lower() not in rm]

    def is_available(self) -> bool:
        """Pakage inapatikana kwa wateja wapya?"""
        return self.status == PackageStatus.active

    def can_upgrade_to(self, other: "BotPackage") -> bool:
        """Sheria nyepesi ya upgrade (tier order)."""
        order = [PackageTier.starter, PackageTier.pro, PackageTier.business, PackageTier.enterprise]
        try:
            return order.index(self.tier) < order.index(other.tier) and other.is_available()
        except ValueError:
            return False

    def can_downgrade_to(self, other: "BotPackage") -> bool:
        order = [PackageTier.starter, PackageTier.pro, PackageTier.business, PackageTier.enterprise]
        try:
            return order.index(self.tier) > order.index(other.tier) and other.is_available()
        except ValueError:
            return False

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<BotPackage id={self.id} slug={self.slug!r} tier={self.tier} status={self.status} "
            f"monthly={self.price_monthly} yearly={self.price_yearly}>"
        )

    # --------- Constraints / Indexes ---------
    __table_args__ = (
        CheckConstraint("length(trim(slug)) >= 3", name="ck_botpkg_slug_len"),
        CheckConstraint("length(trim(name)) >= 3", name="ck_botpkg_name_len"),
        CheckConstraint("price_monthly >= 0", name="ck_botpkg_price_monthly_nonneg"),
        CheckConstraint("price_yearly >= 0", name="ck_botpkg_price_yearly_nonneg"),
        CheckConstraint("trial_days >= 0", name="ck_botpkg_trial_nonneg"),
        CheckConstraint("max_bots >= 1", name="ck_botpkg_max_bots_min1"),
        CheckConstraint("max_concurrent_sessions >= 1", name="ck_botpkg_concurrency_min1"),
        CheckConstraint("monthly_message_quota >= 0", name="ck_botpkg_msgs_nonneg"),
        CheckConstraint("monthly_minutes_quota >= 0", name="ck_botpkg_minutes_nonneg"),
        CheckConstraint("overage_per_1000_msgs >= 0", name="ck_botpkg_overage_msgs_nonneg"),
        CheckConstraint("overage_per_60_min >= 0", name="ck_botpkg_overage_minutes_nonneg"),
        Index("ix_botpkg_status_tier", "status", "tier"),
        Index("ix_botpkg_default_active", "is_default", "status"),
    )


# --------- Normalizers / Guards ---------
@listens_for(BotPackage, "before_insert")
def _bp_before_insert(_m, _c, t: BotPackage) -> None:
    if t.slug:
        t.slug = t.slug.strip()[:80]
    if t.name:
        t.name = t.name.strip()[:80]
    if t.description:
        t.description = t.description.strip()[:400]
    # quantize money
    t.price_monthly = _money(t.price_monthly or 0)
    t.price_yearly  = _money(t.price_yearly or 0)
    t.overage_per_1000_msgs = _money(t.overage_per_1000_msgs or 0)
    t.overage_per_60_min    = _money(t.overage_per_60_min or 0)
    # clean tags (limit length, uniq, case-insensitive)
    if t.feature_tags:
        norm = []
        seen = set()
        for tag in t.feature_tags:
            tg = (str(tag).strip())[:40]
            if tg and tg.lower() not in seen:
                norm.append(tg)
                seen.add(tg.lower())
        t.feature_tags = sorted(norm, key=lambda s: s.lower())


@listens_for(BotPackage, "before_update")
def _bp_before_update(_m, _c, t: BotPackage) -> None:
    if t.slug:
        t.slug = t.slug.strip()[:80]
    if t.name:
        t.name = t.name.strip()[:80]
    if t.description:
        t.description = t.description.strip()[:400]
    t.price_monthly = _money(t.price_monthly or 0)
    t.price_yearly  = _money(t.price_yearly or 0)
    t.overage_per_1000_msgs = _money(t.overage_per_1000_msgs or 0)
    t.overage_per_60_min    = _money(t.overage_per_60_min or 0)
    if t.feature_tags:
        norm = []
        seen = set()
        for tag in t.feature_tags:
            tg = (str(tag).strip())[:40]
            if tg and tg.lower() not in seen:
                norm.append(tg)
                seen.add(tg.lower())
        t.feature_tags = sorted(norm, key=lambda s: s.lower())
