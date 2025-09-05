# backend/models/replay_activity_log.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import enum
import datetime as dt
from typing import Optional, TYPE_CHECKING, Dict, Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum as SQLEnum,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
    text,
    JSON as SA_JSON,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship, validates
from sqlalchemy.ext.hybrid import hybrid_property

from backend.db import Base
from backend.models._types import JSON_VARIANT, as_mutable_json

# -------- Portable JSON (JSON_VARIANT on Postgres, JSON elsewhere) --------
try:
    pass
except Exception:  # pragma: no cover
    # patched: use shared JSON_VARIANT
    pass

if TYPE_CHECKING:
    from .user import User
    from .video_post import VideoPost

def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)

class ReplayAction(str, enum.Enum):
    share = "share"
    download = "download"

class SharePlatform(str, enum.Enum):
    whatsapp  = "whatsapp"
    facebook  = "facebook"
    instagram = "instagram"
    telegram  = "telegram"
    tiktok    = "tiktok"
    twitter   = "twitter"
    sms       = "sms"
    email     = "email"
    other     = "other"

class ReplayActivityLog(Base):
    """
    Kumbukumbu za matendo ya 'replay' dhidi ya VideoPost:
      - action (share/download) + platform
      - idempotency (kuzuia marudio)
      - muktadha (destination/UA/IP/nchi, kifaa, referrer)
      - payload ya kushare (mf. message, media refs) na short_url
    """
    __tablename__ = "replay_activity_logs"
    __mapper_args__ = {"eager_defaults": True}

    # --- Identity ---
    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)

    # --- Targets ---
    video_post_id: Mapped[int] = mapped_column(
        ForeignKey("video_posts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        doc="VideoPost inayohusika",
    )
    user_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        doc="Mtumiaji aliyechochea tukio (kama yupo)",
    )

    # --- Tukio ---
    action: Mapped[ReplayAction] = mapped_column(
        SQLEnum(ReplayAction, name="replay_action", native_enum=False, validate_strings=True),
        nullable=False,
        index=True,
    )
    platform: Mapped[Optional[SharePlatform]] = mapped_column(
        SQLEnum(SharePlatform, name="replay_platform", native_enum=False, validate_strings=True),
        nullable=True,
        index=True,
    )
    destination: Mapped[Optional[str]] = mapped_column(String(160))  # group/channel/phone/email
    recipient_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default=text("0"),
        doc="Idadi ya walengwa, kama share ilikuwa kwa kundi/daftari",
    )

    # --- Client context ---
    user_agent:  Mapped[Optional[str]] = mapped_column(String(400))
    ip_address:  Mapped[Optional[str]] = mapped_column(String(64))
    country:     Mapped[Optional[str]] = mapped_column(String(2))  # ISO-3166 alpha-2
    device_id:   Mapped[Optional[str]] = mapped_column(String(120), index=True)
    device_type: Mapped[Optional[str]] = mapped_column(String(80))   # mfano "Android-13/Chrome"
    app_version: Mapped[Optional[str]] = mapped_column(String(40))

    # --- URLs / payload ---
    referrer_url:  Mapped[Optional[str]] = mapped_column(String(1024))
    short_url:     Mapped[Optional[str]] = mapped_column(String(512))
    share_payload: Mapped[Optional[Dict[str, Any]]] = mapped_column(as_mutable_json(JSON_VARIANT))
    # mfano: {"text":"..", "media":[{"type":"image","url":".."}]}

    # --- Idempotency / tracing ---
    idempotency_key: Mapped[Optional[str]] = mapped_column(String(120), unique=True, index=True)
    request_id:      Mapped[Optional[str]] = mapped_column(String(64), index=True)

    # --- Metadata ya ziada ---
    meta: Mapped[Optional[Dict[str, Any]]] = mapped_column(as_mutable_json(JSON_VARIANT))

    # --- Times ---
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )

    # --- Relationships ---
    user: Mapped[Optional["User"]] = relationship(
        "User",
        lazy="selectin",
        passive_deletes=True,
    )
    # MUHIMU: linganisha na VideoPost.replay_activity_logs
    video_post: Mapped["VideoPost"] = relationship(
        "VideoPost",
        back_populates="replay_activity_logs",
        lazy="selectin",
        passive_deletes=True,
    )

    # ---------- Hybrids ----------
    @hybrid_property
    def is_share(self) -> bool:
        return self.action == ReplayAction.share

    @hybrid_property
    def is_download(self) -> bool:
        return self.action == ReplayAction.download

    @hybrid_property
    def anonymous(self) -> bool:
        return self.user_id is None

    # ---------- Validators ----------
    @validates("destination", "user_agent", "ip_address", "device_id", "device_type",
               "app_version", "referrer_url", "short_url", "idempotency_key", "request_id")
    def _trim(self, _k: str, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        s = v.strip()
        return s or None

    @validates("country")
    def _country_iso2(self, _k: str, v: Optional[str]) -> Optional[str]:
        if not v:
            return None
        v = v.strip().upper()
        return v[:2] if len(v) >= 2 else None

    # ---------- Helpers ----------
    def touch_idempotency(self, key: str) -> None:
        """Weka ufunguo wa idempotency (kuzuia marudio)."""
        self.idempotency_key = (key or "").strip() or None

    def set_context(
        self,
        *,
        user_agent: Optional[str] = None,
        ip_address: Optional[str] = None,
        country: Optional[str] = None,
        device_id: Optional[str] = None,
        device_type: Optional[str] = None,
        app_version: Optional[str] = None,
        referrer_url: Optional[str] = None,
    ) -> None:
        if user_agent is not None:  self.user_agent = user_agent
        if ip_address is not None:  self.ip_address = ip_address
        if country is not None:     self.country = country
        if device_id is not None:   self.device_id = device_id
        if device_type is not None: self.device_type = device_type
        if app_version is not None: self.app_version = app_version
        if referrer_url is not None:self.referrer_url = referrer_url

    def bump_recipients(self, n: int = 1) -> None:
        self.recipient_count = max(0, (self.recipient_count or 0) + max(0, int(n)))

    def record_share(
        self,
        *,
        platform: Optional[SharePlatform],
        destination: Optional[str] = None,
        recipients: int = 1,
        short_url: Optional[str] = None,
        payload: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.action = ReplayAction.share
        self.platform = platform
        if destination is not None:
            self.destination = destination
        if short_url is not None:
            self.short_url = short_url
        if payload is not None:
            self.share_payload = {**(self.share_payload or {}), **payload}
        self.bump_recipients(recipients)

    def record_download(self) -> None:
        self.action = ReplayAction.download
        # kwa download mara nyingi hakuna platform/destination
        self.platform = self.platform or None
        self.destination = self.destination or None

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<ReplayActivityLog id={self.id} video={self.video_post_id} user={self.user_id} "
            f"{self.action} {self.platform}>"
        )

    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_replay_idem"),
        CheckConstraint("recipient_count >= 0", name="ck_replay_recipient_nonneg"),
        CheckConstraint("country IS NULL OR length(country) = 2", name="ck_replay_country_iso2"),
        Index("ix_replay_video_created", "video_post_id", "created_at"),
        Index("ix_replay_user_created", "user_id", "created_at"),
        Index("ix_replay_action_platform", "action", "platform"),
        Index("ix_replay_country", "country"),
    )
