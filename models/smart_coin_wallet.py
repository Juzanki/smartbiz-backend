# backend/models/smart_coin_wallet.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import enum
import datetime as dt
from decimal import Decimal, ROUND_DOWN
from typing import Optional, TYPE_CHECKING, List

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.db import Base
from backend.models._types import JSON_VARIANT, as_mutable_json

if TYPE_CHECKING:
    from .user import User
    from .smart_coin_transaction import SmartCoinTransaction


# ---------------- Enums ----------------
class WalletStatus(str, enum.Enum):
    active  = "active"
    frozen  = "frozen"
    closed  = "closed"


class WalletTheme(str, enum.Enum):
    system = "system"
    light  = "light"
    dark   = "dark"


# --------------- Helpers ---------------
def _pow10(n: int) -> Decimal:
    return Decimal(10) ** int(max(0, n))


# ---------------- Model ----------------
class SmartCoinWallet(Base):
    """
    SmartCoin Wallet (1:1 na User, mobile-first).

    Maboresho makuu:
    - **Scale-aware atomic units**: 'balance_atomic' + 'scale' (chaguo-msingi 6dp kwa sarafu za kidijiti).
      *NB: tulihifadhi ulinganifu kwa waliokuwa na "cents": weka scale=2.*
    - **Holds/Reservations** kwa malipo yanayosubiri -> `holds_atomic`, `available_atomic` na helpers.
    - **Ulinzi wa concurrency**: `lock_version` (optimistic locking).
    - **Ufuatiliaji**: `last_txn_id/last_txn_at` + `meta` (JSON), `status/theme`, `coin_symbol`.
    - **API ya urahisi**: credit/debit/transfer (atomic & Decimal), apply_transaction(delta), freeze/unfreeze/close.
    """

    __tablename__ = "smart_coin_wallets"
    __mapper_args__ = {"eager_defaults": True}

    # Identity
    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)

    # 1:1 na User
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
        doc="Wallet ya mtumiaji huyu",
    )

    # Sifa za wallet
    coin_symbol: Mapped[str] = mapped_column(String(16), default="SMART", nullable=False, index=True)
    status: Mapped[WalletStatus] = mapped_column(
        String(16), default=WalletStatus.active.value, nullable=False, index=True
    )
    theme: Mapped[WalletTheme] = mapped_column(
        String(16), default=WalletTheme.system.value, nullable=False
    )

    # Scale-aware atomic units (badala ya cents tu). scale=6 kwa sarafu za kidijiti; 2 kwa fiat.
    scale: Mapped[int] = mapped_column(Integer, default=6, nullable=False)

    # Mizani
    balance_atomic: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    holds_atomic:   Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # Ufuatiliaji/metadata
    last_txn_id:  Mapped[Optional[int]] = mapped_column(Integer, index=True)
    last_txn_at:  Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    meta:         Mapped[Optional[dict]] = mapped_column(as_mutable_json(JSON_VARIANT))

    # Timestamps
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    # Optimistic locking
    lock_version: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # ---------------- Relationships ----------------
    user: Mapped["User"] = relationship(
        "User",
        back_populates="wallet",
        lazy="selectin",
        uselist=False,
    )

    # Lazima ilingane na SmartCoinTransaction.wallet (back_populates="wallet")
    transactions: Mapped[List["SmartCoinTransaction"]] = relationship(
        "SmartCoinTransaction",
        back_populates="wallet",
        foreign_keys="SmartCoinTransaction.wallet_id",
        cascade="all, delete-orphan",
        passive_deletes=True,
        lazy="selectin",
    )

    # ---------------- Constraints & Indexes ----------------
    __table_args__ = (
        UniqueConstraint("user_id", name="uq_wallet_user"),
        CheckConstraint("scale >= 0 AND scale <= 12", name="ck_wallet_scale_bounds"),
        CheckConstraint("length(coin_symbol) BETWEEN 1 AND 16", name="ck_wallet_coin_len"),
        CheckConstraint("balance_atomic >= 0", name="ck_wallet_balance_nonneg"),
        CheckConstraint("holds_atomic >= 0", name="ck_wallet_holds_nonneg"),
        CheckConstraint("balance_atomic >= holds_atomic", name="ck_wallet_available_nonneg"),
        Index("ix_wallet_status", "status"),
        Index("ix_wallet_coin", "coin_symbol"),
    )

    # --------------- Conversions ---------------
    @property
    def unit(self) -> Decimal:
        """Kiwango cha mgawanyo (10^scale)."""
        return _pow10(self.scale)

    def to_amount(self, atomic: int | Decimal) -> Decimal:
        """Badili atomic units -> Decimal amount (heshimu scale)."""
        return (Decimal(int(atomic)) / self.unit).quantize(Decimal(1) / self.unit, rounding=ROUND_DOWN)

    def to_atomic(self, amount: Decimal | float | int | str) -> int:
        """Badili Decimal amount -> atomic int (heshimu scale)."""
        q = (Decimal(str(amount)) * self.unit).to_integral_value(rounding=ROUND_DOWN)
        if q < 0:
            raise ValueError("amount must be >= 0")
        return int(q)

    # --------------- Balances ---------------
    @property
    def balance(self) -> Decimal:
        """Salio (Decimal) likiheshimu scale."""
        return self.to_amount(self.balance_atomic)

    @property
    def holds(self) -> Decimal:
        return self.to_amount(self.holds_atomic)

    @property
    def available_atomic(self) -> int:
        return max(0, int(self.balance_atomic) - int(self.holds_atomic))

    @property
    def available(self) -> Decimal:
        return self.to_amount(self.available_atomic)

    def _touch(self) -> None:
        self.updated_at = dt.datetime.now(dt.timezone.utc)
        self.lock_version += 1

    # --------------- State controls ---------------
    def freeze(self) -> None:
        self.status = WalletStatus.frozen.value
        self._touch()

    def unfreeze(self) -> None:
        self.status = WalletStatus.active.value
        self._touch()

    def close(self) -> None:
        self.status = WalletStatus.closed.value
        self._touch()

    # --------------- Holds / Reservations ---------------
    def place_hold(self, amount: Decimal | float | int | str) -> int:
        """Weka hold (reservation) – inarudisha kiasi cha atomic kilichowekwa hold."""
        if self.status != WalletStatus.active.value:
            raise ValueError("wallet not active")
        atoms = self.to_atomic(amount)
        if atoms <= 0:
            return 0
        if atoms > self.available_atomic:
            raise ValueError("insufficient available balance")
        self.holds_atomic += atoms
        self._touch()
        return atoms

    def release_hold(self, atomic: int) -> None:
        """Ondoa hold sehemu/nyote (atomic)."""
        if atomic < 0:
            raise ValueError("atomic must be >= 0")
        self.holds_atomic = max(0, int(self.holds_atomic) - int(atomic))
        self._touch()

    # --------------- Mutations (atomic) ---------------
    def credit_atomic(self, atomic: int, *, reason: Optional[str] = None) -> None:
        if atomic < 0:
            raise ValueError("atomic must be >= 0")
        if self.status == WalletStatus.closed.value:
            raise ValueError("wallet closed")
        self.balance_atomic += int(atomic)
        self._touch()

    def debit_atomic(self, atomic: int, *, use_hold: bool = False) -> None:
        if atomic < 0:
            raise ValueError("atomic must be >= 0")
        if self.status != WalletStatus.active.value:
            raise ValueError("wallet not active")
        # kama tunatumia hold, punguzo hutoka kwenye available (na hushusha hold kwanza)
        if use_hold:
            if atomic > self.holds_atomic:
                raise ValueError("insufficient held amount")
            self.holds_atomic -= int(atomic)
        if atomic > self.balance_atomic:
            raise ValueError("insufficient funds")
        self.balance_atomic -= int(atomic)
        self._touch()

    # --------------- Mutations (Decimal) ---------------
    def credit(self, amount: Decimal | float | int | str) -> int:
        atoms = self.to_atomic(amount)
        self.credit_atomic(atoms)
        return atoms

    def debit(self, amount: Decimal | float | int | str, *, use_hold: bool = False) -> int:
        atoms = self.to_atomic(amount)
        self.debit_atomic(atoms, use_hold=use_hold)
        return atoms

    # --------------- Transfers ---------------
    def transfer_to(self, other: "SmartCoinWallet", amount: Decimal | float | int | str) -> int:
        """Hamisha kiasi (Decimal) kutoka wallet hii kwenda nyingine (coin lazima lilingane)."""
        if self.id == other.id:
            raise ValueError("cannot transfer to the same wallet")
        if self.coin_symbol != other.coin_symbol or self.scale != other.scale:
            raise ValueError("coin/scale mismatch between wallets")
        atoms = self.debit(amount)
        other.credit_atomic(atoms)
        return atoms

    # --------------- Txn integration ---------------
    def apply_transaction(
        self,
        txn: "SmartCoinTransaction",
        *,
        post: bool = True,
    ) -> None:
        """
        Tumia athari ya muamala kwenye mizani ya wallet.
        - Inatarajia `txn.delta` (Decimal signed) kutoka kwenye model yako ya SmartCoinTransaction.
        - Ikiwa `post=True`, itaweka snapshot (`txn.stamp_posted`), `last_txn_*`.
        """
        # Ulinzi wa aina ya sarafu/scale
        if getattr(txn, "coin_symbol", self.coin_symbol) != self.coin_symbol:
            raise ValueError("coin mismatch")
        # Badilisha delta kwenda atomic
        delta_dec: Decimal = getattr(txn, "delta")  # signed Decimal
        delta_atoms = int((delta_dec * self.unit).to_integral_value(rounding=ROUND_DOWN))
        if delta_atoms == 0:
            # still stamp snapshots for audit
            if post and hasattr(txn, "stamp_posted"):
                txn.stamp_posted(balance_before=self.balance, balance_after=self.balance)
            return
        if delta_atoms > 0:
            self.credit_atomic(delta_atoms)
        else:
            self.debit_atomic(-delta_atoms)

        if post and hasattr(txn, "stamp_posted"):
            txn.stamp_posted(balance_before=self.balance - self.to_amount(delta_atoms), balance_after=self.balance)
            self.last_txn_id = txn.id
            self.last_txn_at = dt.datetime.now(dt.timezone.utc)

    # --------------- Debug / public ---------------
    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<SmartCoinWallet id={self.id} user={self.user_id} "
            f"coin={self.coin_symbol} scale={self.scale} "
            f"bal_atomic={self.balance_atomic} holds={self.holds_atomic} "
            f"status={self.status}>"
        )


# Alias optional (compat)
Wallet = SmartCoinWallet
