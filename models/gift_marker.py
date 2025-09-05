# backend/models/gift_marker.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import enum
import datetime as dt
from typing import Optional, TYPE_CHECKING, Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum as SQLEnum,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship, validates
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.ext.mutable import MutableDict
from sqlalchemy.event import listens_for

from backend.db import Base
from backend.models._types import JSON_VARIANT  # portable JSON (PG: JSONB; others: JSON)

if TYPE_CHECKING:
    from .live_stream import LiveStream
    from .gift_fly import GiftFly


# ---------- Enums ----------
class MarkerType(str, enum.Enum):
    gift = "gift"            # imechochewa na tukio la zawadi
    milestone = "milestone"  # lengo/hatua ya stream
    manual = "manual"        # imeongezwa na host/mod


class GiftMarker(Base):
    """
    Alama (marker) kwenye LiveStream kwa ajili ya kurudia, analytics, au chapters.
    Inaweza kuunganishwa na tukio la `GiftFly` (hiari).

    Lengo:
    - Cross-DB portability (JSON_VARIANT)
    - In-place updates kwenye `meta` kupitia MutableDict
    - Idempotency key kuzuia marudio
    - Helpers/hybrids kwa offsets na usability ya API
    """
    __tablename__ = "gift_markers"
    __mapper_args__ = {"eager_defaults": True}

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)

    # Wapi & chanzo
    stream_id: Mapped[int] = mapped_column(
        ForeignKey("live_streams.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    gift_fly_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("gift_fly_events.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    # Taarifa ya alama
    gift_name: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    marker_type: Mapped[MarkerType] = mapped_column(
        SQLEnum(MarkerType, name="gift_marker_type", native_enum=False, validate_strings=True),
        default=MarkerType.gift,
        nullable=False,
        index=True,
    )

    # Nafasi/timeline
    position: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    offset_seconds: Mapped[Optional[int]] = mapped_column(Integer)  # sekunde tangu mwanzo wa stream
    offset_ms: Mapped[Optional[int]] = mapped_column(Integer)       # granularity zaidi

    # Maelezo ya ziada / UI
    note: Mapped[Optional[str]] = mapped_column(String(240))
    meta: Mapped[Optional[dict[str, Any]]] = mapped_column(
        MutableDict.as_mutable(JSON_VARIANT)
    )  # {"color":"neon","icon":"..."} n.k.
    idempotency_key: Mapped[Optional[str]] = mapped_column(String(100), unique=True, index=True)

    # Timestamps
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP"), nullable=False, index=True
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("CURRENT_TIMESTAMP"),
        onupdate=text("CURRENT_TIMESTAMP"),
        nullable=False,
    )

    # Relationships
    stream: Mapped["LiveStream"] = relationship(
        "LiveStream",
        back_populates="gift_markers",
        foreign_keys=[stream_id],
        passive_deletes=True,
        lazy="selectin",
    )
    gift_fly: Mapped[Optional["GiftFly"]] = relationship(
        "GiftFly",
        foreign_keys=[gift_fly_id],
        lazy="joined",
        passive_deletes=True,
    )

    # ---------- Hybrids ----------
    @hybrid_property
    def has_gift_event(self) -> bool:
        return self.gift_fly_id is not None

    @hybrid_property
    def effective_offset_ms(self) -> Optional[int]:
        """Rudisha offset kwa milisekunde (ikijumlisha sekunde + ms)."""
        if self.offset_seconds is None and self.offset_ms is None:
            return None
        sec = int(self.offset_seconds or 0) * 1000
        ms = int(self.offset_ms or 0)
        return max(0, sec + ms)

    # ---------- Helpers ----------
    def set_offset(self, *, seconds: int | None = None, ms: int | None = None) -> None:
        if seconds is not None:
            self.offset_seconds = max(0, int(seconds))
        if ms is not None:
            self.offset_ms = max(0, int(ms))

    def bump_position(self, step: int = 1) -> None:
        self.position = max(0, (self.position or 0) + max(0, int(step)))

    def set_idempotency(self, key: str | None) -> None:
        self.idempotency_key = (key or "").strip()[:100] or None

    def merge_meta(self, **kv: Any) -> None:
        self.meta = {**(self.meta or {}), **kv}

    # ---------- Validators ----------
    @validates("gift_name", "note")
    def _trim_strings(self, key: str, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        v = value.strip()
        maxlen = {"gift_name": 120, "note": 240}[key]
        return v[:maxlen] or None

    @validates("position")
    def _val_position(self, _k: str, v: int) -> int:
        iv = int(v or 0)
        if iv < 0:
            raise ValueError("position must be >= 0")
        return iv

    @validates("offset_seconds", "offset_ms")
    def _val_offsets(self, _k: str, v: Optional[int]) -> Optional[int]:
        if v is None:
            return None
        iv = int(v)
        if iv < 0:
            raise ValueError("offsets must be >= 0")
        # guard against silly values (e.g. > 24h in ms)
        if _k == "offset_ms" and iv > 24 * 60 * 60 * 1000:
            raise ValueError("offset_ms too large")
        if _k == "offset_seconds" and iv > 24 * 60 * 60:
            raise ValueError("offset_seconds too large")
        return iv

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<GiftMarker id={self.id} stream={self.stream_id} type={self.marker_type} "
            f"gift={self.gift_name!r} pos={self.position} t={self.offset_seconds}s>"
        )

    # ---------- Indexes & Constraints ----------
    __table_args__ = (
        UniqueConstraint("stream_id", "position", name="uq_gm_stream_position"),
        Index("ix_gm_stream_time", "stream_id", "created_at"),
        Index("ix_gm_type_position", "marker_type", "position"),
        Index("ix_gm_stream_offset", "stream_id", "offset_seconds"),
        Index("ix_gm_gift_lower", func.lower(gift_name)),
        CheckConstraint("position >= 0", name="ck_gm_position_nonneg"),
        CheckConstraint(
            "(offset_seconds IS NULL OR offset_seconds >= 0) AND (offset_ms IS NULL OR offset_ms >= 0)",
            name="ck_gm_offsets_nonneg",
        ),
        CheckConstraint("length(trim(gift_name)) >= 2", name="ck_gm_gift_name_minlen"),
    )


# ---------- Normalizers (auto-fix before write) ----------
@listens_for(GiftMarker, "before_insert")
def _gm_before_insert(_m, _c, t: GiftMarker) -> None:  # pragma: no cover
    if t.gift_name:
        t.gift_name = t.gift_name.strip()[:120]
    if t.note:
        t.note = t.note.strip()[:240]
    if t.idempotency_key:
        t.idempotency_key = t.idempotency_key.strip()[:100]
    # clamp offsets
    if t.offset_seconds is not None and t.offset_seconds < 0:
        t.offset_seconds = 0
    if t.offset_ms is not None and t.offset_ms < 0:
        t.offset_ms = 0


@listens_for(GiftMarker, "before_update")
def _gm_before_update(_m, _c, t: GiftMarker) -> None:  # pragma: no cover
    # keep normalization consistent
    if t.gift_name:
        t.gift_name = t.gift_name.strip()[:120]
    if t.note:
        t.note = t.note.strip()[:240]
    if t.idempotency_key:
        t.idempotency_key = t.idempotency_key.strip()[:100]
    if t.offset_seconds is not None and t.offset_seconds < 0:
        t.offset_seconds = 0
    if t.offset_ms is not None and t.offset_ms < 0:
        t.offset_ms = 0
