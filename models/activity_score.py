# backend/models/activity_score.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import datetime as dt
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, TYPE_CHECKING

from sqlalchemy import (
    Integer,
    DateTime,
    ForeignKey,
    CheckConstraint,
    Index,
    func,
    Numeric,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship, Session
from sqlalchemy.event import listens_for

from backend.db import Base

if TYPE_CHECKING:
    from .user import User

# 0.00 .. 100.00 (tunatumia DECIMAL(5,2))
_DEC2 = Numeric(5, 2)


def _to_pct(value: float | int | Decimal) -> Decimal:
    """Rudisha Decimal(2dp) iliyokatwa 0..100."""
    d = Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if d < 0:
        return Decimal("0.00")
    if d > Decimal("100.00"):
        return Decimal("100.00")
    return d


class ActivityScore(Base):
    """
    Huhifadhi alama za ushiriki wa mtumiaji (engagement) kwa analytics/gamification.

    - Typed mappings (SQLAlchemy 2.0)
    - score/response_rate zimekaba 0..100
    - Counters zisizo hasi
    - Timestamps TZ-aware + onupdate
    - QoL helpers kwa updates & upsert
    """
    __tablename__ = "activity_scores"
    __mapper_args__ = {"eager_defaults": True}

    # 1:1 na User — tumefanya user_id kuwa primary key
    user_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
        nullable=False,
        doc="Matches users.id (1:1).",
    )

    # Vipimo vya msingi
    score: Mapped[Decimal] = mapped_column(
        _DEC2,
        default=Decimal("0.00"),
        nullable=False,
        doc="Composite engagement score (0..100).",
    )
    messages_sent: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False, doc="Total messages sent (>=0)."
    )
    platforms_connected: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False, doc="Connected platforms count (>=0)."
    )
    response_rate: Mapped[Decimal] = mapped_column(
        _DEC2,
        default=Decimal("0.00"),
        nullable=False,
        doc="(responded / received) * 100, clamped to 0..100.",
    )

    # Muda
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    last_updated: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    # Uhusiano
    user: Mapped["User"] = relationship(
        "User",
        back_populates="activity_score",
        lazy="joined",
    )

    __table_args__ = (
        # Ulinzi wa data
        CheckConstraint("score >= 0 AND score <= 100", name="ck_activity_score_bounds"),
        CheckConstraint("response_rate >= 0 AND response_rate <= 100", name="ck_activity_rr_bounds"),
        CheckConstraint("messages_sent >= 0", name="ck_activity_msgs_nonneg"),
        CheckConstraint("platforms_connected >= 0", name="ck_activity_plat_nonneg"),
        # Faharasa zenye manufaa
        Index("ix_activity_score_score_desc", "score"),
        Index("ix_activity_score_updated", "last_updated"),
    )

    # ---------- Helpers (APIs za kirafiki) ----------

    def bump_messages(self, count: int = 1) -> None:
        """Ongeza `messages_sent` kwa usalama (haiwezi kuwa hasi)."""
        if count < 0:
            raise ValueError("count must be non-negative")
        self.messages_sent += count

    def set_platforms_connected(self, n: int) -> None:
        """Weka `platforms_connected` (>=0)."""
        if n < 0:
            raise ValueError("platforms_connected must be >= 0")
        self.platforms_connected = n

    def update_response_rate(self, responded: int, total_received: int) -> None:
        """Sasisha `response_rate` kama asilimia: responded/received * 100."""
        if responded < 0 or total_received < 0:
            raise ValueError("responded/total_received must be non-negative")
        if total_received == 0:
            self.response_rate = Decimal("0.00")
            return
        pct = (Decimal(responded) / Decimal(total_received)) * Decimal(100)
        self.response_rate = _to_pct(pct)

    def recompute_score(
        self,
        *,
        w_messages: Decimal | float = Decimal("0.40"),
        w_platforms: Decimal | float = Decimal("0.20"),
        w_response: Decimal | float = Decimal("0.40"),
        messages_cap: int = 1000,
        platforms_cap: int = 5,
    ) -> None:
        """
        Hesabu upya `score` kwa mizani rahisi:
        - messages_sent hufinyangwa kwa `messages_cap`
        - platforms_connected hufinyangwa kwa `platforms_cap`
        - response_rate tayari iko 0..100
        """
        wm = Decimal(str(w_messages))
        wp = Decimal(str(w_platforms))
        wr = Decimal(str(w_response))

        msg_pct = (Decimal(min(self.messages_sent, messages_cap)) / Decimal(messages_cap)) * Decimal(100)
        plat_pct = (Decimal(min(self.platforms_connected, platforms_cap)) / Decimal(platforms_cap)) * Decimal(100)

        composite = (wm * msg_pct) + (wp * plat_pct) + (wr * self.response_rate)
        self.score = _to_pct(composite)

    # ---------- QoL: upsert salama ----------

    @classmethod
    def get_or_create(cls, session: Session, user_id: int) -> "ActivityScore":
        """
        Pata au tengeneza ActivityScore ya user.
        - Inarejesha instance tayari attached kwenye `session`
        - Haina commit; acha caller aamue transaction
        """
        obj = session.get(cls, user_id)
        if obj is None:
            obj = cls(user_id=user_id)  # defaults zitatekelezwa
            session.add(obj)
        return obj

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<ActivityScore user_id={self.user_id} "
            f"score={self.score} msgs={self.messages_sent} "
            f"plats={self.platforms_connected} rr={self.response_rate}>"
        )


# ---------- Normalizers / Guards ----------

@listens_for(ActivityScore, "before_insert")
def _activity_score_before_insert(_m, _c, a: ActivityScore) -> None:
    # clamp percents
    a.score = _to_pct(a.score or Decimal("0.00"))
    a.response_rate = _to_pct(a.response_rate or Decimal("0.00"))
    # non-negative ints
    a.messages_sent = max(0, int(a.messages_sent or 0))
    a.platforms_connected = max(0, int(a.platforms_connected or 0))


@listens_for(ActivityScore, "before_update")
def _activity_score_before_update(_m, _c, a: ActivityScore) -> None:
    a.score = _to_pct(a.score or Decimal("0.00"))
    a.response_rate = _to_pct(a.response_rate or Decimal("0.00"))
    a.messages_sent = max(0, int(a.messages_sent or 0))
    a.platforms_connected = max(0, int(a.platforms_connected or 0))
