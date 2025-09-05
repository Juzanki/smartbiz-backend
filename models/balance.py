# backend/models/balance.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import datetime as dt
from decimal import Decimal, ROUND_DOWN
from typing import Optional, TYPE_CHECKING

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship, validates, Session
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.event import listens_for

# ─────────── Portable NUMERIC(18,2) (PG → NUMERIC, others → Numeric) ───────────
from sqlalchemy import Numeric as SA_NUMERIC
try:
    from sqlalchemy.dialects.postgresql import NUMERIC as PG_NUMERIC  # type: ignore
    DECIMAL_TYPE = PG_NUMERIC(18, 2)
except Exception:  # pragma: no cover
    DECIMAL_TYPE = SA_NUMERIC(18, 2)

from backend.db import Base

if TYPE_CHECKING:
    from .user import User


# Helper: quantize to 2dp like money (ROUND_DOWN = deterministic “towards zero”)
def _q(amount: Decimal | int | float | str, quant: str = "0.01") -> Decimal:
    d = amount if isinstance(amount, Decimal) else Decimal(str(amount))
    return d.quantize(Decimal(quant), rounding=ROUND_DOWN)


class Balance(Base):
    """
    Wallet ya mtumiaji (1:1 na User, currency-aware).

    • total     — jumla yote
    • reserved  — kiasi kilichoshikiliwa (hold)
    • available — (hybrid) total - reserved
    • version_id — optimistic locking (OCC)
    """
    __tablename__ = "balances"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    # 1:1 na User (lakini currency inaweza kutofautiana ikiwa utataka multi-currency)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
        unique=True,          # ikiwa unataka wallet moja tu kwa user; toa hii ukitaka multi-currency
    )

    # ISO code (TZS, USD, n.k.)
    currency: Mapped[str] = mapped_column(String(8), default="TZS", nullable=False, index=True)

    # Fedha
    total: Mapped[Decimal] = mapped_column(DECIMAL_TYPE, nullable=False, server_default=text("0"))
    reserved: Mapped[Decimal] = mapped_column(DECIMAL_TYPE, nullable=False, server_default=text("0"))

    # Nyakati
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    last_activity_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True), index=True)

    # Optimistic concurrency control (OCC)
    version_id: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    __mapper_args__ = {
        "version_id_col": version_id,
        "version_id_generator": lambda v: (v or 0) + 1,
    }

    # Uhusiano
    user: Mapped["User"] = relationship(
        "User",
        back_populates="balance",
        foreign_keys=[user_id],
        passive_deletes=True,
        uselist=False,
        lazy="selectin",
    )

    # ---------- Hybrids ----------
    @hybrid_property
    def available(self) -> Decimal:
        return (self.total or Decimal("0")) - (self.reserved or Decimal("0"))

    # ---------- Normalizers ----------
    @validates("currency")
    def _norm_currency(self, _k: str, v: str) -> str:
        v = (v or "").strip().upper()
        if len(v) < 2 or len(v) > 8:
            raise ValueError("currency must be 2–8 chars (ISO code like TZS, USD)")
        return v

    # ---------- Mantiki ya biashara (side-effect free; hakuna commit hapa) ----------
    def credit(self, amount: Decimal | int | float | str) -> None:
        amt = _q(amount)
        if amt <= 0:
            raise ValueError("Credit amount must be positive.")
        self.total = _q((self.total or Decimal("0")) + amt)
        self._touch()

    def debit(self, amount: Decimal | int | float | str) -> None:
        amt = _q(amount)
        if amt <= 0:
            raise ValueError("Debit amount must be positive.")
        if self.available < amt:
            raise ValueError("Insufficient available balance.")
        self.total = _q((self.total or Decimal("0")) - amt)
        self._touch()

    def reserve(self, amount: Decimal | int | float | str) -> None:
        amt = _q(amount)
        if amt <= 0:
            raise ValueError("Reserve amount must be positive.")
        if self.available < amt:
            raise ValueError("Insufficient available to reserve.")
        self.reserved = _q((self.reserved or Decimal("0")) + amt)
        self._touch()

    def release(self, amount: Decimal | int | float | str) -> None:
        amt = _q(amount)
        if amt <= 0:
            raise ValueError("Release amount must be positive.")
        if (self.reserved or Decimal("0")) < amt:
            raise ValueError("Cannot release more than reserved.")
        self.reserved = _q((self.reserved or Decimal("0")) - amt)
        self._touch()

    def capture_reserved(self, amount: Decimal | int | float | str) -> None:
        amt = _q(amount)
        if amt <= 0:
            raise ValueError("Capture amount must be positive.")
        if (self.reserved or Decimal("0")) < amt:
            raise ValueError("Cannot capture more than reserved.")
        # tumia reserved → total (kutoka hold kwenda charge halisi)
        self.reserved = _q((self.reserved or Decimal("0")) - amt)
        self.total = _q((self.total or Decimal("0")) - amt)
        self._touch()

    # ---------- QoL helpers ----------
    def can_debit(self, amount: Decimal | int | float | str) -> bool:
        return self.available >= _q(amount)

    def can_reserve(self, amount: Decimal | int | float | str) -> bool:
        return self.available >= _q(amount)

    def _touch(self) -> None:
        self.last_activity_at = dt.datetime.now(dt.timezone.utc)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Balance user_id={self.user_id} total={self.total} reserved={self.reserved} currency={self.currency}>"

    # ---------- Vizuizi & Fahirisi ----------
    __table_args__ = (
        CheckConstraint("total >= 0", name="ck_balance_total_nonneg"),
        CheckConstraint("reserved >= 0", name="ck_balance_reserved_nonneg"),
        CheckConstraint("total >= reserved", name="ck_balance_total_ge_reserved"),
        CheckConstraint("length(trim(currency)) >= 2", name="ck_balance_currency_len"),
        CheckConstraint("version_id >= 1", name="ck_balance_version_min"),
        Index("ix_balances_user_currency", "user_id", "currency"),
    )

    # ---------- Atomic helpers zinazotumia Session (in-transaction) ----------
    @classmethod
    def ensure(cls, session: Session, *, user_id: int, currency: str = "TZS") -> "Balance":
        """
        Pata (au tengeneza) wallet ya user. Haina commit; tumia ndani ya transaction.
        """
        row = session.query(cls).filter_by(user_id=user_id).one_or_none()
        if row:
            return row
        row = cls(user_id=user_id, currency=currency)
        session.add(row)
        return row

    @classmethod
    def get_for_update(
        cls, session: Session, *, user_id: int, with_for_update: bool = True
    ) -> "Balance | None":
        """
        Chukua wallet ya user kwa lock (SELECT FOR UPDATE) ili kuzuia race conditions.
        """
        q = session.query(cls).filter_by(user_id=user_id)
        if with_for_update:
            q = q.with_for_update()
        return q.one_or_none()

    @classmethod
    def transfer(
        cls,
        session: Session,
        *,
        sender_id: int,
        receiver_id: int,
        amount: Decimal | int | float | str,
        currency: Optional[str] = None,
        require_same_currency: bool = True,
        use_reserved: bool = False,
    ) -> tuple["Balance", "Balance"]:
        """
        **Atomic transfer** kati ya wallets mbili (dani ya transaction yako).
        - Lock zote mbili (FOR UPDATE) kwa utaratibu thabiti (asc id) ili kuepuka deadlocks.
        - Ikiwa `use_reserved=True`: kwanza `reserve()` kisha `capture_reserved()` kwa sender.
          La sivyo: `debit()` moja kwa moja.
        - `require_same_currency=True`: weka vyote kwenye sarafu moja; vinginevyo toa `currency` kwa walengwa
          (hii logic ya FX iko nje ya scope ya model).

        Returns: (sender, receiver) already mutated (no commit).
        """
        amt = _q(amount)
        if amt <= 0:
            raise ValueError("Transfer amount must be positive.")

        # Lock in deterministic order
        a_id, b_id = sorted([sender_id, receiver_id])
        a = cls.get_for_update(session, user_id=a_id)
        b = cls.get_for_update(session, user_id=b_id)
        if not a or not b:
            raise ValueError("Sender/receiver wallet not found.")

        sender = a if a.user_id == sender_id else b
        receiver = b if a.user_id == sender_id else a

        if require_same_currency and sender.currency != receiver.currency:
            raise ValueError("Currency mismatch between wallets.")

        # (Hiari) weka currency ya receiver kama imetolewa na tofauti inaruhusiwa
        if (not require_same_currency) and currency:
            receiver.currency = currency

        # Move funds
        if use_reserved:
            # Hold then capture for sender
            if not sender.can_reserve(amt):
                raise ValueError("Insufficient available to reserve for transfer.")
            sender.reserve(amt)
            sender.capture_reserved(amt)
        else:
            if not sender.can_debit(amt):
                raise ValueError("Insufficient balance for transfer.")
            sender.debit(amt)

        receiver.credit(amt)
        return sender, receiver


# ---------------- Normalizers / Guards ----------------
@listens_for(Balance, "before_insert")
def _bal_before_insert(_m, _c, t: Balance) -> None:
    # clean & clamp
    if t.currency:
        t.currency = t.currency.strip().upper()[:8]
    # ensure non-negative
    t.total = _q(t.total or 0)
    t.reserved = _q(t.reserved or 0)
    if t.total < 0:
        t.total = Decimal("0.00")
    if t.reserved < 0:
        t.reserved = Decimal("0.00")
    if t.total < t.reserved:
        t.reserved = t.total
    t.last_activity_at = t.last_activity_at or dt.datetime.now(dt.timezone.utc)


@listens_for(Balance, "before_update")
def _bal_before_update(_m, _c, t: Balance) -> None:
    if t.currency:
        t.currency = t.currency.strip().upper()[:8]
    t.total = _q(t.total or 0)
    t.reserved = _q(t.reserved or 0)
    if t.total < 0:
        t.total = Decimal("0.00")
    if t.reserved < 0:
        t.reserved = Decimal("0.00")
    if t.total < t.reserved:
        t.reserved = t.total
    t.last_activity_at = dt.datetime.now(dt.timezone.utc)
