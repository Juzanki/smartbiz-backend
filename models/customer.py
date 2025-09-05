# backend/models/customer.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import re
import datetime as dt
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, TYPE_CHECKING, Iterable, Dict, Any, List

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
    Table,
    Column,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship, validates
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.event import listens_for
from sqlalchemy.ext.mutable import MutableDict

from backend.db import Base
from backend.models._types import JSON_VARIANT, DECIMAL_TYPE  # portable NUMERIC & JSON

if TYPE_CHECKING:
    from .user import User
    from .smart_tags import Tag  # lazima iwe na back_populates="customers"
    from .customer_feedback import CustomerFeedback  # back_populates="customer"

# ─────────────────────────── M2M join: customers ↔ tags ───────────────────────────
customer_tags = Table(
    "customer_tags",
    Base.metadata,
    Column("customer_id", Integer, ForeignKey("customers.id", ondelete="CASCADE"), primary_key=True),
    Column("tag_id", Integer, ForeignKey("tags.id", ondelete="CASCADE"), primary_key=True),
    Index("ix_customer_tags_tag", "tag_id"),
)

# ─────────────────────────────── Helpers ───────────────────────────────
_phone_re = re.compile(r"\D+")

def _normalize_phone(phone: str | None) -> str | None:
    if not phone:
        return None
    p = phone.strip()
    if not p:
        return None
    if p.startswith("+"):
        return "+" + _phone_re.sub("", p[1:])
    return _phone_re.sub("", p) or None

def _normalize_email(email: str | None) -> str | None:
    s = (email or "").strip().lower()
    return s or None

def _money(x: Decimal | int | float | str) -> Decimal:
    d = x if isinstance(x, Decimal) else Decimal(str(x))
    return d.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


class Customer(Base):
    """
    CRM mteja anayeimilikiwa na User (user_id).

    - Mawasiliano (simu/email) + vibali (consent)
    - Metrics (lifetime_value, orders_count)
    - Soft delete + tagging kupitia customer_tags
    - JSON fields mutable (address, meta) → in-place updates hutambulika
    """
    __tablename__ = "customers"
    __mapper_args__ = {"eager_defaults": True}

    # ---- Identity / ownership ----
    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )

    name: Mapped[str] = mapped_column(String(120), nullable=False, index=True)

    # Contact (raw + normalized)
    phone_number: Mapped[str] = mapped_column(String(32), nullable=False)   # raw (kwa auditing)
    phone_normalized: Mapped[Optional[str]] = mapped_column(String(32), index=True)
    email: Mapped[Optional[str]] = mapped_column(String(160))
    email_lower: Mapped[Optional[str]] = mapped_column(String(160), index=True)

    # Profile / notes / meta
    region: Mapped[Optional[str]] = mapped_column(String(120))
    notes: Mapped[Optional[str]] = mapped_column(Text)

    # Mutable JSON (dict)
    address: Mapped[Optional[Dict[str, Any]]] = mapped_column(MutableDict.as_mutable(JSON_VARIANT))
    meta:    Mapped[Optional[Dict[str, Any]]] = mapped_column(MutableDict.as_mutable(JSON_VARIANT))

    # Consent & preferred channel
    allow_sms:   Mapped[bool] = mapped_column(Boolean, default=True,  nullable=False, index=True)
    allow_email: Mapped[bool] = mapped_column(Boolean, default=True,  nullable=False, index=True)
    preferred_channel: Mapped[Optional[str]] = mapped_column(String(16))  # "sms" | "email" | "whatsapp" | ...

    # Simple metrics
    lifetime_value: Mapped[Decimal] = mapped_column(DECIMAL_TYPE, nullable=False, server_default=text("0"))
    orders_count:   Mapped[int]     = mapped_column(Integer,      nullable=False, server_default=text("0"))
    last_seen_at:   Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True), index=True)

    # Lifecycle
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP"), nullable=False, index=True
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("CURRENT_TIMESTAMP"),
        onupdate=text("CURRENT_TIMESTAMP"),
        nullable=False,
    )
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)
    deleted_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))

    # ---- Relationships ----
    user: Mapped["User"] = relationship(
        "User",
        back_populates="customers",
        foreign_keys=[user_id],
        passive_deletes=True,
        lazy="selectin",
    )

    tags: Mapped[List["Tag"]] = relationship(
        "Tag",
        secondary=customer_tags,
        back_populates="customers",
        lazy="selectin",
        passive_deletes=True,
    )

    # NEW: link to feedbacks (1→Many)
    customer_feedbacks: Mapped[List["CustomerFeedback"]] = relationship(
        "CustomerFeedback",
        back_populates="customer",
        foreign_keys="CustomerFeedback.customer_id",
        passive_deletes=True,
        lazy="selectin",
    )

    # ---- Hybrids & helpers ----
    @hybrid_property
    def has_contact(self) -> bool:
        return bool(self.phone_normalized or self.email_lower)

    @hybrid_property
    def can_sms(self) -> bool:
        return bool(self.allow_sms and self.phone_normalized)

    @hybrid_property
    def can_email(self) -> bool:
        return bool(self.allow_email and self.email_lower)

    def soft_delete(self) -> None:
        self.is_deleted = True
        self.deleted_at = dt.datetime.now(dt.timezone.utc)

    def touch_seen(self) -> None:
        self.last_seen_at = dt.datetime.now(dt.timezone.utc)

    def add_value(self, amount: Decimal | int | float | str) -> None:
        """Ongeza LTV na counter ya order moja (round 2dp)."""
        amt = _money(amount)
        if amt < 0:
            raise ValueError("amount must be >= 0")
        self.lifetime_value = _money((self.lifetime_value or Decimal("0")) + amt)
        self.orders_count = (self.orders_count or 0) + 1

    def set_consent(self, *, sms: Optional[bool] = None, email: Optional[bool] = None) -> None:
        if sms is not None:
            self.allow_sms = bool(sms)
        if email is not None:
            self.allow_email = bool(email)

    def prefer_channel(self, channel: Optional[str]) -> None:
        if not channel:
            self.preferred_channel = None
            return
        ch = channel.strip().lower()
        allowed = {"sms", "email", "whatsapp", "telegram", "web", "other"}
        self.preferred_channel = ch if ch in allowed else "other"

    def merge_address(self, updates: Dict[str, Any]) -> None:
        data = dict(self.address or {})
        for k, v in (updates or {}).items():
            data[str(k)] = v
        self.address = data

    def merge_meta(self, updates: Dict[str, Any]) -> None:
        data = dict(self.meta or {})
        for k, v in (updates or {}).items():
            data[str(k)] = v
        self.meta = data

    def add_tags(self, tags: Iterable[Tag] | Iterable[str]) -> None:  # type: ignore[name-defined]
        """Ongeza Tag objects tayari loaded kwenye session (service layer huresolve majina→Tag)."""
        for t in tags or []:
            if hasattr(t, "id"):  # object
                if t not in self.tags:
                    self.tags.append(t)  # type: ignore[arg-type]

    def remove_tags(self, tags: Iterable[str]) -> None:
        wanted = {str(x).lower() for x in (tags or [])}
        for t in list(self.tags or []):
            if getattr(t, "name", "").lower() in wanted:
                self.tags.remove(t)

    # ─────────── Normalizers/validators ───────────
    @validates("name")
    def _val_name(self, _k: str, v: str) -> str:
        t = (v or "").strip()
        if len(t) < 2:
            raise ValueError("name must be at least 2 characters")
        return t[:120]

    @validates("phone_number")
    def _val_phone(self, _key: str, value: str) -> str:  # type: ignore[override]
        self.phone_normalized = _normalize_phone(value)
        return (value or "").strip()[:32]

    @validates("email")
    def _val_email(self, _key: str, value: Optional[str]) -> Optional[str]:  # type: ignore[override]
        self.email_lower = _normalize_email(value)
        v = (value or "").strip()
        return v[:160] or None

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<Customer id={self.id} user={self.user_id} name={self.name!r} "
            f"phone={self.phone_normalized} email={self.email_lower}>"
        )

    # ---- Constraints & Indexes ----
    __table_args__ = (
        # Per-owner uniqueness kwenye namba ya simu iliyosafishwa
        UniqueConstraint("user_id", "phone_normalized", name="uq_customer_user_phone"),
        Index("ix_customer_user_created", "user_id", "created_at"),
        Index("ix_customer_phone_norm", "phone_normalized"),
        Index("ix_customer_email_lower", "email_lower"),
        CheckConstraint("length(trim(name)) >= 2", name="ck_customer_name_len"),
        CheckConstraint(
            "phone_normalized IS NOT NULL AND length(phone_normalized) >= 5",
            name="ck_customer_phone_norm_required",
        ),
        CheckConstraint("orders_count >= 0", name="ck_customer_orders_nonneg"),
        CheckConstraint("lifetime_value >= 0", name="ck_customer_ltv_nonneg"),
    )


# ───────────────────────── Event Normalizers ─────────────────────────
@listens_for(Customer, "before_insert")
def _cust_before_insert(_m, _c, t: Customer) -> None:
    if t.region:
        t.region = t.region.strip()[:120]
    if t.notes:
        t.notes = t.notes.strip()
    # ensure monetary is 2dp non-negative
    t.lifetime_value = _money(t.lifetime_value or 0)
    if t.lifetime_value < 0:
        t.lifetime_value = Decimal("0.00")

@listens_for(Customer, "before_update")
def _cust_before_update(_m, _c, t: Customer) -> None:
    _cust_before_insert(_m, _c, t)  # same normalizations
