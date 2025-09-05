# backend/models/replay_caption.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import enum
from datetime import datetime, timedelta, timezone
from typing import Optional, TYPE_CHECKING, List

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Enum as SQLEnum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text as sa_text,  # epuka mgongano na field 'caption_text'
    Float,
    JSON as SA_JSON,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship, validates
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.event import listens_for

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


def _utc(dt_: Optional[datetime]) -> Optional[datetime]:
    if dt_ is None:
        return None
    return dt_ if dt_.tzinfo else dt_.replace(tzinfo=timezone.utc)


def _fmt_ts_srt(ms: int) -> str:
    """HH:MM:SS,mmm kwa SRT."""
    s, ms = divmod(ms, 1000)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _fmt_ts_vtt(ms: int) -> str:
    """HH:MM:SS.mmm kwa WebVTT."""
    s, ms = divmod(ms, 1000)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"


class CaptionSource(str, enum.Enum):
    live           = "live"
    transcription  = "transcription"
    translation    = "translation"
    manual         = "manual"
    import_file    = "import_file"
    other          = "other"


class SpeakerRole(str, enum.Enum):
    host     = "host"
    cohost   = "cohost"
    guest    = "guest"
    system   = "system"
    unknown  = "unknown"


class ReplayCaption(Base):
    """
    Vipande vya maneno (captions) vilivyohusishwa na LiveStream:
      - muda (start/end/offset/duration)
      - utambulisho (ordinal) kwa mpangilio thabiti
      - metadata: lugha, chanzo, mzungumzaji, confidence, styles, tokens
      - helpers za SRT/VTT, shifting na merging
    """
    __tablename__ = "replay_captions"
    __mapper_args__ = {"eager_defaults": True}
    __table_args__ = (
        UniqueConstraint("stream_id", "ordinal", name="uq_caption_stream_ordinal"),
        Index("ix_caption_stream_start", "stream_id", "start_at"),
        Index("ix_caption_stream_span", "stream_id", "start_at", "end_at"),
        Index("ix_caption_stream_created", "stream_id", "created_at"),
        Index("ix_caption_lang", "lang"),
        Index("ix_caption_source", "source"),
        Index("ix_caption_translated", "is_translated"),
        Index("ix_caption_idem", "idempotency_key"),
        CheckConstraint("ordinal >= 0", name="ck_caption_ordinal_nonneg"),
        CheckConstraint(
            "(end_at IS NULL) OR (start_at IS NULL) OR (end_at >= start_at)",
            name="ck_caption_end_after_start",
        ),
        CheckConstraint("offset_seconds >= 0.0", name="ck_caption_offset_nonneg"),
        CheckConstraint("duration_seconds >= 0.0", name="ck_caption_duration_nonneg"),
        CheckConstraint(
            "confidence IS NULL OR (confidence >= 0.0 AND confidence <= 1.0)",
            name="ck_caption_confidence_range",
        ),
        CheckConstraint("length(lang) BETWEEN 2 AND 10", name="ck_caption_lang_len"),
        CheckConstraint("length(caption_text) > 0", name="ck_caption_text_not_empty"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)

    # FK -> live_streams.id
    stream_id: Mapped[int] = mapped_column(
        ForeignKey("live_streams.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    stream: Mapped["LiveStream"] = relationship(
        "LiveStream",
        back_populates="captions",
        lazy="selectin",
        passive_deletes=True,
    )

    # Mpangilio wa kipande (SRT line / segment number)
    ordinal: Mapped[int] = mapped_column(Integer, server_default=sa_text("0"), nullable=False)

    # Muda
    start_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), index=True)
    end_at:   Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), index=True)
    offset_seconds:   Mapped[float] = mapped_column(Float, nullable=False, server_default=sa_text("0"))
    duration_seconds: Mapped[float] = mapped_column(Float, nullable=False, server_default=sa_text("0"))

    # Maandishi
    caption_text: Mapped[str] = mapped_column(Text, nullable=False)

    # Lugha & chanzo
    lang:   Mapped[str] = mapped_column(String(10), default="en", nullable=False, index=True)
    source: Mapped[CaptionSource] = mapped_column(
        SQLEnum(CaptionSource, name="caption_source", native_enum=False, validate_strings=True),
        default=CaptionSource.transcription,
        nullable=False,
        index=True,
    )

    # Mzungumzaji (hiari)
    speaker_label: Mapped[Optional[str]] = mapped_column(String(64))  # e.g., "spk_0"
    speaker_role:  Mapped[SpeakerRole] = mapped_column(
        SQLEnum(SpeakerRole, name="caption_speaker_role", native_enum=False, validate_strings=True),
        default=SpeakerRole.unknown,
        nullable=False,
        index=True,
    )

    # Uhakika/ubora (0..1)
    confidence: Mapped[Optional[float]] = mapped_column(Float)

    # Styles & tokenization (mutable JSON ili in-place updates zitambuliwe)
    styles: Mapped[Optional[dict]] = mapped_column(as_mutable_json(JSON_VARIANT))       # {"bold": False, ...}
    tokens: Mapped[Optional[List[dict]]] = mapped_column(as_mutable_json(JSON_VARIANT)) # [{"t":"hello","s":0.50,...}]

    # Tafsiri
    translated_from: Mapped[Optional[str]] = mapped_column(String(10))
    is_translated:   Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)

    # Idempotency / tracing
    idempotency_key: Mapped[Optional[str]] = mapped_column(String(120), unique=True)
    request_id:      Mapped[Optional[str]] = mapped_column(String(64), index=True)

    # Nyakati
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    # ---------- Hybrids ----------
    @hybrid_property
    def end_or_computed(self) -> Optional[datetime]:
        """Rudisha end_at au (start_at + duration) kama end_at haipo."""
        if self.end_at:
            return self.end_at
        if self.start_at:
            return self.start_at + timedelta(seconds=self.duration_seconds or 0.0)
        return None

    @hybrid_property
    def start_ms(self) -> Optional[int]:
        """Mwanzo kwa milliseconds (kwa SRT/VTT)."""
        if not self.start_at:
            return None
        return int(self.start_at.timestamp() * 1000)

    @hybrid_property
    def end_ms(self) -> Optional[int]:
        """Mwisho kwa milliseconds (kwa SRT/VTT)."""
        e = self.end_or_computed
        return int(e.timestamp() * 1000) if e else None

    @hybrid_property
    def has_timing(self) -> bool:
        return bool(self.start_at or self.duration_seconds > 0 or self.end_at)

    # ---------- Validators / Helpers ----------
    @validates("lang")
    def _norm_lang(self, _, value: str) -> str:
        v = (value or "en").strip()
        return v if 2 <= len(v) <= 10 else "en"

    @validates("caption_text", "speaker_label", "translated_from", "idempotency_key", "request_id")
    def _trim(self, _k: str, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        s = v.strip()
        return s or None

    @validates("start_at", "end_at")
    def _tz_guard(self, _key, value: Optional[datetime]) -> Optional[datetime]:
        return _utc(value)

    @validates("offset_seconds", "duration_seconds", "confidence")
    def _nonneg(self, key: str, value: Optional[float]) -> Optional[float]:
        if value is None:
            return None
        if key in {"offset_seconds", "duration_seconds"}:
            return max(0.0, float(value))
        # confidence
        v = float(value)
        if v < 0.0:
            v = 0.0
        if v > 1.0:
            v = 1.0
        return v

    # ---- public helpers ----
    def set_span(
        self,
        *,
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
        offset_seconds: Optional[float] = None,
        duration_seconds: Optional[float] = None,
    ) -> None:
        if start is not None:
            self.start_at = _utc(start)
        if end is not None:
            self.end_at = _utc(end)
        if offset_seconds is not None:
            self.offset_seconds = max(0.0, float(offset_seconds))
        if duration_seconds is not None:
            self.duration_seconds = max(0.0, float(duration_seconds))

    def ensure_duration(self) -> None:
        """Kama tuna start_at & end_at bila duration, weka duration = end - start."""
        if self.start_at and self.end_at and (self.duration_seconds or 0.0) <= 0.0:
            self.duration_seconds = max(0.0, (self.end_at - self.start_at).total_seconds())

    def ensure_end(self) -> None:
        """Kama tuna start_at & duration bila end_at, kisha set end_at = start + duration."""
        if self.start_at and not self.end_at and (self.duration_seconds or 0.0) > 0.0:
            self.end_at = self.start_at + timedelta(seconds=float(self.duration_seconds))

    def append_text(self, extra: str) -> None:
        self.caption_text = (self.caption_text or "") + (extra or "")

    def shift(self, seconds: float) -> None:
        """Sogeza mwanzo/mwisho kwa sekunde (+/-)."""
        delta = timedelta(seconds=float(seconds))
        if self.start_at:
            self.start_at = _utc(self.start_at + delta)
        if self.end_at:
            self.end_at = _utc(self.end_at + delta)

    def merge_with(self, other: "ReplayCaption") -> None:
        """Unganisha maandishi & kurefusha muda hadi mwisho wa 'other'."""
        self.caption_text = (self.caption_text or "") + " " + (other.caption_text or "")
        if other.end_or_computed:
            self.end_at = other.end_or_computed
        if not self.start_at and other.start_at:
            self.start_at = other.start_at
        self.ensure_duration()

    def mark_translated(self, *, from_lang: str | None = None) -> None:
        self.is_translated = True
        if from_lang:
            self.translated_from = from_lang

    # ---- Export helpers ----
    def to_srt_block(self) -> str:
        """Rudisha block moja ya SRT (ikishindikana, hutoa maandishi tu)."""
        if not self.start_ms or not self.end_ms:
            return f"{self.ordinal}\n{self.caption_text}\n"
        return (
            f"{self.ordinal}\n"
            f"{_fmt_ts_srt(self.start_ms)} --> {_fmt_ts_srt(self.end_ms)}\n"
            f"{self.caption_text}\n"
        )

    def to_vtt_block(self) -> str:
        """Rudisha cue ya VTT (bila 'WEBVTT' header)."""
        if not self.start_ms or not self.end_ms:
            return f"{self.caption_text}\n"
        return (
            f"{_fmt_ts_vtt(self.start_ms)} --> {_fmt_ts_vtt(self.end_ms)}\n"
            f"{self.caption_text}\n"
        )

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<ReplayCaption id={self.id} stream={self.stream_id} "
            f"ord={self.ordinal} lang={self.lang} src={self.source}>"
        )


# --------- Event listeners: normalize/derive fields ---------
@listens_for(ReplayCaption, "before_insert")
def _cap_before_insert(_m, _c, target: ReplayCaption) -> None:  # pragma: no cover
    # TZ guard & derived fields
    target.start_at = _utc(target.start_at)
    target.end_at = _utc(target.end_at)
    # Derive missing time data
    target.ensure_duration()
    target.ensure_end()
    # Trim text
    if target.caption_text:
        target.caption_text = target.caption_text.strip()

@listens_for(ReplayCaption, "before_update")
def _cap_before_update(_m, _c, target: ReplayCaption) -> None:  # pragma: no cover
    target.start_at = _utc(target.start_at)
    target.end_at = _utc(target.end_at)
    target.ensure_duration()
    target.ensure_end()
    if target.caption_text:
        target.caption_text = target.caption_text.strip()
