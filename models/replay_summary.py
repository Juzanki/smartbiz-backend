# backend/models/replay_summary.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import enum
import datetime as dt
from typing import Optional, List, TYPE_CHECKING

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum as SQLEnum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text as sa_text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship, validates
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.event import listens_for

from backend.db import Base
from backend.models._types import JSON_VARIANT, as_mutable_json

# Hakuna try/except zisizo na mwili—JSON_VARIANT tayari inafanya kazi kwenye injini zote.

if TYPE_CHECKING:
    from .live_stream import LiveStream

def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)

class SummarySource(str, enum.Enum):
    ai          = "ai"
    human       = "human"
    import_file = "import_file"
    other       = "other"

class SummaryStyle(str, enum.Enum):
    abstractive = "abstractive"
    extractive  = "extractive"
    hybrid      = "hybrid"

class ReplaySummary(Base):
    """
    Muhtasari wa Replay wa LiveStream (1:1):
      - Maandishi ya muhtasari + (hiari) HTML
      - Vidokezo muhimu / mada / vyanzo
      - Metadata ya AI model & token usage
      - Idempotency + revision ndogo
    """
    __tablename__ = "replay_summaries"
    __mapper_args__ = {"eager_defaults": True}
    __table_args__ = (
        UniqueConstraint("live_stream_id", name="uq_replay_summary_stream"),
        UniqueConstraint("idempotency_key", name="uq_replay_summary_idem"),
        Index("ix_replay_summary_stream_created", "live_stream_id", "created_at"),
        Index("ix_replay_summary_lang", "lang"),
        Index("ix_replay_summary_tokens", "total_tokens"),
        CheckConstraint("length(summary_text) > 0", name="ck_replay_summary_text_not_empty"),
        CheckConstraint("length(lang) BETWEEN 2 AND 10", name="ck_replay_summary_lang_len"),
        CheckConstraint(
            "quality_score IS NULL OR (quality_score >= 0.0 AND quality_score <= 1.0)",
            name="ck_replay_summary_quality_range",
        ),
        CheckConstraint(
            "prompt_tokens >= 0 AND completion_tokens >= 0 AND total_tokens >= 0",
            name="ck_replay_summary_tokens_nonneg",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)

    # FK -> live_streams.id
    live_stream_id: Mapped[int] = mapped_column(
        ForeignKey("live_streams.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Maudhui
    title:        Mapped[Optional[str]] = mapped_column(String(160))
    summary_text: Mapped[str]           = mapped_column(Text, nullable=False)
    summary_html: Mapped[Optional[str]] = mapped_column(Text)  # (hiari) render-ready HTML

    # Lugha & mtindo
    lang:  Mapped[str] = mapped_column(String(10), default="en", nullable=False, index=True)
    style: Mapped[SummaryStyle] = mapped_column(
        SQLEnum(SummaryStyle, name="replay_summary_style", native_enum=False, validate_strings=True),
        default=SummaryStyle.abstractive,
        nullable=False,
        index=True,
    )

    # Chanzo (AI/human/import)
    source: Mapped[SummarySource] = mapped_column(
        SQLEnum(SummarySource, name="replay_summary_source", native_enum=False, validate_strings=True),
        default=SummarySource.ai,
        nullable=False,
        index=True,
    )

    # Vidokezo & mada (mutable JSON)
    key_points: Mapped[Optional[List[str]]]  = mapped_column(as_mutable_json(JSON_VARIANT))
    topics:     Mapped[Optional[List[str]]]  = mapped_column(as_mutable_json(JSON_VARIANT))
    sources:    Mapped[Optional[List[dict]]] = mapped_column(as_mutable_json(JSON_VARIANT))
    meta:       Mapped[Optional[dict]]       = mapped_column(as_mutable_json(JSON_VARIANT))

    # Ubora & sentiment
    quality_score: Mapped[Optional[float]] = mapped_column()            # 0..1
    sentiment:     Mapped[Optional[str]]   = mapped_column(String(16))  # "positive"/"neutral"/"negative"

    # AI model metadata / token usage (hiari)
    model_name:    Mapped[Optional[str]] = mapped_column(String(120))
    model_version: Mapped[Optional[str]] = mapped_column(String(60))
    prompt_id:     Mapped[Optional[str]] = mapped_column(String(120), index=True)

    prompt_tokens:     Mapped[int] = mapped_column(Integer, nullable=False, server_default=sa_text("0"))
    completion_tokens: Mapped[int] = mapped_column(Integer, nullable=False, server_default=sa_text("0"))
    total_tokens:      Mapped[int] = mapped_column(Integer, nullable=False, server_default=sa_text("0"))

    # Revision nyepesi (1:1)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, server_default=sa_text("1"))

    # Idempotency / tracing
    idempotency_key: Mapped[Optional[str]] = mapped_column(String(120), unique=True, index=True)
    external_ref:    Mapped[Optional[str]] = mapped_column(String(160), index=True)

    # Nyakati
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=sa_text("CURRENT_TIMESTAMP"),
        nullable=False, index=True,
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=sa_text("CURRENT_TIMESTAMP"),
        onupdate=sa_text("CURRENT_TIMESTAMP"), nullable=False,
    )

    # Relationship (1:1) na mzazi
    stream: Mapped["LiveStream"] = relationship(
        "LiveStream", back_populates="summary_ai", lazy="selectin", passive_deletes=True
    )

    # ---------- Hybrid properties ----------
    @hybrid_property
    def has_html(self) -> bool:
        return bool(self.summary_html and self.summary_html.strip())

    @hybrid_property
    def points_count(self) -> int:
        return len(self.key_points or [])

    @hybrid_property
    def reading_minutes(self) -> int:
        """Makadirio ya muda wa kusoma (~200 wpm)."""
        words = 0 if not self.summary_text else len([w for w in self.summary_text.split() if w.strip()])
        mins = (words + 199) // 200
        return max(1, mins)

    # ---------- Validators ----------
    @validates("title", "summary_text", "summary_html", "lang",
               "model_name", "model_version", "prompt_id", "sentiment",
               "idempotency_key", "external_ref")
    def _trim(self, _k: str, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        s = v.strip()
        if _k == "lang":
            s = (s or "en")[:10]
        return s or None

    @validates("quality_score")
    def _clamp_quality(self, _k: str, v: Optional[float]) -> Optional[float]:
        if v is None:
            return None
        return max(0.0, min(1.0, float(v)))

    @validates("prompt_tokens", "completion_tokens", "total_tokens", "revision")
    def _nonneg_int(self, _k: str, v: int) -> int:
        return max(0, int(v or 0))

    # ---------- Helpers ----------
    def set_language(self, lang: str) -> None:
        self.lang = (lang or "en")[:10]

    def set_quality(self, score: float | None) -> None:
        self.quality_score = None if score is None else max(0.0, min(1.0, float(score)))

    def set_tokens(self, *, prompt: int = 0, completion: int = 0) -> None:
        self.prompt_tokens = max(0, int(prompt))
        self.completion_tokens = max(0, int(completion))
        self.total_tokens = self.prompt_tokens + self.completion_tokens

    def append_points(self, *points: str) -> None:
        arr = list(self.key_points or [])
        arr.extend([p.strip() for p in points if p and p.strip()])
        # kata hadi entries 100 ili kuzuia kujaa kupita kiasi
        self.key_points = arr[:100]

    def add_sources(self, *sources: dict) -> None:
        arr = list(self.sources or [])
        arr.extend([s for s in sources if s])
        self.sources = arr[:100]

    def bump_revision(self) -> None:
        self.revision = int(self.revision or 1) + 1
        self.updated_at = _utcnow()

    def ensure_token_totals(self) -> None:
        """Hakikisha total_tokens = prompt + completion (kuhusu inserts/updates)."""
        pt = int(self.prompt_tokens or 0)
        ct = int(self.completion_tokens or 0)
        self.total_tokens = max(0, pt + ct)

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<ReplaySummary id={self.id} stream={self.live_stream_id} "
            f"lang={self.lang} source={self.source} rev={self.revision} "
            f"tokens={self.total_tokens}>"
        )

# --------- Event listeners: sanitize & derive ---------
@listens_for(ReplaySummary, "before_insert")
def _rs_before_insert(_mapper, _conn, target: ReplaySummary) -> None:  # pragma: no cover
    target.lang = (target.lang or "en")[:10]
    target.set_quality(target.quality_score)
    target.ensure_token_totals()

@listens_for(ReplaySummary, "before_update")
def _rs_before_update(_mapper, _conn, target: ReplaySummary) -> None:  # pragma: no cover
    target.lang = (target.lang or "en")[:10]
    target.set_quality(target.quality_score)
    target.ensure_token_totals()
