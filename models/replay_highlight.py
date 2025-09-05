# backend/models/replay_highlight.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import enum
import re
import datetime as dt
from typing import Optional, List, TYPE_CHECKING

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
    text as sa_text,
    Float,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship, validates
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.event import listens_for

from backend.db import Base
from backend.models._types import JSON_VARIANT, as_mutable_json

# Hakuna try/except inayovunja indentation hapa—JSON_VARIANT tayari imetolewa na _types
# na inafanya kazi kwenye Postgres/SQLite/MySQL sawasawa.

if TYPE_CHECKING:
    from .video_post import VideoPost

_TS_RE = re.compile(r"^(?P<h>\d{1,2}):(?P<m>[0-5]?\d):(?P<s>[0-5]?\d)(?:\.(?P<ms>\d{1,3}))?$")

def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)

def _hhmmss_to_seconds(s: str) -> float:
    """'HH:MM:SS(.mmm)' au 'MM:SS(.mmm)' -> sekunde (float)."""
    s = (s or "").strip()
    if not s:
        return 0.0
    m = _TS_RE.match(s)
    if m:
        h = int(m.group("h"))
        mi = int(m.group("m"))
        sec = int(m.group("s"))
        ms = m.group("ms")
        base = h * 3600 + mi * 60 + sec
        return float(base) + (int(ms) / 1000.0 if ms else 0.0)
    # jaribu MM:SS(.mmm)
    parts = s.split(":")
    if len(parts) == 2:
        h, mi, sec = 0, int(parts[0]), float(parts[1])
        return h * 3600 + mi * 60 + sec
    return 0.0

def _seconds_to_hhmmss(x: float) -> str:
    """Sekunde (float) -> 'HH:MM:SS(.mmm)'."""
    total_ms = int(round(max(0.0, float(x)) * 1000))
    s, ms = divmod(total_ms, 1000)
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    base = f"{h:02d}:{m:02d}:{s:02d}"
    return f"{base}.{ms:03d}" if ms else base

class HighlightKind(str, enum.Enum):
    moment      = "moment"      # nukta moja
    clip        = "clip"        # range
    achievement = "achievement"
    reaction    = "reaction"
    other       = "other"

class Visibility(str, enum.Enum):
    public   = "public"
    unlisted = "unlisted"
    private  = "private"

class ReplayHighlight(Base):
    """
    Vipande vya 'highlight' kwa VideoPost:
      - 'moment' (nukta) au 'clip' (range)
      - uonekano, tagi, na metadata
      - ordering ('ordinal') kwa mpangilio thabiti wa UI
      - `fingerprint` ya kudeduplicate (video + aina + start + end + title)
    """
    __tablename__ = "replay_highlights"
    __mapper_args__ = {"eager_defaults": True}
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_replay_highlight_idem"),
        UniqueConstraint("fingerprint", name="uq_replay_highlight_fpr"),
        Index("ix_highlight_video_pos", "video_post_id", "position_seconds"),
        Index("ix_highlight_video_created", "video_post_id", "created_at"),
        Index("ix_highlight_kind_visibility", "kind", "visibility"),
        CheckConstraint("ordinal >= 0", name="ck_highlight_ordinal_nonneg"),
        CheckConstraint("position_seconds >= 0.0", name="ck_highlight_pos_nonneg"),
        CheckConstraint(
            "end_position_seconds IS NULL OR end_position_seconds >= position_seconds",
            name="ck_highlight_end_gte_start",
        ),
        CheckConstraint("length(title) > 0", name="ck_highlight_title_not_empty"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)

    # Target video
    video_post_id: Mapped[int] = mapped_column(
        ForeignKey("video_posts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    video_post: Mapped["VideoPost"] = relationship(
        "VideoPost", back_populates="highlights", lazy="selectin", passive_deletes=True
    )

    # Meta ya highlight
    title: Mapped[str] = mapped_column(String(140), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text)
    kind: Mapped[HighlightKind] = mapped_column(
        SQLEnum(HighlightKind, name="replay_highlight_kind", native_enum=False, validate_strings=True),
        default=HighlightKind.moment,
        nullable=False,
        index=True,
    )

    # Ordering ndani ya video ile ile
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False, server_default=sa_text("0"))

    # Nafasi ya tukio (sekunde tangu mwanzo wa video)
    position_seconds: Mapped[float] = mapped_column(Float, nullable=False, server_default=sa_text("0"))
    end_position_seconds: Mapped[Optional[float]] = mapped_column(Float)

    # (Hiari) hifadhi pia HH:MM:SS kwa urafiki wa UI
    timestamp_str: Mapped[Optional[str]] = mapped_column(String(16), index=True)
    end_timestamp_str: Mapped[Optional[str]] = mapped_column(String(16), index=True)

    # Uonekano & sifa
    visibility: Mapped[Visibility] = mapped_column(
        SQLEnum(Visibility, name="replay_highlight_visibility", native_enum=False, validate_strings=True),
        default=Visibility.unlisted,
        nullable=False,
        index=True,
    )
    is_pinned: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)

    # Picha/viungo
    thumbnail_url: Mapped[Optional[str]] = mapped_column(String(1024))
    poster_url:    Mapped[Optional[str]] = mapped_column(String(1024))

    # Tags & meta (mutable JSON)
    tags: Mapped[Optional[List[str]]] = mapped_column(as_mutable_json(JSON_VARIANT))
    meta: Mapped[Optional[dict]] = mapped_column(as_mutable_json(JSON_VARIANT))

    # Dedupe / tracing
    fingerprint:     Mapped[Optional[str]] = mapped_column(String(64), unique=True, index=True)
    idempotency_key: Mapped[Optional[str]] = mapped_column(String(120), unique=True)
    request_id:      Mapped[Optional[str]] = mapped_column(String(64), index=True)

    # Nyakati
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=sa_text("CURRENT_TIMESTAMP"), nullable=False, index=True
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=sa_text("CURRENT_TIMESTAMP"),
        onupdate=sa_text("CURRENT_TIMESTAMP"),
        nullable=False,
    )

    # ---------- Hybrids ----------
    @hybrid_property
    def is_clip(self) -> bool:
        return self.end_position_seconds is not None

    @hybrid_property
    def duration_seconds(self) -> float:
        """Muda wa highlight (sekunde). 0 ikiwa ni 'moment'."""
        if self.end_position_seconds is None:
            return 0.0
        return max(0.0, float(self.end_position_seconds) - float(self.position_seconds or 0.0))

    # ---------- Validators ----------
    @validates("title", "description", "thumbnail_url", "poster_url", "timestamp_str",
               "end_timestamp_str", "request_id", "idempotency_key")
    def _trim(self, _k: str, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        s = v.strip()
        if _k in {"timestamp_str", "end_timestamp_str"} and s:
            # ruhusu pia MM:SS(.mmm)
            if not (_TS_RE.match(s) or len(s.split(":")) == 2):
                # batili -> acha ORM iandike None ili tusihifadhi uovu
                return None
        return s or None

    @validates("position_seconds", "end_position_seconds")
    def _nonneg(self, _k: str, v: Optional[float]) -> Optional[float]:
        if v is None:
            return None
        return max(0.0, float(v))

    # ---------- Helpers ----------
    def set_position_from_hhmmss(self, s: str) -> None:
        self.position_seconds = max(0.0, _hhmmss_to_seconds(s))
        self.timestamp_str = (s or "").strip() or None

    def set_end_from_hhmmss(self, s: str) -> None:
        self.end_position_seconds = max(0.0, _hhmmss_to_seconds(s))
        self.end_timestamp_str = (s or "").strip() or None

    def set_range_seconds(self, start: float, end: Optional[float] = None) -> None:
        self.position_seconds = max(0.0, float(start))
        self.end_position_seconds = None if end is None else max(self.position_seconds, float(end))
        self.timestamp_str = _seconds_to_hhmmss(self.position_seconds)
        self.end_timestamp_str = (
            None if self.end_position_seconds is None else _seconds_to_hhmmss(self.end_position_seconds)
        )

    def set_range_hhmmss(self, start: str, end: Optional[str] = None) -> None:
        self.set_position_from_hhmmss(start)
        self.end_position_seconds = None
        self.end_timestamp_str = None
        if end is not None:
            self.set_end_from_hhmmss(end)

    def ensure_timestamps(self) -> None:
        """Hakikisha HH:MM:SS zipo kulingana na sekunde."""
        if not self.timestamp_str:
            self.timestamp_str = _seconds_to_hhmmss(self.position_seconds or 0.0)
        if self.end_position_seconds is not None and not self.end_timestamp_str:
            self.end_timestamp_str = _seconds_to_hhmmss(self.end_position_seconds or 0.0)

    def sync_seconds_from_timestamps(self) -> None:
        """Kama HH:MM:SS zimekuja, hakikisha sekunde zimejaa sawa."""
        if self.timestamp_str:
            self.position_seconds = max(0.0, _hhmmss_to_seconds(self.timestamp_str))
        if self.end_timestamp_str:
            self.end_position_seconds = max(0.0, _hhmmss_to_seconds(self.end_timestamp_str))

    def pin(self) -> None:
        self.is_pinned = True

    def unpin(self) -> None:
        self.is_pinned = False

    # ---- Fingerprint / Dedupe ----
    def compute_fingerprint(self) -> str:
        # video|kind|vis|start|end|title_norm
        start = f"{float(self.position_seconds or 0.0):.3f}"
        end = "" if self.end_position_seconds is None else f"{float(self.end_position_seconds):.3f}"
        title_norm = (self.title or "").strip().lower()
        raw = "|".join([str(self.video_post_id or ""), str(self.kind or ""), str(self.visibility or ""), start, end, title_norm])
        # sha1 si lazima; key yetu fupi inatosha pia—tunatumia hex ya sha1 kwa uthabiti
        import hashlib
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()

    def refresh_fingerprint(self) -> None:
        self.fingerprint = self.compute_fingerprint()

    def __repr__(self) -> str:  # pragma: no cover
        rng = (
            f"{self.timestamp_str}?{self.end_timestamp_str}"
            if self.end_position_seconds is not None
            else self.timestamp_str or "00:00:00"
        )
        return f"<ReplayHighlight id={self.id} video={self.video_post_id} kind={self.kind} {rng} title={self.title!r}>"

# --------- Event listeners: normalize & derive fields ---------
@listens_for(ReplayHighlight, "before_insert")
def _rh_before_insert(_m, _c, target: ReplayHighlight) -> None:  # pragma: no cover
    target.sync_seconds_from_timestamps()
    target.ensure_timestamps()
    target.refresh_fingerprint()

@listens_for(ReplayHighlight, "before_update")
def _rh_before_update(_m, _c, target: ReplayHighlight) -> None:  # pragma: no cover
    target.sync_seconds_from_timestamps()
    target.ensure_timestamps()
    target.refresh_fingerprint()
