# -*- coding: utf-8 -*-
from __future__ import annotations

import secrets
import enum
import datetime as dt
from typing import Optional, Dict, Any, List, TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    UniqueConstraint,
    func,
    Enum as SQLEnum,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.db import Base
from backend.models._types import JSON_VARIANT, DECIMAL_TYPE, as_mutable_json

if TYPE_CHECKING:
    from .live_stream import LiveStream

# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€ Enums (DB-safe via SQLEnum) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
class Visibility(str, enum.Enum):
    public = "public"
    unlisted = "unlisted"
    private = "private"
    followers = "followers"

class LatencyMode(str, enum.Enum):
    normal = "normal"
    low = "low"
    ultra_low = "ultra_low"

class ChatMode(str, enum.Enum):
    everyone = "everyone"
    followers = "followers"
    subscribers = "subscribers"
    disabled = "disabled"

class RecordingQuality(str, enum.Enum):
    auto = "auto"
    p360 = "360p"
    p480 = "480p"
    p720 = "720p"
    p1080 = "1080p"
    p1440 = "1440p"
    p2160 = "2160p"

class QualityPreset(str, enum.Enum):
    auto = "auto"
    data_saver = "data_saver"
    balanced = "balanced"
    high_quality = "high_quality"

class StreamSettings(Base):
    """
    StreamSettings â€” one row per live stream controlling AV, chat, privacy & ingest.

    â€¢ Typed mappings + TZ-aware timestamps
    â€¢ Strict constraints & hot indexes
    â€¢ Profiles (quality_preset) + latency
    â€¢ Chat controls (mode, slow mode)
    â€¢ Recording/DVR/captions & language
    â€¢ Monetization & visibility toggles
    """
    __tablename__ = "stream_settings"
    __mapper_args__ = {"eager_defaults": True}
    __table_args__ = (
        UniqueConstraint("stream_id", name="uq_stream_settings_stream"),
        # numeric bounds
        CheckConstraint("video_width  BETWEEN 160 AND 7680", name="ck_stream_video_width_bounds"),
        CheckConstraint("video_height BETWEEN 120 AND 4320", name="ck_stream_video_height_bounds"),
        CheckConstraint("bitrate_kbps BETWEEN 100 AND 50000", name="ck_stream_bitrate_bounds"),
        CheckConstraint("framerate_fps BETWEEN 1 AND 120", name="ck_stream_fps_bounds"),
        CheckConstraint("slow_mode_seconds BETWEEN 0 AND 3600", name="ck_stream_slowmode_bounds"),
        CheckConstraint("start_delay_seconds BETWEEN 0 AND 600", name="ck_stream_start_delay_bounds"),
        CheckConstraint("min_superchat_minor IS NULL OR min_superchat_minor >= 0", name="ck_stream_superchat_min_nonneg"),
        CheckConstraint("length(language) BETWEEN 2 AND 12", name="ck_stream_language_len"),
        # indexes
        Index("ix_stream_settings_stream", "stream_id"),
        Index("ix_stream_settings_visibility", "visibility"),
        Index("ix_stream_settings_latency", "latency_mode"),
        Index("ix_stream_settings_updated", "updated_at"),
    )

    # Identity
    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    stream_id: Mapped[int] = mapped_column(
        ForeignKey("live_streams.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )

    # AV basics
    camera_on: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    mic_on: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # Video pipeline
    quality_preset: Mapped[QualityPreset] = mapped_column(
        SQLEnum(QualityPreset, name="stream_quality_preset"),
        default=QualityPreset.balanced,
        nullable=False,
    )
    video_width: Mapped[int] = mapped_column(Integer, default=1280, nullable=False)
    video_height: Mapped[int] = mapped_column(Integer, default=720, nullable=False)
    bitrate_kbps: Mapped[int] = mapped_column(Integer, default=2500, nullable=False)
    framerate_fps: Mapped[int] = mapped_column(Integer, default=30, nullable=False)
    latency_mode: Mapped[LatencyMode] = mapped_column(
        SQLEnum(LatencyMode, name="stream_latency_mode"),
        default=LatencyMode.low,
        nullable=False,
    )
    dvr_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # Recording
    recording_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    recording_quality: Mapped[RecordingQuality] = mapped_column(
        SQLEnum(RecordingQuality, name="stream_recording_quality"),
        default=RecordingQuality.auto,
        nullable=False,
    )

    # Captions / language
    captions_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    language: Mapped[str] = mapped_column(String(12), default="en", nullable=False)

    # Chat controls
    chat_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    chat_mode: Mapped[ChatMode] = mapped_column(
        SQLEnum(ChatMode, name="stream_chat_mode"),
        default=ChatMode.everyone,
        nullable=False,
    )
    slow_mode_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    slow_mode_seconds: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # Visibility / privacy
    visibility: Mapped[Visibility] = mapped_column(
        SQLEnum(Visibility, name="stream_visibility"),
        default=Visibility.public,
        nullable=False,
    )
    geo_allow: Mapped[List[str] | None] = mapped_column(JSON, default=None)  # ISO country codes allow-list
    geo_block: Mapped[List[str] | None] = mapped_column(JSON, default=None)  # ISO country codes block-list
    start_delay_seconds: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # Ingest
    ingest_server: Mapped[Optional[str]] = mapped_column(String(255), default=None)
    stream_key: Mapped[Optional[str]] = mapped_column(String(128), default=None)

    # Monetization toggles (optional integration points)
    superchat_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    min_superchat_minor: Mapped[Optional[int]] = mapped_column(Integer, default=100)

    # Media assets
    thumbnail_url: Mapped[Optional[str]] = mapped_column(String(255), default=None)
    poster_url: Mapped[Optional[str]] = mapped_column(String(255), default=None)

    # Extensibility
    moderation: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON, default=None)  # e.g., {"profanity_filter": true}
    meta: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON, default=None)

    # Timestamps
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    # Relationship
    stream: Mapped["LiveStream"] = relationship(
        "LiveStream", back_populates="settings", lazy="selectin", passive_deletes=True
    )

    # â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€ Helpers â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    def toggle_camera(self, on: Optional[bool] = None) -> None:
        self.camera_on = (not self.camera_on) if on is None else bool(on)

    def toggle_mic(self, on: Optional[bool] = None) -> None:
        self.mic_on = (not self.mic_on) if on is None else bool(on)

    def enable_chat(self, mode: ChatMode | str = ChatMode.everyone) -> None:
        m = ChatMode(mode) if not isinstance(mode, ChatMode) else mode
        if m == ChatMode.disabled:
            raise ValueError("Invalid chat mode for enable_chat")
        self.chat_enabled = True
        self.chat_mode = m

    def disable_chat(self) -> None:
        self.chat_enabled = False
        self.chat_mode = ChatMode.disabled
        self.slow_mode_enabled = False
        self.slow_mode_seconds = 0

    def set_slow_mode(self, seconds: int) -> None:
        if seconds < 0 or seconds > 3600:
            raise ValueError("slow_mode_seconds must be between 0 and 3600")
        self.slow_mode_enabled = seconds > 0
        self.slow_mode_seconds = seconds

    def apply_preset(self, preset: QualityPreset | str) -> None:
        """
        Quick profiles for mobile clients.
        - data_saver:   480p @  900kbps 24fps
        - balanced/auto:720p @ 2500kbps 30fps
        - high_quality: 1080p @ 4500kbps 60fps
        """
        p = QualityPreset(preset) if not isinstance(preset, QualityPreset) else preset
        self.quality_preset = p
        if p == QualityPreset.data_saver:
            self.video_width, self.video_height = 854, 480
            self.bitrate_kbps, self.framerate_fps = 900, 24
        elif p in (QualityPreset.balanced, QualityPreset.auto):
            self.video_width, self.video_height = 1280, 720
            self.bitrate_kbps, self.framerate_fps = 2500, 30
        elif p == QualityPreset.high_quality:
            self.video_width, self.video_height = 1920, 1080
            self.bitrate_kbps, self.framerate_fps = 4500, 60

    def set_latency(self, mode: LatencyMode | str) -> None:
        self.latency_mode = LatencyMode(mode) if not isinstance(mode, LatencyMode) else mode

    def rotate_stream_key(self, length: int = 48) -> str:
        """Generate a new random stream key; returns the plaintext (hash/store if needed)."""
        key = secrets.token_urlsafe(length)
        self.stream_key = key
        return key

    def build_rtmp_url(self) -> Optional[str]:
        """Return RTMP URL if ingest_server and stream_key are configured."""
        if not self.ingest_server or not self.stream_key:
            return None
        base = self.ingest_server.rstrip("/")
        return f"{base}/{self.stream_key}"

    def set_language(self, code: str) -> None:
        """Set language to BCP47-ish lower string, e.g., 'en', 'sw', 'en-US'."""
        value = (code or "en").strip()
        self.language = value.lower()[:12]

    def set_geo_allow(self, countries: List[str] | None) -> None:
        self.geo_allow = self._canon_countries(countries)

    def set_geo_block(self, countries: List[str] | None) -> None:
        self.geo_block = self._canon_countries(countries)

    @staticmethod
    def _canon_countries(lst: List[str] | None) -> List[str] | None:
        if not lst:
            return None
        seen = set()
        out: List[str] = []
        for x in lst:
            if not x:
                continue
            cc = x.strip().upper()
            if len(cc) not in (2, 3):
                continue
            if cc not in seen:
                seen.add(cc)
                out.append(cc)
        return out or None

    def to_public_dict(self) -> Dict[str, Any]:
        """Compact mobile-ready projection (no secrets)."""
        return {
            "stream_id": self.stream_id,
            "camera_on": self.camera_on,
            "mic_on": self.mic_on,
            "quality": {
                "preset": self.quality_preset.value,
                "width": self.video_width,
                "height": self.video_height,
                "bitrate_kbps": self.bitrate_kbps,
                "fps": self.framerate_fps,
                "latency": self.latency_mode.value,
                "dvr": self.dvr_enabled,
            },
            "recording": {
                "enabled": self.recording_enabled,
                "quality": self.recording_quality.value,
            },
            "captions": {"enabled": self.captions_enabled, "language": self.language},
            "chat": {
                "enabled": self.chat_enabled,
                "mode": self.chat_mode.value,
                "slow_mode_seconds": self.slow_mode_seconds if self.slow_mode_enabled else 0,
            },
            "privacy": {
                "visibility": self.visibility.value,
                "geo_allow": self.geo_allow or [],
                "geo_block": self.geo_block or [],
                "start_delay_seconds": self.start_delay_seconds,
            },
            "monetization": {
                "superchat_enabled": self.superchat_enabled,
                "min_superchat_minor": self.min_superchat_minor,
            },
            "assets": {"thumbnail_url": self.thumbnail_url, "poster_url": self.poster_url},
            "ingest": {"server": self.ingest_server, "rtmp_url": self.build_rtmp_url()},
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<StreamSettings stream={self.stream_id} vis={self.visibility.value} "
            f"{self.video_width}x{self.video_height}@{self.framerate_fps}fps {self.bitrate_kbps}kbps>"
        )




