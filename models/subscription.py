# backend/models/subscription.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import datetime as dt
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Dict, Any, List

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    Numeric,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from pydantic import BaseModel, Field

from backend.db import Base
from backend.models._types import JSON_VARIANT, DECIMAL_TYPE, as_mutable_json

# ----------------------------- Plan (catalog) -----------------------------

_DEC2 = Numeric(18, 2)
_ALLOWED_INTERVAL = ("day", "week", "month", "year")
_ALLOWED_STATUS = ("active", "archived")
_DEFAULT_CURRENCY = "TZS"

class SubscriptionPlan(Base):
    """
    SubscriptionPlan — productized plan definition (Free, Pro, Business, …)

    Mobile-first upgrades:
    - Decimal pricing + currency
    - Billing interval & interval_count (e.g., 1 month, 12 months)
    - Trials, grace period, and quotas in JSON
    - Status & sort_order for catalog UI
    - Slug for stable references in clients
    """
    __tablename__ = "subscription_plans"
    __table_args__ = (
        UniqueConstraint("slug", name="uq_subscription_plan_slug"),
        UniqueConstraint("name", name="uq_subscription_plan_name"),
        CheckConstraint("price >= 0", name="ck_plan_price_nonneg"),
        CheckConstraint("interval_count >= 1", name="ck_plan_interval_count_min1"),
        CheckConstraint(
            "billing_interval in ('day','week','month','year')",
            name="ck_plan_billing_interval_enum"
        ),
        CheckConstraint("trial_days >= 0", name="ck_plan_trial_nonneg"),
        CheckConstraint("grace_period_days >= 0", name="ck_plan_grace_nonneg"),
        Index("ix_plans_status_sort", "status", "sort_order"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)

    # Identity
    name: Mapped[str] = mapped_column(String(50), nullable=False)
    slug: Mapped[str] = mapped_column(String(50), nullable=False, doc="stable, url-safe code e.g. pro, business")

    # Pricing
    price: Mapped[Decimal] = mapped_column(_DEC2, default=Decimal("0.00"), nullable=False)
    currency: Mapped[str] = mapped_column(String(8), default=_DEFAULT_CURRENCY, nullable=False)

    # Billing cadence
    billing_interval: Mapped[str] = mapped_column(String(8), default="month", nullable=False)  # day|week|month|year
    interval_count: Mapped[int] = mapped_column(Integer, default=1, nullable=False)            # e.g. 1 month, 12 months

    # Trials & grace
    trial_days: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    grace_period_days: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # Quotas & feature flags (portable)
    features: Mapped[List[str]] = mapped_column(JSON, default=list, nullable=False)  # ["priority_support", ...]
    limits: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON, default=None)     # {"seats": 5, "storage_mb": 10240}

    # UX / lifecycle
    description: Mapped[Optional[str]] = mapped_column(String(255), default=None)
    status: Mapped[str] = mapped_column(String(16), default="active", nullable=False)  # active|archived
    sort_order: Mapped[int] = mapped_column(Integer, default=100, nullable=False)

    # Timestamps
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    # Relationships
    subscriptions: Mapped[list["UserSubscription"]] = relationship(
        "UserSubscription", back_populates="plan", cascade="all, delete-orphan", lazy="selectin"
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<SubscriptionPlan id={self.id} slug={self.slug} price={self.price} {self.currency} {self.billing_interval}/{self.interval_count}>"

# ----------------------------- User Subscription -----------------------------

_SUB_STATUS = ("trialing", "active", "past_due", "canceled", "expired")

class UserSubscription(Base):
    """
    UserSubscription — tracks a user's plan membership over time.

    Mobile-first upgrades:
    - Clear period fields (current_period_start/end)
    - Auto-renew flags, cancel_at_period_end, grace window
    - Provider linkage (external_subscription_id, provider)
    - Status machine + invoicing hooks
    - Strong indexes for "expiring soon" and "renewal queue"
    """
    __tablename__ = "user_subscriptions"
    __table_args__ = (
        Index("ix_usersub_user_status", "user_id", "status"),
        Index("ix_usersub_period_end", "current_period_end"),
        Index("ix_usersub_provider", "provider", "external_subscription_id"),
        CheckConstraint("status in ('trialing','active','past_due','canceled','expired')", name="ck_usersub_status_enum"),
        CheckConstraint("current_period_end IS NULL OR current_period_start <= current_period_end", name="ck_usersub_period_order"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    plan_id: Mapped[int] = mapped_column(ForeignKey("subscription_plans.id", ondelete="RESTRICT"), nullable=False, index=True)

    # Period & lifecycle
    status: Mapped[str] = mapped_column(String(16), default="trialing", nullable=False)
    current_period_start: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True), default=None)
    current_period_end: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True), default=None)

    # Auto-renew & cancellation
    auto_renew: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    cancel_at_period_end: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    canceled_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True), default=None)
    grace_end_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True), default=None)

    # Provider integration
    provider: Mapped[Optional[str]] = mapped_column(String(24), default=None)  # e.g., "stripe", "mpesa", "paypal"
    external_subscription_id: Mapped[Optional[str]] = mapped_column(String(64), default=None)
    last_invoice_id: Mapped[Optional[str]] = mapped_column(String(64), default=None)
    meta: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON, default=None)

    # Audit
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    activated_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True), default=None)
    expired_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True), default=None)

    # Relationships
    user: Mapped["User"] = relationship("User", back_populates="subscriptions", lazy="selectin")
    plan: Mapped["SubscriptionPlan"] = relationship("SubscriptionPlan", back_populates="subscriptions", lazy="joined")

    # ------------------------------- Helpers -------------------------------

    @staticmethod
    def _q2(v: Decimal | float | int) -> Decimal:
        return Decimal(str(v)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    @staticmethod
    def _add_interval(start: dt.datetime, *, interval: str, count: int) -> dt.datetime:
        """Add plan cadence to a datetime. (Month/year via naive approximation; use dateutil if available.)"""
        if interval == "day":
            return start + dt.timedelta(days=count)
        if interval == "week":
            return start + dt.timedelta(weeks=count)
        if interval == "month":
            # naive month add: 30 days * count (replace with relativedelta if you prefer)
            return start + dt.timedelta(days=30 * count)
        if interval == "year":
            return start + dt.timedelta(days=365 * count)
        raise ValueError("Invalid interval")

    def start(self, now: Optional[dt.datetime] = None) -> None:
        """Start trial or active period based on plan.trial_days."""
        if not self.plan:
            raise ValueError("Plan must be loaded/assigned before start()")
        now = now or dt.datetime.now(dt.timezone.utc)
        self.current_period_start = now
        interval_end = self._add_interval(
            now,
            interval=self.plan.billing_interval,
            count=self.plan.interval_count,
        )
        self.current_period_end = interval_end
        if (self.plan.trial_days or 0) > 0:
            self.status = "trialing"
            self.activated_at = None
        else:
            self.status = "active"
            self.activated_at = now
        if (self.plan.grace_period_days or 0) > 0:
            self.grace_end_at = interval_end + dt.timedelta(days=self.plan.grace_period_days)

    def activate(self, at: Optional[dt.datetime] = None) -> None:
        """Switch to active (e.g., payment confirmed)."""
        self.status = "active"
        self.activated_at = at or dt.datetime.now(dt.timezone.utc)

    def mark_past_due(self) -> None:
        self.status = "past_due"

    def cancel_now(self) -> None:
        """Immediate cancel; disables auto-renew and sets canceled_at."""
        self.auto_renew = False
        self.cancel_at_period_end = False
        self.canceled_at = dt.datetime.now(dt.timezone.utc)
        self.status = "canceled"

    def cancel_at_end(self) -> None:
        """Mark to end at period boundary; access continues until then."""
        self.cancel_at_period_end = True
        self.auto_renew = False

    def resume(self) -> None:
        """Resume auto-renew before period ends."""
        if self.status not in {"active", "trialing", "past_due"}:
            raise ValueError("Cannot resume a canceled/expired subscription")
        self.cancel_at_period_end = False
        self.auto_renew = True

    def renew(self, now: Optional[dt.datetime] = None) -> None:
        """
        Advance to next billing period (called after successful charge).
        Resets past_due and extends grace window.
        """
        if not self.plan:
            raise ValueError("Plan must be loaded/assigned before renew()")
        now = now or dt.datetime.now(dt.timezone.utc)
        start = self.current_period_end or now
        self.current_period_start = start
        self.current_period_end = self._add_interval(
            start,
            interval=self.plan.billing_interval,
            count=self.plan.interval_count,
        )
        self.status = "active"
        self.activated_at = now if not self.activated_at else self.activated_at
        if (self.plan.grace_period_days or 0) > 0:
            self.grace_end_at = self.current_period_end + dt.timedelta(days=self.plan.grace_period_days)

    def expire(self, at: Optional[dt.datetime] = None) -> None:
        """Mark as expired (no access)."""
        self.status = "expired"
        self.expired_at = at or dt.datetime.now(dt.timezone.utc)
        self.auto_renew = False

    # ----- Queries / checks (pure functions)

    def is_active(self, when: Optional[dt.datetime] = None) -> bool:
        when = when or dt.datetime.now(dt.timezone.utc)
        if self.status in {"active", "trialing"} and self.current_period_end:
            return when <= self.current_period_end or (
                self.grace_end_at is not None and when <= self.grace_end_at
            )
        return False

    def days_left(self, when: Optional[dt.datetime] = None) -> Optional[int]:
        if not self.current_period_end:
            return None
        when = when or dt.datetime.now(dt.timezone.utc)
        delta = self.current_period_end - when
        return max(0, delta.days)

    def in_grace(self, when: Optional[dt.datetime] = None) -> bool:
        when = when or dt.datetime.now(dt.timezone.utc)
        return self.grace_end_at is not None and when <= self.grace_end_at

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<UserSubscription id={self.id} user={self.user_id} plan={self.plan_id} "
            f"status={self.status} period={self.current_period_start}..{self.current_period_end}>"
        )

# ----------------------------- Lightweight DTOs -----------------------------

class PlanOut(BaseModel):
    id: int
    name: str
    slug: str
    price: Decimal
    currency: str
    billing_interval: str
    interval_count: int
    trial_days: int
    grace_period_days: int
    description: Optional[str] = None
    features: List[str] = Field(default_factory=list)
    limits: Optional[Dict[str, Any]] = None

    class Config:
        from_attributes = True

class SubscriptionOut(BaseModel):
    id: int
    user_id: int
    plan_id: int
    status: str
    current_period_start: Optional[dt.datetime] = None
    current_period_end: Optional[dt.datetime] = None
    auto_renew: bool
    cancel_at_period_end: bool
    grace_end_at: Optional[dt.datetime] = None
    provider: Optional[str] = None
    external_subscription_id: Optional[str] = None

    class Config:
        from_attributes = True



