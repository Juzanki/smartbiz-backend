# backend/models/smart_coin_transaction.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import enum
import datetime as dt
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Dict, Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum as SQLEnum,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
    Numeric,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship, validates

from backend.db import Base
from backend.models._types import JSON_VARIANT, as_mutable_json

# --------------------------- Constants & helpers ---------------------------

# precise internal coin math (e.g., SMART); keep 6dp for coins, 10dp for rates
_DEC6 = Numeric(24, 6)
_RATE  = Numeric(24, 10)

DEFAULT_COIN = "SMART"

class TxnType(str, enum.Enum):
    earn          = "earn"          # in-app earnings (ads, rewards)
    spend         = "spend"         # purchases/consumption
    transfer_in   = "transfer_in"   # from another wallet
    transfer_out  = "transfer_out"  # to another wallet
    deposit       = "deposit"       # fiat->coin bridge, top-up
    withdraw      = "withdraw"      # coin->fiat bridge, cash-out
    convert_in    = "convert_in"    # conversion result (+)
    convert_out   = "convert_out"   # conversion source (-)
    adjust        = "adjust"        # admin/compensation/corrections

class TxnStatus(str, enum.Enum):
    pending  = "pending"
    success  = "success"
    failed   = "failed"
    reversed = "reversed"

def _q6(x: Decimal | float | int | str) -> Decimal:
    return Decimal(str(x)).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)

def _norm_symbol(s: str | None) -> str:
    v = (s or DEFAULT_COIN).strip().upper()
    if not (1 <= len(v) <= 16):
        raise ValueError("coin_symbol must be 1..16 chars")
    return v

def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)

# ------------------------------- Model ------------------------------------

class SmartCoinTransaction(Base):
    """
    SmartCoinTransaction — immutable ledger line for SmartCoinWallet.

    Upgrades:
    - Python Enums (validated), rich constraints & targeted indexes
    - Idempotency + external refs + group_id (batch/transfer pair)
    - Balance snapshots (before/after) for auditability
    - Fiat bridge fields (fiat_amount/currency/rate) and fee_amount/fee_meta
    - Reversals & counterparty (double-entry friendly)
    - Typed JSON (mutable) and safe factory helpers
    """
    __tablename__ = "smart_coin_transactions"
    __mapper_args__ = {"eager_defaults": True}
    __table_args__ = (
        # hot paths
        Index("ix_sc_txn_wallet_time", "wallet_id", "created_at"),
        Index("ix_sc_txn_user_time", "user_id", "created_at"),
        Index("ix_sc_txn_type_status", "type", "status"),
        Index("ix_sc_txn_external", "external_ref"),
        Index("ix_sc_txn_group", "group_id"),
        # dedupe scopes
        UniqueConstraint("wallet_id", "idempotency_key", name="uq_sc_txn_wallet_idem"),
        # guards
        CheckConstraint("amount > 0", name="ck_sc_txn_amount_pos"),
        CheckConstraint("fee_amount >= 0", name="ck_sc_txn_fee_nonneg"),
        CheckConstraint("length(coin_symbol) BETWEEN 1 AND 16", name="ck_sc_txn_symbol_len"),
        CheckConstraint("fiat_currency IS NULL OR length(fiat_currency) = 3", name="ck_sc_txn_fiat_ccy_iso3"),
    )

    # Keys
    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)

    # Ownership & scope
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    wallet_id: Mapped[int] = mapped_column(ForeignKey("smart_coin_wallets.id", ondelete="CASCADE"), nullable=False, index=True)

    # Business
    coin_symbol: Mapped[str] = mapped_column(String(16), default=DEFAULT_COIN, nullable=False, index=True)
    type: Mapped[TxnType] = mapped_column(
        SQLEnum(TxnType, name="sc_txn_type", native_enum=False, validate_strings=True),
        nullable=False,
        index=True,
    )
    status: Mapped[TxnStatus] = mapped_column(
        SQLEnum(TxnStatus, name="sc_txn_status", native_enum=False, validate_strings=True),
        default=TxnStatus.success,
        nullable=False,
        index=True,
    )
    amount: Mapped[Decimal] = mapped_column(_DEC6, nullable=False)
    description: Mapped[Optional[str]] = mapped_column(String(255))

    # Fees (in coin units unless noted in fee_meta)
    fee_amount: Mapped[Decimal] = mapped_column(_DEC6, nullable=False, default=Decimal("0.000000"))
    fee_meta: Mapped[Optional[Dict[str, Any]]] = mapped_column(as_mutable_json(JSON_VARIANT))

    # Fiat/bridge info (optional)
    fiat_amount: Mapped[Optional[Decimal]] = mapped_column(_DEC6)  # positive number
    fiat_currency: Mapped[Optional[str]] = mapped_column(String(3), index=True)
    rate_coin_per_fiat: Mapped[Optional[Decimal]] = mapped_column(_RATE)  # coin per 1 fiat unit

    # Snapshots (ledger immutability helpers)
    balance_before: Mapped[Optional[Decimal]] = mapped_column(_DEC6)
    balance_after:  Mapped[Optional[Decimal]] = mapped_column(_DEC6)

    # Cross-refs / dedupe / grouping
    counterparty_wallet_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("smart_coin_wallets.id", ondelete="SET NULL"),
        index=True,
    )
    reversal_of_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("smart_coin_transactions.id", ondelete="SET NULL"),
        index=True,
        doc="If this is a reversal, link to the original transaction.",
    )
    external_ref: Mapped[Optional[str]] = mapped_column(String(64), index=True)        # PSP/bridge txn id
    idempotency_key: Mapped[Optional[str]] = mapped_column(String(64), index=True)     # client/server dedupe key
    group_id: Mapped[Optional[str]] = mapped_column(String(36), index=True)            # batch/transfer pair

    meta: Mapped[Optional[Dict[str, Any]]] = mapped_column(as_mutable_json(JSON_VARIANT))

    # Timestamps / lifecycle
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )
    posted_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))   # when applied to wallet
    settled_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))  # after confirmations
    failed_at:  Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    reversed_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))

    # Relationships
    user: Mapped["User"] = relationship("User", back_populates="smart_coin_transactions", lazy="selectin")
    wallet: Mapped["SmartCoinWallet"] = relationship(
        "SmartCoinWallet", back_populates="transactions", foreign_keys=[wallet_id], lazy="selectin"
    )
    counterparty_wallet: Mapped[Optional["SmartCoinWallet"]] = relationship(
        "SmartCoinWallet", foreign_keys=[counterparty_wallet_id], lazy="selectin"
    )
    reversal_of: Mapped[Optional["SmartCoinTransaction"]] = relationship(
        "SmartCoinTransaction",
        remote_side=[id],
        lazy="selectin",
        doc="Original txn that this one reverses.",
    )

    # --------------------------- Validation ---------------------------

    @validates("coin_symbol")
    def _v_symbol(self, _k: str, v: str) -> str:
        return _norm_symbol(v)

    # ------------------------------ Factory ------------------------------

    @classmethod
    def new(
        cls,
        *,
        user_id: int,
        wallet_id: int,
        type_: TxnType | str,
        amount: Decimal | float | int | str,
        coin_symbol: str | None = None,
        status: TxnStatus | str = TxnStatus.success,
        description: Optional[str] = None,
        external_ref: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        counterparty_wallet_id: Optional[int] = None,
        group_id: Optional[str] = None,
        fee_amount: Decimal | float | int | str = 0,
        fiat_amount: Optional[Decimal | float | int | str] = None,
        fiat_currency: Optional[str] = None,
        rate_coin_per_fiat: Optional[Decimal | float | int | str] = None,
        meta: Optional[Dict[str, Any]] = None,
        balance_before: Optional[Decimal | float | int | str] = None,
        balance_after: Optional[Decimal | float | int | str] = None,
    ) -> "SmartCoinTransaction":
        t = TxnType(type_) if not isinstance(type_, TxnType) else type_
        s = TxnStatus(status) if not isinstance(status, TxnStatus) else status
        fa = None if fiat_amount is None else _q6(fiat_amount)
        rb = None if rate_coin_per_fiat is None else Decimal(str(rate_coin_per_fiat))
        bb = None if balance_before is None else _q6(balance_before)
        ba = None if balance_after is None else _q6(balance_after)
        return cls(
            user_id=int(user_id),
            wallet_id=int(wallet_id),
            type=t,
            status=s,
            amount=_q6(amount),
            coin_symbol=_norm_symbol(coin_symbol),
            description=(description or None),
            external_ref=(external_ref or None),
            idempotency_key=(idempotency_key or None),
            counterparty_wallet_id=counterparty_wallet_id,
            group_id=group_id,
            fee_amount=_q6(fee_amount),
            fiat_amount=fa,
            fiat_currency=(fiat_currency or None),
            rate_coin_per_fiat=rb,
            meta=meta,
            balance_before=bb,
            balance_after=ba,
        )

    @classmethod
    def new_transfer_pair(
        cls,
        *,
        user_id_from: int,
        wallet_id_from: int,
        user_id_to: int,
        wallet_id_to: int,
        amount: Decimal | float | int | str,
        coin_symbol: str | None = None,
        group_id: Optional[str] = None,
        description: Optional[str] = None,
        idempotency_key_out: Optional[str] = None,
        idempotency_key_in: Optional[str] = None,
        meta: Optional[Dict[str, Any]] = None,
    ) -> tuple["SmartCoinTransaction", "SmartCoinTransaction"]:
        """Create (transfer_out, transfer_in) pair sharing group_id and counterparty links."""
        gid = group_id or f"trf:{_utcnow().timestamp()}"
        amt = _q6(amount)
        sym = _norm_symbol(coin_symbol)
        out_tx = cls.new(
            user_id=user_id_from,
            wallet_id=wallet_id_from,
            type_=TxnType.transfer_out,
            amount=amt,
            coin_symbol=sym,
            description=description,
            idempotency_key=idempotency_key_out,
            counterparty_wallet_id=wallet_id_to,
            group_id=gid,
            meta=meta,
        )
        in_tx = cls.new(
            user_id=user_id_to,
            wallet_id=wallet_id_to,
            type_=TxnType.transfer_in,
            amount=amt,
            coin_symbol=sym,
            description=description,
            idempotency_key=idempotency_key_in,
            counterparty_wallet_id=wallet_id_from,
            group_id=gid,
            meta=meta,
        )
        return out_tx, in_tx

    # --------------------------- Status helpers ---------------------------

    def mark_pending(self) -> None:
        self.status = TxnStatus.pending

    def mark_success(self) -> None:
        self.status = TxnStatus.success
        self.settled_at = self.settled_at or _utcnow()

    def mark_failed(self) -> None:
        self.status = TxnStatus.failed
        self.failed_at = _utcnow()

    def mark_reversed(self, *, reversal_of_id: Optional[int] = None) -> None:
        self.status = TxnStatus.reversed
        self.reversed_at = _utcnow()
        if reversal_of_id:
            self.reversal_of_id = reversal_of_id

    # ------------------------------ Semantics ------------------------------

    @property
    def is_credit(self) -> bool:
        return self.type in {
            TxnType.earn, TxnType.transfer_in, TxnType.deposit, TxnType.convert_in, TxnType.adjust
        }

    @property
    def is_debit(self) -> bool:
        return self.type in {
            TxnType.spend, TxnType.transfer_out, TxnType.withdraw, TxnType.convert_out
        }

    @property
    def delta(self) -> Decimal:
        """Signed effect on balance (amount - fee for credits; -(amount+fee) for debits)."""
        amt = Decimal(self.amount or 0)
        fee = Decimal(self.fee_amount or 0)
        if self.is_credit:
            return (amt - fee).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)
        if self.is_debit:
            return (-(amt + fee)).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)
        # adjust could be either; treat as credit unless meta["direction"] == "debit"
        if self.type == TxnType.adjust and (self.meta or {}).get("direction") == "debit":
            return (-(amt + fee)).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)
        return (amt - fee).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)

    def stamp_posted(self, *, balance_before: Decimal | float | int | str, balance_after: Decimal | float | int | str) -> None:
        self.posted_at = _utcnow()
        self.balance_before = _q6(balance_before)
        self.balance_after = _q6(balance_after)

    # ------------------------- API/Feed projection -------------------------

    def to_public_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "user_id": self.user_id,
            "wallet_id": self.wallet_id,
            "coin": self.coin_symbol,
            "type": self.type.value,
            "status": self.status.value,
            "amount": str(self.amount),
            "fee": str(self.fee_amount or Decimal("0")),
            "delta": str(self.delta),
            "description": self.description,
            "external_ref": self.external_ref,
            "counterparty_wallet_id": self.counterparty_wallet_id,
            "group_id": self.group_id,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "posted_at": self.posted_at.isoformat() if self.posted_at else None,
            "settled_at": self.settled_at.isoformat() if self.settled_at else None,
            "reversed_at": self.reversed_at.isoformat() if self.reversed_at else None,
        }

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<SCTxn id={self.id} user={self.user_id} wallet={self.wallet_id} "
            f"{self.type.value} {self.amount} {self.coin_symbol} status={self.status.value}>"
        )
