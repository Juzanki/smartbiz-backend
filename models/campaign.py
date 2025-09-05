# backend/models/campaign.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import enum
import datetime as dt
import secrets
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, TYPE_CHECKING, Any, Dict, Tuple

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum as SQLEnum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship, backref, validates, Session
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.event import listens_for
from sqlalchemy.ext.mutable import MutableDict

from backend.db import Base
# Portable JSON/NUMERIC (PG→JSONB/NUMERIC, others→JSON/Numeric)
from backend.models._types import JSON_VARIANT, DECIMAL_TYPE

if TYPE_CHECKING:
    from .user import User


# ───────── Enums ─────────
class CampaignStatus(str, enum.Enum):
    draft = "draft"
    active = "active"
    paused = "paused"
    ended = "ended"
    archived = "archived"


class CommissionType(str, enum.Enum):
    percent = "percent"   # 10% ya thamani
    fixed   = "fixed"     # 200 TZS kwa conversion


def _money(x: Decimal | int | float | str) -> Decimal:
    d = x if isinstance(x, Decimal) else Decimal(str(x))
    return d.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


# ───────── Models ─────────
class Campaign(Base):
    """Promotional campaign with tracking + commission rules."""
    __tablename__ = "campaigns"
    __mapper_args__ = {"eager_defaults": True}

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    # Identity
    slug: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    title: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    product_name: Mapped[str] = mapped_column(String(120), nullable=False, index=True)

    description: Mapped[Optional[str]] = mapped_column(Text)

    # Portable + mutable JSON (detect in-place updates)
    meta: Mapped[Optional[Dict[str, Any]]] = mapped_column(MutableDict.as_mutable(JSON_VARIANT))

    # Lifecycle
    status: Mapped[CampaignStatus] = mapped_column(
        SQLEnum(CampaignStatus, name="campaign_status", native_enum=False, validate_strings=True),
        default=CampaignStatus.draft,
        nullable=False,
        index=True,
    )
    start_date: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )
    end_date: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True), index=True)

    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    # Budgeting
    budget_total: Mapped[Decimal] = mapped_column(DECIMAL_TYPE, nullable=False, server_default=text("0"))
    budget_spent: Mapped[Decimal] = mapped_column(DECIMAL_TYPE, nullable=False, server_default=text("0"))

    # Commission rule
    commission_type: Mapped[CommissionType] = mapped_column(
        SQLEnum(CommissionType, name="campaign_commission_type", native_enum=False, validate_strings=True),
        default=CommissionType.percent,
        nullable=False,
        index=True,
    )
    # percent (0..100) OR fixed amount (>=0)
    commission_value: Mapped[Decimal] = mapped_column(DECIMAL_TYPE, nullable=False, server_default=text("0"))

    # Caps (0 = unlimited)
    max_affiliates:  Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    max_clicks:      Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    max_conversions: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))

    # Relationships
    affiliates: Mapped[list["CampaignAffiliate"]] = relationship(
        "CampaignAffiliate",
        back_populates="campaign",
        cascade="all, delete-orphan",
        passive_deletes=True,
        lazy="selectin",
    )

    # ---------- Computed ----------
    @hybrid_property
    def is_active_now(self) -> bool:
        """True only when status=active and we are within [start_date, end_date)."""
        if self.status != CampaignStatus.active:
            return False
        now = dt.datetime.now(dt.timezone.utc)
        if self.start_date and now < self.start_date:
            return False
        if self.end_date and now >= self.end_date:
            return False
        return True

    @hybrid_property
    def budget_remaining(self) -> Decimal:
        return _money((self.budget_total or 0) - (self.budget_spent or 0))

    # ---------- Validators ----------
    @validates("slug", "title", "product_name")
    def _trim120(self, key: str, v: str) -> str:
        t = (v or "").strip()
        if not t:
            raise ValueError(f"{key} is required")
        return t[:120]

    # ---------- Business helpers (no DB commit here) ----------
    def can_join(self, *, current_affiliate_count: int) -> bool:
        return (self.max_affiliates == 0) or (current_affiliate_count < self.max_affiliates)

    def mark_active(self) -> None:
        if self.status not in {CampaignStatus.draft, CampaignStatus.paused}:
            raise ValueError("Only draft/paused can be activated.")
        self.status = CampaignStatus.active

    def pause(self) -> None:
        if self.status != CampaignStatus.active:
            raise ValueError("Only active can be paused.")
        self.status = CampaignStatus.paused

    def end_now(self) -> None:
        self.status = CampaignStatus.ended
        self.end_date = dt.datetime.now(dt.timezone.utc)

    def archive(self) -> None:
        if self.status not in {CampaignStatus.ended, CampaignStatus.draft, CampaignStatus.paused}:
            raise ValueError("Only ended/draft/paused can be archived.")
        self.status = CampaignStatus.archived

    def add_spend(self, amount: Decimal | int | float) -> Decimal:
        """Increase budget_spent (clamped to budget_total). Returns new spent."""
        inc = _money(amount)
        if inc < 0:
            raise ValueError("Spend increment must be >= 0")
        self.budget_spent = _money((self.budget_spent or 0) + inc)
        # do not auto-clamp negative remaining; allow overspend if business accepts
        return self.budget_spent

    def compute_commission(self, order_value: Decimal | int | float) -> Decimal:
        ov = _money(order_value)
        if self.commission_type == CommissionType.percent:
            pct = _money(self.commission_value or 0)
            if pct < 0 or pct > 100:
                raise ValueError("percent commission must be 0..100")
            return _money(ov * (pct / Decimal("100")))
        else:
            fixed = _money(self.commission_value or 0)
            if fixed < 0:
                raise ValueError("fixed commission must be >= 0")
            return fixed

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Campaign id={self.id} slug={self.slug!r} title={self.title!r} status={self.status}>"

    __table_args__ = (
        CheckConstraint("length(trim(slug)) >= 3", name="ck_campaign_slug_len"),
        CheckConstraint(
            "(end_date IS NULL) OR (start_date IS NULL) OR (end_date > start_date)",
            name="ck_campaign_dates_order",
        ),
        CheckConstraint("budget_total >= 0", name="ck_campaign_budget_total_nonneg"),
        CheckConstraint("budget_spent >= 0", name="ck_campaign_budget_spent_nonneg"),
        CheckConstraint("commission_value >= 0", name="ck_campaign_commission_nonneg"),
        CheckConstraint("max_affiliates >= 0", name="ck_campaign_max_affiliates_nonneg"),
        CheckConstraint("max_clicks >= 0", name="ck_campaign_max_clicks_nonneg"),
        CheckConstraint("max_conversions >= 0", name="ck_campaign_max_conversions_nonneg"),
        Index("ix_campaign_status_dates", "status", "start_date", "end_date"),
    )


class CampaignAffiliate(Base):
    """Link a user to a campaign + track their performance."""
    __tablename__ = "campaign_affiliates"
    __mapper_args__ = {"eager_defaults": True}
    __table_args__ = (
        UniqueConstraint("campaign_id", "user_id",  name="uq_campaign_user"),
        UniqueConstraint("campaign_id", "ref_code", name="uq_campaign_refcode"),
        Index("ix_campa_aff_user_campaign", "user_id", "campaign_id"),
        CheckConstraint("length(trim(ref_code)) >= 6", name="ck_campa_refcode_len"),
        CheckConstraint("clicks >= 0 AND conversions >= 0", name="ck_campa_counts_nonneg"),
        CheckConstraint("revenue >= 0 AND payout_due >= 0", name="ck_campa_money_nonneg"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    campaign_id: Mapped[int] = mapped_column(
        ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )

    # Each affiliate gets a stable referral code for link building
    ref_code: Mapped[str] = mapped_column(String(24), nullable=False, index=True)

    # Performance counters
    clicks:       Mapped[int]     = mapped_column(Integer, nullable=False, server_default=text("0"))
    conversions:  Mapped[int]     = mapped_column(Integer, nullable=False, server_default=text("0"))
    revenue:      Mapped[Decimal] = mapped_column(DECIMAL_TYPE, nullable=False, server_default=text("0"))
    payout_due:   Mapped[Decimal] = mapped_column(DECIMAL_TYPE, nullable=False, server_default=text("0"))

    # Audit
    joined_at:          Mapped[dt.datetime]           = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False, index=True)
    last_click_at:      Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    last_conversion_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))

    # Relationships
    campaign: Mapped["Campaign"] = relationship(
        "Campaign",
        back_populates="affiliates",
        foreign_keys=lambda: [CampaignAffiliate.campaign_id],
        passive_deletes=True,
        lazy="selectin",
    )
    # Backref: User.joined_campaigns
    user: Mapped["User"] = relationship(
        "User",
        backref=backref("joined_campaigns", lazy="selectin"),
        foreign_keys=lambda: [CampaignAffiliate.user_id],
        passive_deletes=True,
        lazy="selectin",
    )

    # ---------- Helpers ----------
    @staticmethod
    def generate_ref_code(length: int = 12) -> str:
        return secrets.token_urlsafe(length)[:length]

    @validates("ref_code")
    def _trim_ref(self, _k: str, v: str) -> str:
        t = (v or "").strip()
        if len(t) < 6:
            raise ValueError("ref_code must be >= 6 chars")
        return t[:24]

    def register_click(self) -> None:
        self.clicks += 1
        self.last_click_at = dt.datetime.now(dt.timezone.utc)

    def _commission(self, order_value: Decimal | float | int, *, rule_type: CommissionType, rule_value: Decimal) -> Decimal:
        ov = _money(order_value)
        if rule_type == CommissionType.percent:
            pct = _money(rule_value or 0)
            if pct < 0 or pct > 100:
                raise ValueError("percent commission must be 0..100")
            return _money(ov * (pct / Decimal("100")))
        return _money(rule_value or 0)

    def register_conversion(
        self,
        order_value: Decimal | float | int | str,
        commission_type: CommissionType,
        commission_value: Decimal,
    ) -> Decimal:
        """Apply commission rule and update counters; returns commission amount."""
        if not isinstance(order_value, Decimal):
            order_value = Decimal(str(order_value))
        self.conversions += 1
        self.revenue = _money((self.revenue or 0) + _money(order_value))
        commission = self._commission(order_value, rule_type=commission_type, rule_value=commission_value)
        self.payout_due = _money((self.payout_due or 0) + commission)
        self.last_conversion_at = dt.datetime.now(dt.timezone.utc)
        return commission

    def payout(self, amount: Decimal | int | float) -> None:
        """Apply payout (reduces payout_due); never below zero."""
        amt = _money(amount)
        if amt <= 0:
            raise ValueError("payout amount must be positive")
        due = self.payout_due or Decimal("0")
        if amt > due:
            amt = due
        self.payout_due = _money(due - amt)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<CampaignAffiliate id={self.id} user={self.user_id} camp={self.campaign_id} code={self.ref_code}>"


# ---------------- Normalizers / Guards ----------------
@listens_for(Campaign, "before_insert")
def _c_before_insert(_m, _c, t: Campaign) -> None:
    if t.slug:
        t.slug = t.slug.strip()[:120]
    if t.title:
        t.title = t.title.strip()[:120]
    if t.product_name:
        t.product_name = t.product_name.strip()[:120]
    if t.description:
        t.description = t.description.strip()
    t.budget_total = _money(t.budget_total or 0)
    t.budget_spent = _money(t.budget_spent or 0)
    t.commission_value = _money(t.commission_value or 0)
    if t.budget_total < 0:
        t.budget_total = Decimal("0.00")
    if t.budget_spent < 0:
        t.budget_spent = Decimal("0.00")


@listens_for(Campaign, "before_update")
def _c_before_update(_m, _c, t: Campaign) -> None:
    if t.slug:
        t.slug = t.slug.strip()[:120]
    if t.title:
        t.title = t.title.strip()[:120]
    if t.product_name:
        t.product_name = t.product_name.strip()[:120]
    if t.description:
        t.description = t.description.strip()
    t.budget_total = _money(t.budget_total or 0)
    t.budget_spent = _money(t.budget_spent or 0)
    t.commission_value = _money(t.commission_value or 0)
    if t.budget_total < 0:
        t.budget_total = Decimal("0.00")
    if t.budget_spent < 0:
        t.budget_spent = Decimal("0.00")
