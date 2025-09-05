# backend/models/ad_earning.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import enum
import datetime as dt
from decimal import Decimal
from typing import Optional, Dict, Any, TYPE_CHECKING

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
    Numeric as SA_NUMERIC,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.event import listens_for
from sqlalchemy.ext.mutable import MutableDict

from backend.db import Base
from backend.models._types import JSON_VARIANT  # PG: JSONB, others: JSON

# ---- NUMERIC portability (coins 6dp, fiat 2dp) ----
try:
    from sqlalchemy.dialects.postgresql import NUMERIC as PG_NUMERIC  # type: ignore
    DEC6 = PG_NUMERIC(18, 6)
    DEC2 = PG_NUMERIC(18, 2)
except Exception:  # pragma: no cover
    DEC6 = SA_NUMERIC(18, 6)
    DEC2 = SA_NUMERIC(18, 2)

if TYPE_CHECKING:
    from .user import User


# ---------------- Enums ----------------
class AdType(str, enum.Enum):
    video = "video"
    banner = "banner"
    sponsor = "sponsor"
    other = "other"


class AdEarning(Base):
    """
    Per-user ad revenue accruals (SmartBiz Coins + optional fiat).
    Tracks ad type, campaign, placement, network, and attribution ids.
    """
    __tablename__ = "ad_earnings"
    __mapper_args__ = {"eager_defaults": True}

    # --- Keys ---
    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )

    # --- Classification ---
    ad_type: Mapped[AdType] = mapped_column(
        String(24),  # portable: we’ll validate via constraint + normalizers
        default=AdType.video.value,
        nullable=False,
        doc="video | banner | sponsor | other",
    )
    network: Mapped[Optional[str]] = mapped_column(String(32))
    campaign_id: Mapped[Optional[str]] = mapped_column(String(64))
    placement: Mapped[Optional[str]] = mapped_column(String(32))  # e.g., feed, pre-roll

    # --- Attribution & de-duplication ---
    impression_id: Mapped[Optional[str]] = mapped_column(String(64), index=True)
    click_id: Mapped[Optional[str]] = mapped_column(String(64), index=True)
    idempotency_key: Mapped[Optional[str]] = mapped_column(String(64), index=True, unique=True)

    # --- Earnings ---
    smartcoins_earned: Mapped[Decimal] = mapped_column(
        DEC6, default=Decimal("0"), nullable=False, doc="Coins with 6dp precision."
    )
    fiat_amount: Mapped[Optional[Decimal]] = mapped_column(DEC2)
    fiat_currency: Mapped[Optional[str]] = mapped_column(String(8))

    # --- Extra context ---
    details: Mapped[Optional[str]] = mapped_column(Text)
    # keep DB column name `metadata`, attribute is `meta`
    meta: Mapped[Optional[Dict[str, Any]]] = mapped_column(
        "metadata",
        MutableDict.as_mutable(JSON_VARIANT),
    )

    # --- Timestamps ---
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    # --- Relationships ---
    user: Mapped["User"] = relationship(
        "User", back_populates="ad_earnings", lazy="selectin", passive_deletes=True
    )

    # --- Constraints & Indexes ---
    __table_args__ = (
        CheckConstraint("smartcoins_earned >= 0", name="ck_ad_earn_nonneg_coins"),
        CheckConstraint("fiat_amount IS NULL OR fiat_amount >= 0", name="ck_ad_earn_nonneg_fiat"),
        CheckConstraint(
            "ad_type IN ('video','banner','sponsor','other')",
            name="ck_ad_earn_type_allowed",
        ),
        CheckConstraint(
            "fiat_currency IS NULL OR length(trim(fiat_currency)) BETWEEN 2 AND 8",
            name="ck_ad_earn_currency_len",
        ),
        Index("ix_ad_earn_user_type_time", "user_id", "ad_type", "created_at"),
        Index("ix_ad_earn_campaign_time", "campaign_id", "created_at"),
        Index("ix_ad_earn_network_time", "network", "created_at"),
        Index("ix_ad_earn_ids", "impression_id", "click_id"),
    )

    # ---------------- Helpers / API ----------------
    def set_idempotency(self, key: Optional[str]) -> None:
        """Attach an idempotency key (truncated to 64 chars)."""
        self.idempotency_key = key[:64] if key else None

    def merge_meta(self, **kv: Any) -> None:
        """Shirikisha/ongeza key/value kwenye meta JSON kwa usalama."""
        cur = self.meta or {}
        cur.update(kv)
        self.meta = cur

    @property
    def has_fiat(self) -> bool:
        return self.fiat_amount is not None and (self.fiat_amount or Decimal("0")) > 0

    def award(
        self,
        coins: Decimal | int | float,
        *,
        fiat: Decimal | int | float | None = None,
        currency: str | None = None,
    ) -> None:
        """Ongeza mapato salama; hiari: ongeza fiat na currency."""
        c = coins if isinstance(coins, Decimal) else Decimal(str(coins))
        if c < 0:
            raise ValueError("coins must be >= 0")
        self.smartcoins_earned = (self.smartcoins_earned or Decimal("0")) + c

        if fiat is not None:
            f = fiat if isinstance(fiat, Decimal) else Decimal(str(fiat))
            if f < 0:
                raise ValueError("fiat must be >= 0")
            self.fiat_amount = (self.fiat_amount or Decimal("0")) + f

        if currency:
            self.fiat_currency = currency.strip().upper()[:8]

    def set_fiat(self, amount: Decimal | int | float | None, currency: str | None) -> None:
        """Andika upya fiat amount/currency (normalized)."""
        self.fiat_amount = None if amount is None else Decimal(str(amount))
        self.fiat_currency = None if not currency else currency.strip().upper()[:8]

    @staticmethod
    def dedup_key(
        *,
        impression_id: Optional[str] = None,
        click_id: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> Optional[str]:
        """
        Tumia hii kujenga ufunguo wa kudeduplicate katika layer ya service kabla ya ku-insert.
        Kipaumbele: explicit idempotency_key > impression_id > click_id.
        """
        for v in (idempotency_key, impression_id, click_id):
            if v:
                return v[:64]
        return None

    def __repr__(self) -> str:  # pragma: no cover
        cur = f" {self.fiat_currency}" if self.fiat_currency else ""
        return (
            f"<AdEarning id={self.id} user={self.user_id} type={self.ad_type} "
            f"coins={self.smartcoins_earned} fiat={self.fiat_amount}{cur}>"
        )


# ---------------- Normalizers / Guards ----------------
@listens_for(AdEarning, "before_insert")
def _ad_earning_before_insert(_mapper, _conn, t: AdEarning) -> None:
    # normalize ad_type & currency
    if t.ad_type:
        at = str(t.ad_type).strip().lower()
        if at not in {e.value for e in AdType}:
            at = AdType.other.value
        t.ad_type = at
    if t.fiat_currency:
        t.fiat_currency = t.fiat_currency.strip().upper()[:8]

    # clamp negatives
    if t.smartcoins_earned is not None and t.smartcoins_earned < 0:
        t.smartcoins_earned = Decimal("0")
    if t.fiat_amount is not None and t.fiat_amount < 0:
        t.fiat_amount = Decimal("0")

    # trim ids
    if t.impression_id:
        t.impression_id = t.impression_id.strip()[:64]
    if t.click_id:
        t.click_id = t.click_id.strip()[:64]
    if t.idempotency_key:
        t.idempotency_key = t.idempotency_key.strip()[:64]


@listens_for(AdEarning, "before_update")
def _ad_earning_before_update(_mapper, _conn, t: AdEarning) -> None:
    if t.ad_type:
        at = str(t.ad_type).strip().lower()
        if at not in {e.value for e in AdType}:
            at = AdType.other.value
        t.ad_type = at
    if t.fiat_currency:
        t.fiat_currency = t.fiat_currency.strip().upper()[:8]
    if t.smartcoins_earned is not None and t.smartcoins_earned < 0:
        t.smartcoins_earned = Decimal("0")
    if t.fiat_amount is not None and t.fiat_amount < 0:
        t.fiat_amount = Decimal("0")
    if t.impression_id:
        t.impression_id = t.impression_id.strip()[:64]
    if t.click_id:
        t.click_id = t.click_id.strip()[:64]
    if t.idempotency_key:
        t.idempotency_key = t.idempotency_key.strip()[:64]
