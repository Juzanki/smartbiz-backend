# backend/models/wallet.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import datetime as dt
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Tuple, Dict, Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    JSON,
    Numeric,
    String,
    UniqueConstraint,
    event,
    func,
    select,
    update,
)
from sqlalchemy.orm import Mapped, Session, mapped_column, relationship

from pydantic import BaseModel, Field

from backend.db import Base
from backend.models._types import JSON_VARIANT, DECIMAL_TYPE, as_mutable_json

# --------------------------- Enums & constants --------------------------- #
TXN_TYPES = ("deposit", "withdraw", "transfer", "convert", "adjust")
TXN_STATUSES = ("pending", "success", "failed", "reversed")
FIAT_DEFAULT = "TZS"
COIN_DEFAULT = "SMART"

_DEC2 = Numeric(18, 2)

# ------------------------------- Models --------------------------------- #
class Wallet(Base):
    """
    Fiat + internal credits wallet (one-per-user).
    - Decimal everywhere (2dp)
    - TZ-aware timestamps
    - Idempotent ops (via idempotency_key on ledger)
    - Safe guarded updates to avoid race conditions
    """
    __tablename__ = "wallets"
    __mapper_args__ = {"eager_defaults": True}

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        unique=True, nullable=False, index=True
    )

    # Balances
    balance_fiat: Mapped[Decimal] = mapped_column(_DEC2, default=Decimal("0.00"), nullable=False)
    balance_coin: Mapped[Decimal] = mapped_column(_DEC2, default=Decimal("0.00"), nullable=False)
    fiat_currency: Mapped[str] = mapped_column(String(10), default=FIAT_DEFAULT, nullable=False)
    coin_symbol:   Mapped[str] = mapped_column(String(16), default=COIN_DEFAULT, nullable=False)

    # Timestamps
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    # Relationships
    # NOTE: reverse property on User must be `money_wallet` (see snippet below)
    owner: Mapped["User"] = relationship("User", back_populates="money_wallet", uselist=False, lazy="selectin")

    transactions: Mapped[list["WalletTransaction"]] = relationship(
        "WalletTransaction",
        back_populates="wallet",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="WalletTransaction.id.desc()",
        lazy="selectin",
    )

    __table_args__ = (
        UniqueConstraint("user_id", name="uq_wallet_user"),
        CheckConstraint("balance_fiat >= 0", name="ck_wallet_fiat_nonneg"),
        CheckConstraint("balance_coin >= 0", name="ck_wallet_coin_nonneg"),
        Index("ix_wallet_currencies", "fiat_currency", "coin_symbol"),
    )

    # ---------------------- Domain helpers ---------------------- #
    @staticmethod
    def _q2(value: Decimal | float | int) -> Decimal:
        d = Decimal(str(value))
        return d.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    def _ensure_amount(self, amount: Decimal | float | int) -> Decimal:
        amt = self._q2(amount)
        if amt <= 0:
            raise ValueError("Amount must be > 0")
        return amt

    # ---- Idempotency dedupe ----
    @staticmethod
    def _already_applied(db: Session, wallet_id: int, idem_key: Optional[str]) -> bool:
        if not idem_key:
            return False
        stmt = select(WalletTransaction.id).where(
            WalletTransaction.wallet_id == wallet_id,
            WalletTransaction.idempotency_key == idem_key
        ).limit(1)
        return db.execute(stmt).first() is not None

    # ---- Deposit (atomic) ----
    def deposit(
        self,
        db: Session,
        amount: Decimal | float | int,
        *,
        currency: str = FIAT_DEFAULT,
        note: Optional[str] = None,
        fee: Decimal | float | int = Decimal("0"),
        idempotency_key: Optional[str] = None,
        meta: Optional[Dict[str, Any]] = None,
    ) -> "WalletTransaction":
        if self._already_applied(db, self.id, idempotency_key):
            return db.execute(
                select(WalletTransaction)
                .where(WalletTransaction.wallet_id == self.id,
                       WalletTransaction.idempotency_key == idempotency_key)
                .order_by(WalletTransaction.id.desc())
                .limit(1)
            ).scalar_one()

        amt = self._ensure_amount(amount)
        fee_amt = self._q2(fee)
        if fee_amt < 0:
            raise ValueError("Fee cannot be negative")
        net = self._q2(amt - fee_amt)
        if net <= 0:
            raise ValueError("Net amount must be > 0")

        cur = currency.upper()
        if cur == self.fiat_currency.upper():
            db.execute(
                update(Wallet).where(Wallet.id == self.id)
                .values(balance_fiat=Wallet.balance_fiat + net)
            )
        elif cur == self.coin_symbol.upper():
            db.execute(
                update(Wallet).where(Wallet.id == self.id)
                .values(balance_coin=Wallet.balance_coin + net)
            )
        else:
            raise ValueError("Unsupported currency")

        txn = WalletTransaction.new(
            wallet_id=self.id,
            type_="deposit",
            amount=amt,
            currency=cur,
            status="success",
            desc=note,
            idempotency_key=idempotency_key,
            fee_amount=fee_amt if fee_amt > 0 else None,
            meta=meta,
        )
        db.add(txn)
        return txn

    # ---- Withdraw (atomic, guarded) ----
    def withdraw(
        self,
        db: Session,
        amount: Decimal | float | int,
        *,
        currency: str = FIAT_DEFAULT,
        note: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        meta: Optional[Dict[str, Any]] = None,
    ) -> "WalletTransaction":
        if self._already_applied(db, self.id, idempotency_key):
            return db.execute(
                select(WalletTransaction)
                .where(WalletTransaction.wallet_id == self.id,
                       WalletTransaction.idempotency_key == idempotency_key)
                .order_by(WalletTransaction.id.desc())
                .limit(1)
            ).scalar_one()

        amt = self._ensure_amount(amount)
        cur = currency.upper()

        if cur == self.fiat_currency.upper():
            res = db.execute(
                update(Wallet)
                .where(Wallet.id == self.id, Wallet.balance_fiat >= amt)
                .values(balance_fiat=Wallet.balance_fiat - amt)
            )
        elif cur == self.coin_symbol.upper():
            res = db.execute(
                update(Wallet)
                .where(Wallet.id == self.id, Wallet.balance_coin >= amt)
                .values(balance_coin=Wallet.balance_coin - amt)
            )
        else:
            raise ValueError("Unsupported currency")

        if res.rowcount == 0:
            raise ValueError("Insufficient balance")

        txn = WalletTransaction.new(
            wallet_id=self.id,
            type_="withdraw",
            amount=amt,
            currency=cur,
            status="success",
            desc=note,
            idempotency_key=idempotency_key,
            meta=meta,
        )
        db.add(txn)
        return txn

    # ---- Convert fiat <-> coin ----
    def convert(
        self,
        db: Session,
        amount: Decimal | float | int,
        *,
        direction: str,               # "fiat_to_coin" | "coin_to_fiat"
        rate: Decimal | float | int,  # COIN per 1 FIAT; inverse for coin->fiat
        note: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        meta: Optional[Dict[str, Any]] = None,
    ) -> "WalletTransaction":
        amt = self._ensure_amount(amount)
        rt = self._q2(rate)
        if rt <= 0:
            raise ValueError("Rate must be > 0")

        if direction == "fiat_to_coin":
            res = db.execute(
                update(Wallet)
                .where(Wallet.id == self.id, Wallet.balance_fiat >= amt)
                .values(balance_fiat=Wallet.balance_fiat - amt)
            )
            if res.rowcount == 0:
                raise ValueError("Insufficient fiat balance")
            db.execute(
                update(Wallet).where(Wallet.id == self.id)
                .values(balance_coin=Wallet.balance_coin + self._q2(amt * rt))
            )
            desc = note or f"Converted {amt} {self.fiat_currency} -> {self._q2(amt * rt)} {self.coin_symbol} @ {rt}"
            txn_currency = self.fiat_currency
        elif direction == "coin_to_fiat":
            res = db.execute(
                update(Wallet)
                .where(Wallet.id == self.id, Wallet.balance_coin >= amt)
                .values(balance_coin=Wallet.balance_coin - amt)
            )
            if res.rowcount == 0:
                raise ValueError("Insufficient coin balance")
            db.execute(
                update(Wallet).where(Wallet.id == self.id)
                .values(balance_fiat=Wallet.balance_fiat + self._q2(amt / rt))
            )
            desc = note or f"Converted {amt} {self.coin_symbol} -> {self._q2(amt / rt)} {self.fiat_currency} @ {rt}"
            txn_currency = self.coin_symbol
        else:
            raise ValueError("direction must be 'fiat_to_coin' or 'coin_to_fiat'")

        txn = WalletTransaction.new(
            wallet_id=self.id,
            type_="convert",
            amount=amt,
            currency=txn_currency,
            status="success",
            desc=desc,
            idempotency_key=idempotency_key,
            meta=meta,
            rate=rt,
            direction=direction,
        )
        db.add(txn)
        return txn

    # ---- Transfer (same-currency) ----
    def transfer(
        self,
        db: Session,
        to_wallet: "Wallet",
        amount: Decimal | float | int,
        *,
        currency: str = FIAT_DEFAULT,
        note: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        meta: Optional[Dict[str, Any]] = None,
    ) -> Tuple["WalletTransaction", "WalletTransaction"]:
        if self.id == to_wallet.id:
            raise ValueError("Cannot transfer to the same wallet")
        amt = self._ensure_amount(amount)
        cur = currency.upper()

        if cur == self.fiat_currency.upper() == to_wallet.fiat_currency.upper():
            res = db.execute(
                update(Wallet)
                .where(Wallet.id == self.id, Wallet.balance_fiat >= amt)
                .values(balance_fiat=Wallet.balance_fiat - amt)
            )
            if res.rowcount == 0:
                raise ValueError("Insufficient fiat balance")
            db.execute(
                update(Wallet).where(Wallet.id == to_wallet.id)
                .values(balance_fiat=Wallet.balance_fiat + amt)
            )
        elif cur == self.coin_symbol.upper() == to_wallet.coin_symbol.upper():
            res = db.execute(
                update(Wallet)
                .where(Wallet.id == self.id, Wallet.balance_coin >= amt)
                .values(balance_coin=Wallet.balance_coin - amt)
            )
            if res.rowcount == 0:
                raise ValueError("Insufficient coin balance")
            db.execute(
                update(Wallet).where(Wallet.id == to_wallet.id)
                .values(balance_coin=Wallet.balance_coin + amt)
            )
        else:
            raise ValueError("Mismatched or unsupported currency for transfer")

        debit = WalletTransaction.new(
            wallet_id=self.id,
            type_="transfer",
            amount=amt,
            currency=cur,
            status="success",
            desc=note or f"to:{to_wallet.id}",
            idempotency_key=idempotency_key,
            meta=meta,
            contra_wallet_id=to_wallet.id,
            direction="debit",
        )
        credit = WalletTransaction.new(
            wallet_id=to_wallet.id,
            type_="transfer",
            amount=amt,
            currency=cur,
            status="success",
            desc=note or f"from:{self.id}",
            meta=meta,
            contra_wallet_id=self.id,
            direction="credit",
        )
        db.add_all([debit, credit])
        return debit, credit

class WalletTransaction(Base):
    """Immutable ledger entry."""
    __tablename__ = "wallet_transactions"
    __mapper_args__ = {"eager_defaults": True}

    id: Mapped[int]        = mapped_column(Integer, primary_key=True, index=True)
    wallet_id: Mapped[int] = mapped_column(ForeignKey("wallets.id", ondelete="CASCADE"), nullable=False, index=True)

    type: Mapped[str]      = mapped_column(Enum(*TXN_TYPES, name="wallet_txn_type"), nullable=False)
    status: Mapped[str]    = mapped_column(Enum(*TXN_STATUSES, name="wallet_txn_status"), default="success", nullable=False)

    amount: Mapped[Decimal]   = mapped_column(_DEC2, nullable=False)
    currency: Mapped[str]     = mapped_column(String(12), default=FIAT_DEFAULT, nullable=False)

    description: Mapped[Optional[str]] = mapped_column(String(255), default=None)
    idempotency_key: Mapped[Optional[str]] = mapped_column(String(64), index=True, default=None)
    external_ref: Mapped[Optional[str]] = mapped_column(String(64), index=True, default=None)
    meta: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON, default=None)

    fee_amount: Mapped[Optional[Decimal]] = mapped_column(_DEC2, default=None)
    rate: Mapped[Optional[Decimal]] = mapped_column(_DEC2, default=None)
    direction: Mapped[Optional[str]] = mapped_column(String(16), default=None)
    contra_wallet_id: Mapped[Optional[int]] = mapped_column(Integer, index=True, default=None)

    balance_fiat_after: Mapped[Optional[Decimal]] = mapped_column(_DEC2, default=None)
    balance_coin_after: Mapped[Optional[Decimal]] = mapped_column(_DEC2, default=None)

    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)

    wallet: Mapped["Wallet"] = relationship("Wallet", back_populates="transactions", lazy="joined")

    __table_args__ = (
        CheckConstraint("amount > 0", name="ck_wallet_txn_amount_pos"),
        UniqueConstraint("wallet_id", "idempotency_key", name="uq_wallet_txn_idemp_per_wallet"),
        Index("ix_wallet_txn_wallet_created", "wallet_id", "created_at"),
        Index("ix_wallet_txn_type_status", "type", "status"),
    )

    @classmethod
    def new(
        cls,
        wallet_id: int,
        type_: str,
        amount: Decimal | float | int,
        currency: str,
        status: str = "success",
        desc: Optional[str] = None,
        *,
        idempotency_key: Optional[str] = None,
        external_ref: Optional[str] = None,
        fee_amount: Optional[Decimal | float | int] = None,
        meta: Optional[Dict[str, Any]] = None,
        rate: Optional[Decimal | float | int] = None,
        direction: Optional[str] = None,
        contra_wallet_id: Optional[int] = None,
    ) -> "WalletTransaction":
        if type_ not in TXN_TYPES:
            raise ValueError("Invalid transaction type")
        if status not in TXN_STATUSES:
            raise ValueError("Invalid transaction status")

        def _q2(val) -> Optional[Decimal]:
            if val is None:
                return None
            return Decimal(str(val)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

        amt = _q2(amount)
        if amt is None or amt <= 0:
            raise ValueError("Amount must be > 0")

        return cls(
            wallet_id=wallet_id,
            type=type_,
            amount=amt,
            currency=currency.upper(),
            status=status,
            description=desc[:255] if desc else None,
            idempotency_key=(idempotency_key or None)[:64] if idempotency_key else None,
            external_ref=(external_ref or None)[:64] if external_ref else None,
            fee_amount=_q2(fee_amount),
            meta=meta,
            rate=_q2(rate),
            direction=direction,
            contra_wallet_id=contra_wallet_id,
        )

# --------------------- Touch updated_at on balance changes --------------------- #
@event.listens_for(Wallet.balance_fiat, "set", propagate=True)
@event.listens_for(Wallet.balance_coin, "set", propagate=True)
def _wallet_touch_timestamp(target: Wallet, value, oldvalue, initiator):
    target.updated_at = dt.datetime.now(dt.timezone.utc)
    return value

# ----------------------------- Lightweight DTOs ----------------------------- #
class WalletOut(BaseModel):
    id: int
    user_id: int
    fiat: Decimal = Field(..., alias="balance_fiat")
    coin: Decimal = Field(..., alias="balance_coin")
    fiat_currency: str
    coin_symbol: str
    updated_at: dt.datetime

    class Config:
        from_attributes = True
        populate_by_name = True

class TxnOut(BaseModel):
    id: int
    wallet_id: int
    type: str
    status: str
    amount: Decimal
    currency: str
    created_at: dt.datetime
    description: Optional[str] = None
    fee_amount: Optional[Decimal] = None
    rate: Optional[Decimal] = None
    direction: Optional[str] = None
    contra_wallet_id: Optional[int] = None
    balance_fiat_after: Optional[Decimal] = None
    balance_coin_after: Optional[Decimal] = None
    external_ref: Optional[str] = None
    idempotency_key: Optional[str] = None

    class Config:
        from_attributes = True



