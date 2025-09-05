# backend/models/recorded_stream.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import enum
import datetime as dt
from typing import Optional, TYPE_CHECKING, List, Dict, Any

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
    text,
    JSON as SA_JSON,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship, validates
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.event import listens_for

from backend.db import Base
from backend.models._types import JSON_VARIANT, as_mutable_json

# ---- Portable JSON: JSON_VARIANT on Postgres, JSON elsewhere ----
try:
    # JSON_VARIANT tayari inatolewa na _types; hakuna cha kufanya
    pass
except Exception:  # pragma: no cover
    pass  # fallback isiyovunja sintaksia

if TYPE_CHECKING:
    from .live_stream import LiveStream
    from .user import User
    # from .video_post import VideoPost  # weka kweli ukitumia

def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)

class StorageProvider(str, enum.Enum):
    local = "local"
    s3    = "s3"
    gcs   = "gcs"
    r2    = "r2"
    azure = "azure"
    other = "other"

class RecordingStatus(str, enum.Enum):
    recording  = "recording"
    uploading  = "uploading"
    processing = "processing"
    ready      = "ready"
    failed     = "failed"
    archived   = "archived"

class Privacy(str, enum.Enum):
    public   = "public"
    unlisted = "unlisted"
    private  = "private"

class RecordedStream(Base):
    """
    Rekodi ya LiveStream:
      - Kiungo na LiveStream, (hiari) User, na (hiari) VideoPost
      - Metadata ya media (duration/dimensions/codec/bitrate/size)
      - Renditions (HLS/DASH/mp4), subtitles, na viungo (playback/download/thumb)
      - Lifecycle helpers (uploading→processing→ready/failed/archived)
      - Normalization/validation + indices za utafutaji wa haraka
    """
    __tablename__ = "recorded_streams"
    __mapper_args__ = {"eager_defaults": True}
    __table_args__ = (
        UniqueConstraint("stream_id", "storage_key", name="uq_rec_stream_storage_key"),
        UniqueConstraint("checksum_sha256", name="uq_rec_stream_checksum"),
        UniqueConstraint("idempotency_key", name="uq_rec_stream_idem"),
        Index("ix_rec_stream_stream_created", "stream_id", "created_at"),
        Index("ix_rec_stream_status_time", "status", "created_at"),
        Index("ix_rec_stream_provider", "storage_provider"),
        Index("ix_rec_stream_user", "user_id"),
        Index("ix_rec_stream_privacy_status", "privacy", "status"),
        Index("ix_rec_stream_ready_at", "ready_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)

    # Chanzo
    stream_id: Mapped[int] = mapped_column(
        ForeignKey("live_streams.id", ondelete="CASCADE"), nullable=False, index=True
    )
    stream: Mapped["LiveStream"] = relationship(
        "LiveStream", back_populates="recorded_streams", lazy="selectin", passive_deletes=True
    )

    # (Hiari) mmiliki/host wa wakati huo (denormalized kwa maswali ya haraka)
    user_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    user: Mapped[Optional["User"]] = relationship("User", lazy="selectin")

    # Mahali pa kuhifadhi
    storage_provider: Mapped[StorageProvider] = mapped_column(
        SQLEnum(StorageProvider, name="storage_provider", native_enum=False, validate_strings=True),
        default=StorageProvider.local,
        nullable=False,
        index=True,
    )
    storage_bucket: Mapped[Optional[str]] = mapped_column(String(120))
    storage_key:    Mapped[Optional[str]] = mapped_column(String(512))   # path/key ndani ya bucket
    file_path:      Mapped[Optional[str]] = mapped_column(String(512))   # legacy/local path
    playback_url:   Mapped[Optional[str]] = mapped_column(String(1024))  # HLS/DASH/mp4 URL
    download_url:   Mapped[Optional[str]] = mapped_column(String(1024))
    thumbnail_url:  Mapped[Optional[str]] = mapped_column(String(1024))

    # Uhusiano na VideoPost (1:1 hiari)
    # KUMBUKA: hakikisha VideoPost ina back_populates="recorded_stream" ukitumia
    video_post: Mapped[Optional["VideoPost"]] = relationship(
        "VideoPost", back_populates="recorded_stream", uselist=False, lazy="selectin"
    )

    # Media metadata
    duration_seconds: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    width:   Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    height:  Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    fps:     Mapped[Optional[int]] = mapped_column(Integer)
    bitrate_kbps: Mapped[Optional[int]] = mapped_column(Integer)
    codec_video:  Mapped[Optional[str]] = mapped_column(String(64))
    codec_audio:  Mapped[Optional[str]] = mapped_column(String(64))
    aspect_ratio: Mapped[Optional[str]] = mapped_column(String(16))  # "16:9", "9:16", ...

    # Ukubwa & uadilifu
    size_bytes:      Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    checksum_sha256: Mapped[Optional[str]] = mapped_column(String(100), index=True)

    # Renditions & metadata nyingine
    renditions: Mapped[Optional[List[dict]]] = mapped_column(as_mutable_json(JSON_VARIANT))  # [{"quality":"720p","url":"..."}]
    subtitles:  Mapped[Optional[List[dict]]] = mapped_column(as_mutable_json(JSON_VARIANT))  # [{"lang":"en","url":"..."}]
    meta:       Mapped[Optional[dict]] = mapped_column(as_mutable_json(JSON_VARIANT))

    # Hali na faragha
    status: Mapped[RecordingStatus] = mapped_column(
        SQLEnum(RecordingStatus, name="recording_status", native_enum=False, validate_strings=True),
        default=RecordingStatus.uploading,
        nullable=False,
        index=True,
    )
    privacy: Mapped[Privacy] = mapped_column(
        SQLEnum(Privacy, name="recording_privacy", native_enum=False, validate_strings=True),
        default=Privacy.unlisted,
        nullable=False,
        index=True,
    )

    # Rejea za nje / idempotency
    idempotency_key: Mapped[Optional[str]] = mapped_column(String(120), unique=True, index=True)
    external_ref:    Mapped[Optional[str]] = mapped_column(String(160), index=True)  # job/run id n.k.

    # Nyakati
    started_at:  Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True), index=True)
    ended_at:    Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True), index=True)
    uploaded_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP"), nullable=False, index=True
    )
    created_at:  Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP"), nullable=False, index=True
    )
    updated_at:  Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("CURRENT_TIMESTAMP"),
        onupdate=text("CURRENT_TIMESTAMP"),
        nullable=False,
    )
    ready_at:    Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    failed_at:   Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))

    # ---------- Hybrids ----------
    @hybrid_property
    def is_ready(self) -> bool:
        return self.status == RecordingStatus.ready and bool(self.playback_url)

    @hybrid_property
    def duration_minutes(self) -> int:
        return max(0, (self.duration_seconds or 0) // 60)

    @hybrid_property
    def resolution(self) -> str:
        return f"{self.width}x{self.height}"

    # ---------- Validators ----------
    @validates(
        "storage_bucket", "storage_key", "file_path", "playback_url",
        "download_url", "thumbnail_url", "codec_video", "codec_audio",
        "aspect_ratio", "checksum_sha256", "external_ref", "idempotency_key"
    )
    def _trim_text(self, _k: str, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        s = v.strip()
        return s or None

    # ---------- Helpers ----------
    def set_media_info(
        self,
        *,
        duration_seconds: int | float | None = None,
        width: int | None = None,
        height: int | None = None,
        fps: int | None = None,
        bitrate_kbps: int | None = None,
        codec_video: Optional[str] = None,
        codec_audio: Optional[str] = None,
        aspect_ratio: Optional[str] = None,
        size_bytes: int | None = None,
    ) -> None:
        if duration_seconds is not None:
            self.duration_seconds = max(0, int(duration_seconds))
        if width is not None:
            self.width = max(0, int(width))
        if height is not None:
            self.height = max(0, int(height))
        if fps is not None:
            self.fps = max(0, int(fps))
        if bitrate_kbps is not None:
            self.bitrate_kbps = max(0, int(bitrate_kbps))
        if codec_video is not None:
            self.codec_video = codec_video.strip() or None
        if codec_audio is not None:
            self.codec_audio = codec_audio.strip() or None
        if aspect_ratio is not None:
            self.aspect_ratio = aspect_ratio.strip() or None
        if size_bytes is not None:
            self.size_bytes = max(0, int(size_bytes))

    def add_rendition(self, rendition: Dict[str, Any]) -> None:
        """Ongeza rendition moja: keys zinazosaidia: quality, url, bandwidth, mime."""
        arr = list(self.renditions or [])
        arr.append(rendition)
        self.renditions = arr

    def add_subtitle(self, sub: Dict[str, Any]) -> None:
        """Ongeza subtitle: {"lang":"en","url":"...","kind":"captions"}"""
        arr = list(self.subtitles or [])
        arr.append(sub)
        self.subtitles = arr

    def best_rendition(self, prefer: str = "1080p") -> Optional[Dict[str, Any]]:
        """Rudisha rendition bora kwa heuristic rahisi ya 'quality'."""
        if not self.renditions:
            return None
        # Jaribu mechi halisi ya quality, vinginevyo chukua yenye 'bandwidth' kubwa
        for r in self.renditions:
            if str(r.get("quality", "")).lower() == str(prefer).lower():
                return r
        return max(self.renditions, key=lambda r: int(r.get("bandwidth", 0)))

    def mark_uploading(self) -> None:
        self.status = RecordingStatus.uploading
        self.uploaded_at = self.uploaded_at or _utcnow()

    def mark_processing(self) -> None:
        self.status = RecordingStatus.processing

    def mark_ready(self, *, playback_url: Optional[str] = None, download_url: Optional[str] = None) -> None:
        self.status = RecordingStatus.ready
        self.ready_at = _utcnow()
        if playback_url:
            self.playback_url = playback_url.strip() or None
        if download_url:
            self.download_url = download_url.strip() or None

    def mark_failed(self) -> None:
        self.status = RecordingStatus.failed
        self.failed_at = _utcnow()

    def archive(self) -> None:
        """Tia kumbukumbu (si kufuta)."""
        self.status = RecordingStatus.archived

    # ---------- SQL Guards ----------
    __table_args__ = (
        CheckConstraint("duration_seconds >= 0", name="ck_rec_duration_nonneg"),
        CheckConstraint("size_bytes >= 0", name="ck_rec_size_nonneg"),
        CheckConstraint("width >= 0 AND height >= 0", name="ck_rec_dims_nonneg"),
        CheckConstraint(
            "(ended_at IS NULL) OR (started_at IS NULL) OR (ended_at >= started_at)",
            name="ck_rec_end_after_start",
        ),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<RecordedStream id={self.id} stream={self.stream_id} "
            f"status={self.status} provider={self.storage_provider} res={self.resolution}>"
        )


# ---------- Normalization hooks ----------
@listens_for(RecordedStream, "before_insert")
def _rs_before_insert(_m, _c, t: RecordedStream) -> None:  # pragma: no cover
    # Normalizations za vitufe vya maandishi
    for attr in ("storage_bucket", "storage_key", "file_path",
                 "playback_url", "download_url", "thumbnail_url",
                 "checksum_sha256", "external_ref", "aspect_ratio"):
        val = getattr(t, attr, None)
        if isinstance(val, str):
            setattr(t, attr, val.strip() or None)
    # Auto-fill ready flag
    if t.status == RecordingStatus.ready and not t.ready_at:
        t.ready_at = _utcnow()
    # End-after-start guard kwa upande wa programu
    if t.started_at and t.ended_at and t.ended_at < t.started_at:
        t.ended_at = t.started_at


@listens_for(RecordedStream, "before_update")
def _rs_before_update(_m, _c, t: RecordedStream) -> None:  # pragma: no cover
    # Trims
    for attr in ("storage_bucket", "storage_key", "file_path",
                 "playback_url", "download_url", "thumbnail_url",
                 "checksum_sha256", "external_ref", "aspect_ratio"):
        val = getattr(t, attr, None)
        if isinstance(val, str):
            setattr(t, attr, val.strip() or None)
    # Keep ready_at in sync
    if t.status == RecordingStatus.ready and not t.ready_at:
        t.ready_at = _utcnow()
