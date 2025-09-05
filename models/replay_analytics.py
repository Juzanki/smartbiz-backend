# backend/models/replay_analytics.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import enum
import datetime as dt
from typing import Optional, TYPE_CHECKING

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum as SQLEnum,
    ForeignKey,
    Index,
    Integer,
    String,
    Float,
    UniqueConstraint,
    text,
    func,
    JSON as SA_JSON,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship, validates
from sqlalchemy.ext.hybrid import hybrid_property

from backend.db import Base
from backend.models._types import JSON_VARIANT, as_mutable_json

# ---- Portable JSON: JSON_VARIANT on Postgres, JSON elsewhere ----
try:
    pass
except Exception:  # pragma: no cover
    # patched: use shared JSON_VARIANT
    pass

if TYPE_CHECKING:
    from .live_stream import LiveStream


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class Granularity(str, enum.Enum):
    """Kipimo cha muda kwa kubaketi takwimu."""
    event  = "event"   # bila bucket (raw event rows)
    minute = "minute"
    hour   = "hour"
    day    = "day"


class ReplayAnalytics(Base):
    """
    Fine-grained analytics kwa LiveStream (replay):
      - metric (views, likes, watch_time_seconds, n.k.)
      - dims za hiari (platform, country)
      - time buckets (granularity + period_start)
      - idempotency key kwa dedupe/upsert
    """
    __tablename__ = "replay_analytics"
    __mapper_args__ = {"eager_defaults": True}
    __table_args__ = (
        # uduplikishaji wa bucket moja (per-dimension)
        UniqueConstraint(
            "live_stream_id", "metric", "granularity", "period_start", "platform", "country",
            name="uq_replay_bucket_unique",
        ),
        # hot query paths
        Index("ix_replay_stream_metric_created", "live_stream_id", "metric", "created_at"),
        Index("ix_replay_stream_bucket", "live_stream_id", "granularity", "period_start"),
        Index("ix_replay_metric_platform", "metric", "platform"),
        Index("ix_replay_country", "country"),
        Index("ix_replay_idem", "idempotency_key"),
        # walinzi
        CheckConstraint("length(metric) > 0", name="ck_replay_metric_not_empty"),
        CheckConstraint("value >= 0.0", name="ck_replay_value_non_negative"),
        CheckConstraint("samples >= 0", name="ck_replay_samples_non_negative"),
        CheckConstraint(
            "(granularity = 'event') OR (period_start IS NOT NULL)",
            name="ck_replay_bucket_requires_period",
        ),
        CheckConstraint("country IS NULL OR length(country) = 2", name="ck_replay_country_iso2"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)

    # FK -> live_streams.id
    live_stream_id: Mapped[int] = mapped_column(
        ForeignKey("live_streams.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )

    # Metric (mf. "views", "likes", "comments", "watch_time_seconds", ...)
    metric: Mapped[str] = mapped_column(String(48), nullable=False, index=True)

    # Thamani kuu (counts/durations)
    value: Mapped[float] = mapped_column(
        Float,
        nullable=False,
        server_default=text("0"),
        doc="Jumla ya thamani kwenye bucket (mf. sekunde, idadi ya views)",
    )

    # Sampuli (msaada kwa averages; hiari)
    samples: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default=text("0"),
        doc="Idadi ya matukio yaliyochangia 'value' (kwa wastani/ratios).",
    )

    # Time bucketing (portable enums)
    granularity: Mapped[Granularity] = mapped_column(
        SQLEnum(Granularity, name="replay_granularity", native_enum=False, validate_strings=True),
        default=Granularity.event,
        nullable=False,
        index=True,
    )
    period_start: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True), index=True)

    # Dimensions (hiari)
    platform: Mapped[Optional[str]] = mapped_column(String(32), index=True)   # whatsapp, facebook, web, ...
    country:  Mapped[Optional[str]] = mapped_column(String(2), index=True)    # ISO-3166 alpha-2

    # Idempotency / tracing
    idempotency_key: Mapped[Optional[str]] = mapped_column(String(120), unique=True)
    request_id:      Mapped[Optional[str]] = mapped_column(String(64))

    # Free-form metadata (mutable kwa in-place updates)
    meta: Mapped[Optional[dict]] = mapped_column(as_mutable_json(JSON_VARIANT))

    # Timestamps
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    expires_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True), index=True)

    # Relationship
    live_stream: Mapped["LiveStream"] = relationship(
        "LiveStream",
        back_populates="replay_analytics",
        lazy="selectin",
        passive_deletes=True,
    )

    # ---------- Hybrids ----------
    @hybrid_property
    def avg(self) -> float:
        """Wastani rahisi: value / max(samples,1)."""
        s = self.samples or 0
        return float(self.value or 0.0) / float(s if s > 0 else 1)

    @hybrid_property
    def is_bucketed(self) -> bool:
        return self.granularity != Granularity.event

    @hybrid_property
    def is_expired(self) -> bool:
        return bool(self.expires_at and _utcnow() >= self.expires_at)

    # ---------- Validators ----------
    @validates("metric", "platform", "idempotency_key", "request_id")
    def _trim(self, _k: str, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        s = v.strip()
        return s or None

    @validates("country")
    def _country(self, _k: str, v: Optional[str]) -> Optional[str]:
        if not v:
            return None
        v2 = v.strip().upper()
        return v2[:2] if len(v2) >= 2 else None

    # ---------- Helpers ----------
    def inc(self, by: float = 1.0) -> None:
        """Ongeza 1 (au by) na usasishe samples."""
        if by <= 0:
            return
        self.value = float(self.value or 0.0) + float(by)
        self.samples = int(self.samples or 0) + 1

    def add(self, amount: float) -> None:
        """Ongeza kiasi maalum kwenye value na ongeza sample 1."""
        if amount <= 0:
            return
        self.value = float(self.value or 0.0) + float(amount)
        self.samples = int(self.samples or 0) + 1

    def merge_from(self, other: "ReplayAnalytics") -> None:
        """Unganisha bucket nyingine ya metric/dimensions sawa (value+samples)."""
        self.value = float(self.value or 0.0) + float(other.value or 0.0)
        self.samples = int(self.samples or 0) + int(other.samples or 0)

    def set_value(self, value: float, *, samples: int | None = None) -> None:
        self.value = max(0.0, float(value))
        if samples is not None:
            self.samples = max(0, int(samples))

    def set_bucket(self, granularity: Granularity, at: Optional[dt.datetime] = None) -> None:
        """Weka ukubwa wa bucket na kulalign timestamp kwenye dakika/saa/siku."""
        self.granularity = granularity
        if granularity == Granularity.event:
            self.period_start = None
            return

        when = at or _utcnow()
        if when.tzinfo is None:
            when = when.replace(tzinfo=dt.timezone.utc)

        if granularity == Granularity.day:
            floored = when.replace(hour=0, minute=0, second=0, microsecond=0)
        elif granularity == Granularity.hour:
            floored = when.replace(minute=0, second=0, microsecond=0)
        elif granularity == Granularity.minute:
            floored = when.replace(second=0, microsecond=0)
        else:
            floored = when

        self.period_start = floored

    def touch_idempotency(self, key: str) -> None:
        self.idempotency_key = (key or "").strip() or None

    def expire_in(self, *, seconds: int = 3600) -> None:
        self.expires_at = _utcnow() + dt.timedelta(seconds=max(1, seconds))

    def upsert_key(self) -> tuple:
        """Key inayotambulisha bucket ya upsert."""
        return (
            self.live_stream_id,
            (self.metric or "").strip(),
            self.granularity.value if isinstance(self.granularity, Granularity) else self.granularity,
            self.period_start,
            (self.platform or None),
            (self.country or None),
        )

    def __repr__(self) -> str:  # pragma: no cover
        g = self.granularity
        p = self.period_start.isoformat() if self.period_start else "—"
        return f"<ReplayAnalytics id={self.id} stream={self.live_stream_id} metric={self.metric} value={self.value} g={g} t={p}>"
