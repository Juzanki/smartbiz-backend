# backend/models/top_contributor.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import datetime as dt
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, TYPE_CHECKING, Dict, Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    UniqueConstraint,
    func,
    text,
    Numeric as SA_NUMERIC,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.db import Base
from backend.models._types import JSON_VARIANT, as_mutable_json

# -------- NUMERIC(18,2) portable: Postgres vs others --------
try:
    from sqlalchemy.dialects.postgresql import NUMERIC as PG_NUMERIC  # type: ignore
    DECIMAL_TYPE = PG_NUMERIC(18, 2)
except Exception:  # pragma: no cover
    DECIMAL_TYPE = SA_NUMERIC(18, 2)

if TYPE_CHECKING:
    from .live_stream import LiveStream
    from .user import User

_DEC2 = Decimal("0.01")


def _q2(v: Decimal | int | float | str) -> Decimal:
    """Quantize to 2dp with HALF_UP for money-like values."""
    return (v if isinstance(v, Decimal) else Decimal(str(v))).quantize(
        _DEC2, rounding=ROUND_HALF_UP
    )


class TopContributor(Base):
    """
    TopContributor — hifadhi ya wachangiaji wakubwa kwa stream fulani.

    - Decimal(18,2) kwa usahihi wa fedha/sarafu
    - Unique per (stream_id, user_id)
    - Metrics: contributions_count, last_contribution_at
    - currency (ISO3), ranking_snapshot, meta (JSON portable)
    """

    __tablename__ = "top_contributors"
    __mapper_args__ = {"eager_defaults": True}
    __table_args__ = (
        UniqueConstraint("stream_id", "user_id", name="uq_top_contributor_per_stream"),
        CheckConstraint("total_value >= 0", name="ck_top_contrib_nonneg"),
        CheckConstraint("contributions_count >= 0", name="ck_top_contrib_count_nonneg"),
        CheckConstraint("length(currency) = 3", name="ck_top_contrib_currency_iso3"),
        Index("ix_top_contrib_stream_value", "stream_id", "total_value"),
        Index("ix_top_contrib_stream_updated", "stream_id", "last_updated"),
        Index("ix_top_contrib_user", "user_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)

    # Scope
    stream_id: Mapped[int] = mapped_column(
        ForeignKey("live_streams.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Money/points
    total_value: Mapped[Decimal] = mapped_column(
        DECIMAL_TYPE, nullable=False, server_default=text("0")
    )
    currency: Mapped[str] = mapped_column(default="TZS", nullable=False)

    # Counters & snapshots
    contributions_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    ranking_snapshot: Mapped[Optional[int]] = mapped_column(Integer)
    meta: Mapped[Optional[Dict[str, Any]]] = mapped_column(as_mutable_json(JSON_VARIANT))

    # Timestamps
    last_contribution_at: Mapped[Optional[dt.datetime]] = mapped_column(
        DateTime(timezone=True), index=True
    )
    last_updated: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
        index=True,
    )

    # Relationships (zinategemea back_populates kuwepo kwenye LiveStream/User)
    stream: Mapped["LiveStream"] = relationship(
        "LiveStream",
        back_populates="top_contributors",
        lazy="selectin",
        passive_deletes=True,
    )
    user: Mapped["User"] = relationship(
        "User",
        back_populates="top_contributions",
        lazy="selectin",
        passive_deletes=True,
    )

    # ------------------------ Helpers ------------------------ #
    def add_contribution(
        self, amount: Decimal | float | int, *, currency: str | None = None
    ) -> None:
        """Ongeza mchango (amount > 0)."""
        amt = _q2(amount)
        if amt <= 0:
            raise ValueError("Contribution amount must be positive")
        if currency:
            c = (currency or "TZS").strip().upper()[:3]
            if len(c) != 3:
                raise ValueError("Currency must be ISO3")
            self.currency = c
        self.total_value = _q2((self.total_value or Decimal("0")) + amt)
        self.contributions_count = (self.contributions_count or 0) + 1
        self.last_contribution_at = dt.datetime.now(dt.timezone.utc)

    def set_total(self, amount: Decimal | float | int) -> None:
        """Weka thamani kamili (>= 0)."""
        amt = _q2(amount)
        if amt < 0:
            raise ValueError("Total value cannot be negative")
        self.total_value = amt
        self.last_contribution_at = dt.datetime.now(dt.timezone.utc)

    def set_currency(self, code: str) -> None:
        c = (code or "TZS").strip().upper()[:3]
        if len(c) != 3:
            raise ValueError("Currency must be ISO3")
        self.currency = c

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<TopContributor id={self.id} stream={self.stream_id} user={self.user_id} "
            f"total={self.total_value} {self.currency} count={self.contributions_count}>"
        )
