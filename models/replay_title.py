# backend/models/replay_title.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import enum
import re
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
    UniqueConstraint,
    text as sa_text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship, validates
from sqlalchemy.event import listens_for

from backend.db import Base
from backend.models._types import JSON_VARIANT, as_mutable_json

# Hakuna try/except tupu hapa—JSON_VARIANT tayari inapatikana kwenye engines zote.

if TYPE_CHECKING:
    from .live_stream import LiveStream

def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)

class TitleSource(str, enum.Enum):
    ai          = "ai"
    human       = "human"
    import_file = "import_file"
    other       = "other"

class TitleStyle(str, enum.Enum):
    descriptive = "descriptive"
    concise     = "concise"
    question    = "question"
    exciting    = "exciting"
    listicle    = "listicle"
    breaking    = "breaking"
    other       = "other"

class ReplayTitle(Base):
    """
    Kichwa cha Replay ya LiveStream (1:1):
      - generated_title + slug
      - style/source/lang + ubora
      - AI metadata (model, tokens)
      - keywords/alt_titles/meta (mutable JSON)
      - idempotency/external ref
    """
    __tablename__ = "replay_titles"
    __mapper_args__ = {"eager_defaults": True}
    __table_args__ = (
        UniqueConstraint("live_stream_id", name="uq_replay_title_stream"),   # 1:1 na stream
        UniqueConstraint("idempotency_key", name="uq_replay_title_idem"),
        UniqueConstraint("live_stream_id", "slug", name="uq_replay_title_stream_slug"),
        Index("ix_replay_title_stream_created", "live_stream_id", "created_at"),
        Index("ix_replay_title_lang", "lang"),
        Index("ix_replay_title_source", "source"),
        Index("ix_replay_title_slug", "slug"),
        Index("ix_replay_title_tokens", "total_tokens"),
        CheckConstraint("length(generated_title) BETWEEN 4 AND 160", name="ck_replay_title_len"),
        CheckConstraint("length(lang) BETWEEN 2 AND 10", name="ck_replay_title_lang_len"),
        CheckConstraint(
            "quality_score IS NULL OR (quality_score >= 0.0 AND quality_score <= 1.0)",
            name="ck_replay_title_quality_range",
        ),
        CheckConstraint(
            "prompt_tokens >= 0 AND completion_tokens >= 0 AND total_tokens >= 0",
            name="ck_replay_title_tokens_nonneg",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)

    # FK -> live_streams.id
    live_stream_id: Mapped[int] = mapped_column(
        ForeignKey("live_streams.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    stream: Mapped["LiveStream"] = relationship(
        "LiveStream", back_populates="auto_title", lazy="selectin", passive_deletes=True
    )

    # Maudhui ya kichwa
    generated_title: Mapped[str] = mapped_column(String(160), nullable=False)
    slug: Mapped[Optional[str]] = mapped_column(String(180), index=True)

    # Sifa
    lang: Mapped[str] = mapped_column(String(10), default="en", nullable=False, index=True)
    source: Mapped[TitleSource] = mapped_column(
        SQLEnum(TitleSource, name="replay_title_source", native_enum=False, validate_strings=True),
        default=TitleSource.ai,
        nullable=False,
        index=True,
    )
    style: Mapped[TitleStyle] = mapped_column(
        SQLEnum(TitleStyle, name="replay_title_style", native_enum=False, validate_strings=True),
        default=TitleStyle.descriptive,
        nullable=False,
        index=True,
    )
    quality_score: Mapped[Optional[float]] = mapped_column()  # inferred Float

    # Neno kuu/alternatives/meta (mutable JSON ili ORM itambue mabadiliko ya ndani)
    keywords:   Mapped[Optional[List[str]]]  = mapped_column(as_mutable_json(JSON_VARIANT))
    alt_titles: Mapped[Optional[List[str]]]  = mapped_column(as_mutable_json(JSON_VARIANT))
    meta:       Mapped[Optional[dict]]       = mapped_column(as_mutable_json(JSON_VARIANT))

    # AI model metadata / token usage (hiari)
    model_name:    Mapped[Optional[str]] = mapped_column(String(120))
    model_version: Mapped[Optional[str]] = mapped_column(String(60))
    prompt_id:     Mapped[Optional[str]] = mapped_column(String(120), index=True)

    prompt_tokens:     Mapped[int] = mapped_column(Integer, nullable=False, server_default=sa_text("0"))
    completion_tokens: Mapped[int] = mapped_column(Integer, nullable=False, server_default=sa_text("0"))
    total_tokens:      Mapped[int] = mapped_column(Integer, nullable=False, server_default=sa_text("0"))

    # Idempotency / tracing
    idempotency_key: Mapped[Optional[str]] = mapped_column(String(120), unique=True, index=True)
    external_ref:    Mapped[Optional[str]] = mapped_column(String(160), index=True)

    # Nyakati
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=sa_text("CURRENT_TIMESTAMP"),
        nullable=False, index=True
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=sa_text("CURRENT_TIMESTAMP"),
        onupdate=sa_text("CURRENT_TIMESTAMP"), nullable=False
    )

    # ---------- Helpers ----------
    _slug_re = re.compile(r"[^a-z0-9]+", re.IGNORECASE)

    def set_title(self, title: str, *, auto_slug: bool = True, max_len: int = 160) -> None:
        t = (title or "").strip()
        if max_len and len(t) > max_len:
            t = t[:max_len].rstrip()
        self.generated_title = t
        if auto_slug:
            self.slug = self.make_slug(t)

    def make_slug(self, text: Optional[str] = None, *, max_len: int = 120) -> str:
        src = (text or self.generated_title or "").lower().strip()
        slug = self._slug_re.sub("-", src).strip("-")
        if max_len and len(slug) > max_len:
            slug = slug[:max_len].rstrip("-")
        return slug or "untitled"

    def ensure_slug(self) -> None:
        if not (self.slug and self.slug.strip()):
            self.slug = self.make_slug()

    def set_quality(self, score: Optional[float]) -> None:
        self.quality_score = None if score is None else max(0.0, min(1.0, float(score)))

    def set_tokens(self, *, prompt: int = 0, completion: int = 0) -> None:
        self.prompt_tokens = max(0, int(prompt))
        self.completion_tokens = max(0, int(completion))
        self.total_tokens = self.prompt_tokens + self.completion_tokens

    def ensure_max_length(self, max_len: int = 100) -> None:
        if self.generated_title and len(self.generated_title) > max_len:
            self.generated_title = self.generated_title[:max_len].rstrip()

    def append_keywords(self, *words: str, cap: int = 50) -> None:
        arr = list(self.keywords or [])
        arr.extend([w.strip() for w in words if w and w.strip()])
        self.keywords = arr[:cap]

    def append_alt_titles(self, *titles: str, cap: int = 20) -> None:
        arr = list(self.alt_titles or [])
        arr.extend([t.strip() for t in titles if t and t.strip()])
        self.alt_titles = arr[:cap]

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<ReplayTitle id={self.id} stream={self.live_stream_id} lang={self.lang} "
            f"source={self.source} title={self.generated_title!r}>"
        )

    # ---------- Validators ----------
    @validates("generated_title", "slug", "lang", "model_name", "model_version",
               "prompt_id", "idempotency_key", "external_ref")
    def _trim(self, _k: str, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        s = v.strip()
        if _k == "lang":
            s = (s or "en")[:10]
        return s or None

    @validates("prompt_tokens", "completion_tokens", "total_tokens")
    def _nonneg(self, _k: str, v: int) -> int:
        return max(0, int(v or 0))

# --------- Event listeners: sanitize & derive ---------
@listens_for(ReplayTitle, "before_insert")
def _rt_before_insert(_mapper, _conn, target: ReplayTitle) -> None:  # pragma: no cover
    target.lang = (target.lang or "en")[:10]
    target.ensure_slug()
    target.set_quality(target.quality_score)
    # hakikisha total_tokens = prompt + completion
    target.total_tokens = max(0, int(target.prompt_tokens or 0) + int(target.completion_tokens or 0))

@listens_for(ReplayTitle, "before_update")
def _rt_before_update(_mapper, _conn, target: ReplayTitle) -> None:  # pragma: no cover
    target.lang = (target.lang or "en")[:10]
    if not target.slug:
        target.ensure_slug()
    target.set_quality(target.quality_score)
    target.total_tokens = max(0, int(target.prompt_tokens or 0) + int(target.completion_tokens or 0))
