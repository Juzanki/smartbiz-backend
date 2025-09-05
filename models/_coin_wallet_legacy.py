# backend/models/smart_coin_wallet.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import datetime as dt
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Dict, Any, TYPE_CHECKING

from sqlalchemy import (
    String, Integer, Boolean, DateTime, ForeignKey, UniqueConstraint, CheckConstraint,
    Index, func, text
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.event import listens_for
from sqlalchemy.ext.mutable import MutableDict

from backend.db import Base
# Portable JSON/NUMERIC (PG -> JSONB/NUMERIC; others -> JSON/Numeric)
from backend.models._types import JSON_VARIANT, DECIMAL_TYPE

if TYPE_CHECKING:
    from .user import User


def _to_money(value: float | Decimal | int) -> Decimal:
    """Normalize any numeric-like to money-safe Decimal(2dp)."""
    if isinstance(value, Decimal):
        amt = value
    else:
        amt = Decimal(str(value))
    return amt.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


class SmartCoinWallet(Base):
    """
    SmartCoinWallet — user coin ledger with soft locks & preferences.

    - `balance`: available spendable coins
    - `locked_balance`: coins on hold (e.g., pending payments)
    - `pending_balance`: pipeline accruals (e.g., ad earnings to be settled)
    - JSON `preferences` (mutable) for UI knobs
    - All money uses DECIMAL_TYPE (PG: NUMERIC(18,2))
    """
    __tablename__ = "coin_wallets"
    __mapper_args__ = {"eager_defaults": True}

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True
    )

    # Balances
    balance: Mapped[Decimal] = mapped_column(DECIMAL_TYPE, nullable=False, server_default=text("0"))
    locked_balance: Mapped[Decimal] = mapped_column(DECIMAL_TYPE, nullable=False, server_default=text("0"))
    pending_balance: Mapped[Decimal] = mapped_column(DECIMAL_TYPE, nullable=False, server_default=text("0"))

    currency: Mapped[str] = mapped_column(String(8), nullable=False, server_default=text("'SBZ'"))
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))

    # Preferences (mutable JSON)
    preferences: Mapped[Optional[Dict[str, Any]]] = mapped_column(
        MutableDict.as_mutable(JSON_VARIANT)
    )

    # Timestamps / audit
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    last_activity_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))

    # Optimistic concurrency (simple)
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))

    # Relationship
    user: Mapped["User"] = relationship("User", back_populates="smart_coin_wallet", lazy="selectin")

    __table_args__ = (
        UniqueConstraint("user_id", name="uq_coin_wallets_user_id"),
        CheckConstraint("balance >= 0", name="ck_wallet_balance_nonneg"),
        CheckConstraint("locked_balance >= 0", name="ck_wallet_locked_nonneg"),
        CheckConstraint("pending_balance >= 0", name="ck_wallet_pending_nonneg"),
        # keep currency short & tidy
        CheckConstraint("length(trim(currency)) BETWEEN 2 AND 8", name="ck_wallet_currency_len"),
        Index("ix_wallet_user_active", "user_id", "is_active"),
    )

    # ---------- Business helpers ----------

    def credit(self, amount: float | Decimal | int) -> None:
        """Add coins to available `balance`."""
        amt = _to_money(amount)
        if amt <= 0:
            raise ValueError("credit amount must be positive")
        self.balance = (self.balance or Decimal("0.00")) + amt
        self._touch()

    def debit(self, amount: float | Decimal | int) -> bool:
        """Subtract from available `balance` if funds are enough."""
        amt = _to_money(amount)
        if amt <= 0:
            raise ValueError("debit amount must be positive")
        if (self.balance or Decimal("0.00")) < amt:
            return False
        self.balance = self.balance - amt
        self._touch()
        return True

    def can_afford(self, amount: float | Decimal | int) -> bool:
        """Check if available >= amount."""
        return (self.balance or Decimal("0.00")) >= _to_money(amount)

    # --- Soft holds ---
    def hold(self, amount: float | Decimal | int) -> bool:
        """Move coins from available -> locked (for pending checkout)."""
        amt = _to_money(amount)
        if amt <= 0:
            raise ValueError("hold amount must be positive")
        if (self.balance or Decimal("0.00")) < amt:
            return False
        self.balance = self.balance - amt
        self.locked_balance = (self.locked_balance or Decimal("0.00")) + amt
        self._touch()
        return True

    def release_hold(self, amount: float | Decimal | int) -> None:
        """Return coins from locked -> available (cancel/timeout)."""
        amt = _to_money(amount)
        if amt <= 0:
            raise ValueError("release amount must be positive")
        if (self.locked_balance or Decimal("0.00")) < amt:
            raise ValueError("release exceeds locked_balance")
        self.locked_balance = self.locked_balance - amt
        self.balance = (self.balance or Decimal("0.00")) + amt
        self._touch()

    def consume_hold(self, amount: float | Decimal | int) -> None:
        """Burn coins from locked (payment captured)."""
        amt = _to_money(amount)
        if amt <= 0:
            raise ValueError("consume amount must be positive")
        if (self.locked_balance or Decimal("0.00")) < amt:
            raise ValueError("consume exceeds locked_balance")
        self.locked_balance = self.locked_balance - amt
        self._touch()

    # --- Pending pipeline ---
    def accrue_pending(self, amount: float | Decimal | int) -> None:
        """Increase `pending_balance` (e.g., ad earnings not yet settled)."""
        amt = _to_money(amount)
        if amt <= 0:
            raise ValueError("pending amount must be positive")
        self.pending_balance = (self.pending_balance or Decimal("0.00")) + amt
        self._touch()

    def settle_pending_to_balance(self, amount: float | Decimal | int | None = None) -> None:
        """
        Move from `pending_balance` -> `balance`.
        If amount is None, settle all.
        """
        if amount is None:
            amt = self.pending_balance or Decimal("0.00")
        else:
            amt = _to_money(amount)
        if amt < 0:
            raise ValueError("settle amount must be >= 0")
        if (self.pending_balance or Decimal("0.00")) < amt:
            raise ValueError("settle exceeds pending_balance")
        self.pending_balance = self.pending_balance - amt
        self.balance = (self.balance or Decimal("0.00")) + amt
        self._touch()

    # --- Transfers ---
    def transfer_to(self, other: "SmartCoinWallet", amount: float | Decimal | int) -> bool:
        """Atomic-ish transfer (same session/txn): self -> other."""
        amt = _to_money(amount)
        if amt <= 0:
            raise ValueError("transfer amount must be positive")
        if not self.debit(amt):
            return False
        other.credit(amt)
        return True

    # --- Preferences ---
    def set_preferences(self, **prefs: Any) -> None:
        """Merge/update preferences JSON safely (mutable)."""
        current = self.preferences or {}
        current.update(prefs)
        self.preferences = current
        self._touch()

    # --- Internals ---
    def _touch(self) -> None:
        self.last_activity_at = dt.datetime.now(dt.timezone.utc)
        self.version = (self.version or 1) + 1

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<SmartCoinWallet id={self.id} user={self.user_id} "
            f"bal={self.balance} locked={self.locked_balance} pending={self.pending_balance} {self.currency}>"
        )


# --------- Normalizers / Guards ---------
@listens_for(SmartCoinWallet, "before_insert")
def _wallet_before_insert(_mapper, _conn, w: SmartCoinWallet) -> None:
    if w.currency:
        w.currency = w.currency.strip().upper()[:8]
    # clamp negatives to zero
    for f in ("balance", "locked_balance", "pending_balance"):
        v = getattr(w, f) or Decimal("0.00")
        if v < 0:
            setattr(w, f, Decimal("0.00"))
    if not w.last_activity_at:
        w.last_activity_at = func.now()  # server time


@listens_for(SmartCoinWallet, "before_update")
def _wallet_before_update(_mapper, _conn, w: SmartCoinWallet) -> None:
    if w.currency:
        w.currency = w.currency.strip().upper()[:8]
    for f in ("balance", "locked_balance", "pending_balance"):
        v = getattr(w, f) or Decimal("0.00")
        if v < 0:
            setattr(w, f, Decimal("0.00"))
