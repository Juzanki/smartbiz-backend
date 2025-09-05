# backend/models/analytics.py
# -*- coding: utf-8 -*-
"""
AnalyticsSnapshot
-----------------
• Snapshot ya kila siku kwa kila mtumiaji kwa ajili ya dashbodi/taarifa.
• Typed SQLAlchemy 2.0 (Mapped / mapped_column).
• TZ-aware timestamps.
• Data safety: non-negative constraints + asilimia 0..100.
• Utendaji: indexi muhimu na unique (user_id, snapshot_date).

⚠️ Hakikisha HAKUNA faili/klass nyingine yenye __tablename__ = "analytics_snapshots".
Ikiwa ulikuwa na `analytics_snapshot.py`, acha MOJA tu ibaki kutengeneza meza.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, TYPE_CHECKING

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.db import Base
from backend.models._types import JSON_VARIANT, DECIMAL_TYPE, as_mutable_json

if TYPE_CHECKING:
    from .user import User  # type-hints tu (hakuna import ya runtime)

# ---------- Decimal column types ----------
# 0.00 .. 100.00 (asilimia)
_PCT: Numeric = Numeric(5, 2)
# engagement score (weka range kubwa kwa mustakabali, 2dp)
_SCORE: Numeric = Numeric(12, 2)

class AnalyticsSnapshot(Base):
    __tablename__ = "analytics_snapshots"
    __mapper_args__ = {"eager_defaults": True}

    # ── Keys ───────────────────────────────────────────────────────────────
    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # ── When ───────────────────────────────────────────────────────────────
    # Tarehe ya snapshot (unique kwa kila user)
    snapshot_date: Mapped[dt.date] = mapped_column(
        Date,
        server_default=func.current_date(),
        nullable=False,
        index=True,
    )
    # Timestamp kamili (kwa audit / “last touch”)
    snapshot_time: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
        index=True,
    )

    # ── Metrics (non-negative) ─────────────────────────────────────────────
    messages_sent: Mapped[int] = mapped_column(
        Integer, server_default=text("0"), nullable=False
    )
    messages_received: Mapped[int] = mapped_column(
        Integer, server_default=text("0"), nullable=False
    )
    active_platforms: Mapped[int] = mapped_column(
        Integer, server_default=text("0"), nullable=False
    )

    # Asilimia 0..100 (2dp)
    response_rate: Mapped[Decimal] = mapped_column(
        _PCT, server_default=text("0.00"), nullable=False
    )

    # Engagement score (≥ 0, 2dp)
    engagement_score: Mapped[Decimal] = mapped_column(
        _SCORE, server_default=text("0.00"), nullable=False
    )

    # ── Relationship ───────────────────────────────────────────────────────
    user: Mapped["User"] = relationship(
        "User",
        back_populates="analytics_snapshots",  # ongeza upande wa User
        lazy="selectin",
        passive_deletes=True,
    )

    # ── Constraints & Indexes ─────────────────────────────────────────────
    __table_args__ = (
        # Unique snapshot per user per day
        UniqueConstraint("user_id", "snapshot_date", name="uq_analytics_user_date"),

        # Data safety
        CheckConstraint("messages_sent >= 0",     name="ck_analytics_msgs_sent_nonneg"),
        CheckConstraint("messages_received >= 0", name="ck_analytics_msgs_recv_nonneg"),
        CheckConstraint("active_platforms >= 0",  name="ck_analytics_active_plat_nonneg"),
        CheckConstraint("response_rate BETWEEN 0 AND 100", name="ck_analytics_rr_bounds"),
        CheckConstraint("engagement_score >= 0",  name="ck_analytics_score_nonneg"),

        # Query patterns
        Index("ix_analytics_user_date", "user_id", "snapshot_date"),
        Index("ix_analytics_user_time", "user_id", "snapshot_time"),
    )

    # ── Hybrids / convenience ─────────────────────────────────────────────
    @hybrid_property
    def total_messages(self) -> int:
        return int(self.messages_sent) + int(self.messages_received)

    @staticmethod
    def _pct(value: int | float | Decimal) -> Decimal:
        """Zungusha hadi 2dp na u-clamp 0..100."""
        d = Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        if d < 0:
            return Decimal("0.00")
        if d > 100:
            return Decimal("100.00")
        return d

    # ── Mutators (no DB I/O) ──────────────────────────────────────────────
    def bump_sent(self, n: int = 1) -> None:
        if n < 0:
            raise ValueError("n must be >= 0")
        self.messages_sent += n

    def bump_received(self, n: int = 1) -> None:
        if n < 0:
            raise ValueError("n must be >= 0")
        self.messages_received += n

    def bump(self, sent: int = 0, received: int = 0) -> None:
        if sent < 0 or received < 0:
            raise ValueError("sent/received must be >= 0")
        self.messages_sent += sent
        self.messages_received += received

    def set_active_platforms(self, n: int) -> None:
        if n < 0:
            raise ValueError("active_platforms must be >= 0")
        self.active_platforms = n

    def compute_response_rate(self) -> None:
        """response_rate = (messages_sent / messages_received) * 100 (zero-safe)."""
        if self.messages_received <= 0:
            self.response_rate = Decimal("0.00")
            return
        pct = (Decimal(self.messages_sent) / Decimal(self.messages_received)) * Decimal(100)
        self.response_rate = self._pct(pct)

    def set_score(self, value: int | float | Decimal) -> None:
        """Weka engagement_score (>= 0, 2dp)."""
        d = Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        self.engagement_score = d if d >= 0 else Decimal("0.00")

    def touch_today(self) -> None:
        """Sasisha tarehe na timestamp kuwa sasa (TZ-aware)."""
        self.snapshot_date = dt.date.today()
        self.snapshot_time = dt.datetime.now(dt.timezone.utc)

    # ── Debug ─────────────────────────────────────────────────────────────
    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<AnalyticsSnapshot id={self.id} user={self.user_id} "
            f"date={self.snapshot_date} sent={self.messages_sent} "
            f"recv={self.messages_received} rr={self.response_rate} "
            f"score={self.engagement_score}>"
        )



