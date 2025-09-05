# backend/models/withdraw_request.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import datetime as dt
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Dict, Any

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
from sqlalchemy.orm import Mapped, mapped_column, relationship, validates
from sqlalchemy.event import listens_for

from backend.db import Base

# --- Constants & helpers -------------------------------------------------

# decimal(18,2) kwa fedha
_DEC2 = Numeric(18, 2)

_ALLOWED_METHODS = ("mpesa", "airtel", "tigo", "bank", "paypal", "crypto", "other")
_ALLOWED_STATUS = ("pending", "approved", "processing", "paid", "rejected", "failed", "canceled")

# Tengeneza orodha ya SQL kwa CHECK (... IN ('a','b',...))
_ALLOWED_METHODS_SQL = ", ".join(f"'{m}'" for m in _ALLOWED_METHODS)
_ALLOWED_STATUS_SQL = ", ".join(f"'{s}'" for s in _ALLOWED_STATUS)


def _q2(val: Decimal | float | int | None) -> Optional[Decimal]:
    """Quantize to 2 decimal places using bankers round (HALF_UP here)."""
    if val is None:
        return None
    return Decimal(str(val)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


# --- Model ----------------------------------------------------------------

class WithdrawRequest(Base):
    """
    Maombi ya malipo (withdraw/payout) yenye hatua kamili ya uendeshaji.

    - FKs mbili kwenda `users`: `user_id` (aliyeomba), `approved_by_user_id` (aliyeidhinisha)
    - back_populates sahihi → hakuna AmbiguousForeignKeysError
    - Guardrails: CheckConstraints, validators, normalization
    - Hesabu thabiti ya net_amount = amount - fee_amount
    """
    __tablename__ = "withdraw_requests"
    __mapper_args__ = {"eager_defaults": True}

    # --- Keys
    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)

    # Mtumiaji aliyeomba (required)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )

    # Aliyeidhinisha (admin/mod) – yaweza kuwa NULL
    approved_by_user_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )

    # --- Amount / currency
    amount: Mapped[Decimal] = mapped_column(_DEC2, nullable=False)
    fee_amount: Mapped[Optional[Decimal]] = mapped_column(
        _DEC2, doc="Ada (>=0). Ikiwa None, net_amount = amount."
    )
    net_amount: Mapped[Optional[Decimal]] = mapped_column(
        _DEC2, doc="amount - fee_amount (computed/validated)"
    )
    currency: Mapped[str] = mapped_column(String(8), default="TZS", nullable=False)

    # --- Payout rails
    method: Mapped[str] = mapped_column(String(16), default="mpesa", nullable=False)  # mpesa|airtel|...
    destination: Mapped[str] = mapped_column(String(128), nullable=False)             # phone/acct/wallet
    destination_name: Mapped[Optional[str]] = mapped_column(String(128), default=None)

    # --- Lifecycle
    status: Mapped[str] = mapped_column(String(16), default="pending", nullable=False, index=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )
    reviewed_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    processing_started_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    paid_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))

    # --- Meta & audit
    meta: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON)
    reviewer_note: Mapped[Optional[str]] = mapped_column(String(255))
    payout_ref:   Mapped[Optional[str]] = mapped_column(String(64),  index=True)  # PSP/Bank ref
    external_id:  Mapped[Optional[str]] = mapped_column(String(64),  index=True)  # correlation/webhook
    idempotency_key: Mapped[Optional[str]] = mapped_column(String(64), index=True)

    # --- Flags
    is_paid: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # --- Relationships (disambiguated foreign_keys)
    user: Mapped["User"] = relationship(
        "User",
        back_populates="withdraw_requests",
        foreign_keys=[user_id],
        lazy="selectin",
        passive_deletes=True,
    )

    approved_by: Mapped[Optional["User"]] = relationship(
        "User",
        back_populates="withdraw_approvals",
        foreign_keys=[approved_by_user_id],
        lazy="selectin",
        passive_deletes=True,
    )

    # --- Constraints & Indexes
    __table_args__ = (
        CheckConstraint("amount > 0", name="ck_withdraw_amount_positive"),
        CheckConstraint("fee_amount IS NULL OR fee_amount >= 0", name="ck_withdraw_fee_nonneg"),
        CheckConstraint("net_amount IS NULL OR net_amount >= 0", name="ck_withdraw_net_nonneg"),
        CheckConstraint("length(currency) BETWEEN 2 AND 8", name="ck_withdraw_currency_len"),
        CheckConstraint(f"status IN ({_ALLOWED_STATUS_SQL})",  name="ck_withdraw_status_allowed"),
        CheckConstraint(f"method IN ({_ALLOWED_METHODS_SQL})", name="ck_withdraw_method_allowed"),
        CheckConstraint("length(destination) > 0",  name="ck_withdraw_destination_nonempty"),

        UniqueConstraint("user_id", "idempotency_key", name="uq_withdraw_idempotency_per_user"),

        Index("ix_withdraw_user_status",    "user_id", "status"),
        Index("ix_withdraw_method_time",    "method",  "created_at"),
        Index("ix_withdraw_approver_time",  "approved_by_user_id", "created_at"),
    )

    # ------------------------ Business helpers ------------------------ #

    def set_fee(self, fee: Decimal | float | int | None) -> None:
        """Weka ada na urekebishe net_amount (>= 0)."""
        qfee = _q2(fee)
        self.fee_amount = qfee
        if qfee is None:
            self.net_amount = _q2(self.amount)
        else:
            net = (_q2(self.amount) or Decimal("0.00")) - qfee
            if net < Decimal("0.00"):
                raise ValueError("Fee cannot exceed amount")
            self.net_amount = net

    # State transitions
    def approve(self, approver_id: int, note: Optional[str] = None, *, fee: Decimal | float | int | None = None) -> None:
        self.status = "approved"
        self.reviewed_at = dt.datetime.now(dt.timezone.utc)
        self.approved_by_user_id = approver_id
        if note:
            self.reviewer_note = note[:255]
        if fee is not None:
            self.set_fee(fee)

    def start_processing(self, external_id: Optional[str] = None) -> None:
        if self.status not in ("approved", "processing"):
            raise ValueError("Can only start processing an approved request")
        self.status = "processing"
        self.processing_started_at = dt.datetime.now(dt.timezone.utc)
        if external_id:
            self.external_id = external_id[:64]

    def mark_paid(self, payout_ref: Optional[str] = None) -> None:
        if self.status not in ("approved", "processing"):
            raise ValueError("Cannot mark paid unless approved/processing")
        self.status = "paid"
        self.is_paid = True
        self.paid_at = dt.datetime.now(dt.timezone.utc)
        if payout_ref:
            self.payout_ref = payout_ref[:64]

    def reject(self, approver_id: int, reason: Optional[str] = None) -> None:
        self.status = "rejected"
        self.reviewed_at = dt.datetime.now(dt.timezone.utc)
        self.approved_by_user_id = approver_id
        if reason:
            self.reviewer_note = reason[:255]

    def fail(self, reason: Optional[str] = None) -> None:
        self.status = "failed"
        self.is_paid = False
        if reason:
            self.reviewer_note = reason[:255]

    def cancel(self, reason: Optional[str] = None) -> None:
        if self.status not in ("pending", "approved"):
            raise ValueError("Only pending/approved requests can be canceled")
        self.status = "canceled"
        if reason:
            self.reviewer_note = reason[:255]

    def set_idempotency(self, key: Optional[str]) -> None:
        self.idempotency_key = key[:64] if key else None

    # Utilities
    @property
    def is_terminal(self) -> bool:
        return self.status in ("paid", "rejected", "failed", "canceled")

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<WithdrawRequest id={self.id} user={self.user_id} "
            f"amount={self.amount} {self.currency} method={self.method} "
            f"status={self.status} paid={self.is_paid}>"
        )


# ------------------------ Validators & normalizers ------------------------ #

@validates("method")
def _validate_method(_cls, v: str) -> str:
    v = (v or "").strip().lower()
    if v not in _ALLOWED_METHODS:
        raise ValueError(f"Unsupported payout method: {v}")
    return v

@validates("status")
def _validate_status(_cls, v: str) -> str:
    v = (v or "").strip().lower()
    if v not in _ALLOWED_STATUS:
        raise ValueError(f"Unsupported status: {v}")
    return v

@validates("currency")
def _validate_currency(_cls, v: str) -> str:
    v = (v or "").strip().upper()
    if not (2 <= len(v) <= 8):
        raise ValueError("Currency must be 2..8 chars")
    return v

@validates("destination", "destination_name", "payout_ref", "external_id", "idempotency_key")
def _strip_text(_cls, v: Optional[str]) -> Optional[str]:
    if v is None:
        return None
    s = " ".join(v.strip().split())
    return s or None


# --- Auto-compute & normalize on insert/update --------------------------- #

@listens_for(WithdrawRequest, "before_insert")
def _wr_before_insert(_mapper, _conn, target: WithdrawRequest) -> None:
    # normalize numeric fields
    target.amount = _q2(target.amount) or Decimal("0.00")
    target.fee_amount = _q2(target.fee_amount) if target.fee_amount is not None else None

    # compute net_amount
    if target.fee_amount is None:
        target.net_amount = target.amount
    else:
        net = target.amount - target.fee_amount
        if net < Decimal("0.00"):
            raise ValueError("Fee cannot exceed amount")
        target.net_amount = _q2(net)

    # normalize strings already handled by validators; ensure flags consistency
    if target.status == "paid":
        target.is_paid = True
        target.paid_at = target.paid_at or dt.datetime.now(dt.timezone.utc)

@listens_for(WithdrawRequest, "before_update")
def _wr_before_update(_mapper, _conn, target: WithdrawRequest) -> None:
    # re-quantize and recompute when amount/fee changes
    target.amount = _q2(target.amount) or Decimal("0.00")
    target.fee_amount = _q2(target.fee_amount) if target.fee_amount is not None else None
    if target.fee_amount is None:
        target.net_amount = target.amount
    else:
        net = target.amount - target.fee_amount
        if net < Decimal("0.00"):
            raise ValueError("Fee cannot exceed amount")
        target.net_amount = _q2(net)

    if target.status == "paid":
        target.is_paid = True
        target.paid_at = target.paid_at or dt.datetime.now(dt.timezone.utc)
