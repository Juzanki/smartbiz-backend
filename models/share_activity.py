# backend/models/share_activity.py
# -*- coding: utf-8 -*-
from __future__ import annotations

"""
ShareActivity:
- Inarekodi tukio la "share" la mtumiaji (copy link, WhatsApp, SMS, email, social, n.k.)
- Generic target (type + id au url kwenye meta.url), UTM params, na client-info
- Dedupe ya haraka kupitia fingerprint (user+target+channel+minute-bucket)
"""

import enum
import hashlib
from typing import Optional, Dict, Any

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
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.ext.hybrid import hybrid_property

from backend.db import Base
from backend.models._types import JSON_VARIANT, as_mutable_json
from backend.models.setting import TimestampMixin  # mixin only (provides created_at/updated_at)

# ───────────────────────────── Enums ───────────────────────────── #

class ShareChannel(str, enum.Enum):
    copy_link = "copy_link"
    whatsapp  = "whatsapp"
    sms       = "sms"
    email     = "email"
    facebook  = "facebook"
    twitter   = "twitter"
    instagram = "instagram"
    tiktok    = "tiktok"
    other     = "other"


class ShareTarget(str, enum.Enum):
    live_stream   = "live_stream"
    post          = "post"
    product       = "product"
    external_link = "external_link"   # tumia url kwenye meta.url


# ───────────────────────────── Model ───────────────────────────── #

class ShareActivity(Base, TimestampMixin):
    """
    Tukio la 'share' lililofanywa na user.
    - user_id: nani alishare
    - target_type + target_id (au meta.url kwa external_link)
    - channel: njia ya kushare
    - message: caption ya hiari
    - meta: JSON huru ({"url":..., "utm": {...}, "ref": "...", "app_version": "...", "ip":"...", "ua":"..."})
    - succeeded: share lilienda?
    - fingerprint: hash ya dedupe (user+target+channel+minute)
    """
    __tablename__ = "share_activity"
    __table_args__ = (
        Index("ix_share_user_created", "user_id", "created_at"),
        Index("ix_share_target", "target_type", "target_id"),
        Index("ix_share_channel", "channel"),
        UniqueConstraint("fingerprint", name="uq_share_fingerprint"),
        CheckConstraint("length(target_id) BETWEEN 0 AND 128", name="ck_share_target_id_len"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    # Nani alishare
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )

    # Alichoshare
    target_type: Mapped[ShareTarget] = mapped_column(
        SQLEnum(ShareTarget, name="share_target", native_enum=False, validate_strings=True),
        nullable=False, index=True
    )
    # Kwa external_link, target_id inaweza kuwa "" (tunaweka url kwenye meta.url)
    target_id: Mapped[str] = mapped_column(String(128), default="", nullable=False)

    # Njia ya kushare
    channel: Mapped[ShareChannel] = mapped_column(
        SQLEnum(ShareChannel, name="share_channel", native_enum=False, validate_strings=True),
        nullable=False, index=True
    )

    # Maelezo ya hiari
    message: Mapped[Optional[str]] = mapped_column(Text)

    # JSON pana (NOTE: attribute ni "meta" ili kuepuka neno lililohifadhiwa "metadata")
    # TUNA-ACHA jina la column DB kuwa "metadata" ili schema ibaki vilevile.
    meta: Mapped[Optional[Dict[str, Any]]] = mapped_column(
        "metadata",  # <<— jina la column kwenye DB
        as_mutable_json(JSON_VARIANT)
    )

    # Telemetry
    succeeded: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # Fingerprint ya dedupe (user+target+channel+minute)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    # Rel: user (module-qualified kuzuia ambiguity/circular import)
    user: Mapped["User"] = relationship(
        "backend.models.user.User",
        foreign_keys=[user_id],
        lazy="selectin",
        passive_deletes=True,
        viewonly=False,
        doc="Mmiliki wa tukio la kushare",
    )

    # ─────────────── Helpers ─────────────── #

    @hybrid_property
    def is_external(self) -> bool:
        return self.target_type == ShareTarget.external_link

    @hybrid_property
    def url(self) -> Optional[str]:
        data = self.meta or {}
        return (data.get("url") or None) if self.is_external else None

    @hybrid_property
    def short_channel(self) -> str:
        return {
            ShareChannel.copy_link: "link",
            ShareChannel.whatsapp:  "wa",
            ShareChannel.sms:       "sms",
            ShareChannel.email:     "mail",
            ShareChannel.facebook:  "fb",
            ShareChannel.twitter:   "x",
            ShareChannel.instagram: "ig",
            ShareChannel.tiktok:    "tt",
        }.get(self.channel, "other")

    def set_meta(self, key: str, value: Any) -> None:
        data = dict(self.meta or {})
        data[key] = value
        self.meta = data

    def set_utm(self, **kwargs: str) -> None:
        """Kuweka UTM haraka: source, medium, campaign, term, content."""
        data = dict(self.meta or {})
        utm = dict(data.get("utm") or {})
        for k, v in kwargs.items():
            if v is not None:
                utm[k] = v
        data["utm"] = utm
        self.meta = data

    def prepare_fingerprint(self) -> None:
        """
        Dedupe ya dakika: user|target|channel|YYYYMMDDHHMM -> SHA1
        (Python-side; rahisi na ya kutosha kwa kuzima duplicates za karibu)
        """
        from datetime import datetime, timezone
        minute_bucket = datetime.now(timezone.utc).strftime("%Y%m%d%H%M")
        key = f"{self.user_id}|{self.target_type.value}|{self.target_id}|{self.channel.value}|{minute_bucket}"
        self.fingerprint = hashlib.sha1(key.encode("utf-8")).hexdigest()

    def __repr__(self) -> str:  # pragma: no cover
        target = self.url or self.target_id or "-"
        return (
            f"<ShareActivity id={self.id} user={self.user_id} "
            f"{self.target_type.value}:{target} via={self.channel.value} ok={self.succeeded}>"
        )

__all__ = ["ShareActivity", "ShareChannel", "ShareTarget"]
