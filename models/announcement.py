# backend/models/announcement.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import enum
import datetime as dt
from typing import Optional, TYPE_CHECKING, Dict, Any, List

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
    func,
    JSON as SA_JSON,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.ext.mutable import MutableList, MutableDict

from backend.db import Base
from backend.models._types import JSON_VARIANT  # PG: JSONB, others: JSON

# ----------------- Cross-DB JSON / ARRAY (PG) -----------------
# - Postgres:  meta => JSONB (JSON_VARIANT), tags => ARRAY(String(48))
# - Others:    meta => JSON,  tags => JSON (list[str])
try:
    from sqlalchemy.dialects.postgresql import ARRAY as PG_ARRAY  # type: ignore
    TAGS_VARIANT = SA_JSON().with_variant(PG_ARRAY(String(48)), "postgresql")
except Exception:  # pragma: no cover
    TAGS_VARIANT = SA_JSON()

if TYPE_CHECKING:
    from .user import User


class AnnouncementStatus(str, enum.Enum):
    draft = "draft"
    scheduled = "scheduled"
    published = "published"
    archived = "archived"


class AnnouncementPriority(str, enum.Enum):
    low = "low"
    normal = "normal"
    high = "high"
    urgent = "urgent"


class AnnouncementAudience(str, enum.Enum):
    all = "all"
    followers = "followers"
    premium = "premium"
    region = "region"
    role = "role"


class AnnouncementChannel(str, enum.Enum):
    system = "system"
    live_room = "live_room"
    product = "product"
    marketing = "marketing"
    other = "other"


class Announcement(Base):
    __tablename__ = "announcements"
    __mapper_args__ = {"eager_defaults": True}

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    # Identity & content
    slug: Mapped[str] = mapped_column(String(160), unique=True, index=True)
    title: Mapped[str] = mapped_column(String(140), nullable=False, index=True)
    summary: Mapped[Optional[str]] = mapped_column(String(280))
    content: Mapped[str] = mapped_column(Text, nullable=False)

    # Classifications
    status: Mapped[AnnouncementStatus] = mapped_column(
        SQLEnum(AnnouncementStatus, name="announcement_status", native_enum=False, validate_strings=True),
        default=AnnouncementStatus.draft,
        index=True,
        nullable=False,
    )
    priority: Mapped[AnnouncementPriority] = mapped_column(
        SQLEnum(AnnouncementPriority, name="announcement_priority", native_enum=False, validate_strings=True),
        default=AnnouncementPriority.normal,
        index=True,
        nullable=False,
    )
    audience: Mapped[AnnouncementAudience] = mapped_column(
        SQLEnum(AnnouncementAudience, name="announcement_audience", native_enum=False, validate_strings=True),
        default=AnnouncementAudience.all,
        index=True,
        nullable=False,
    )
    channel: Mapped[AnnouncementChannel] = mapped_column(
        SQLEnum(AnnouncementChannel, name="announcement_channel", native_enum=False, validate_strings=True),
        default=AnnouncementChannel.system,
        index=True,
        nullable=False,
    )

    # Targeting
    #  - PG: ARRAY(VARCHAR(48))
    #  - Others: JSON list[str]
    tags: Mapped[Optional[List[str]]] = mapped_column(
        MutableList.as_mutable(TAGS_VARIANT)
    )

    # Arbitrary metadata / i18n / CTA buttons, etc.
    meta: Mapped[Optional[Dict[str, Any]]] = mapped_column(
        MutableDict.as_mutable(JSON_VARIANT)
    )

    # Scheduling & lifecycle
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    published_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True), index=True)
    scheduled_for: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True), index=True)
    expires_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True), index=True)

    # Soft delete
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False, index=True, nullable=False)
    deleted_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))

    # Authorship / Audit
    created_by_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    updated_by_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True
    )

    created_by: Mapped[Optional["User"]] = relationship(
        "User", foreign_keys=[created_by_id], backref="announcements_created", lazy="selectin"
    )
    updated_by: Mapped[Optional["User"]] = relationship(
        "User", foreign_keys=[updated_by_id], backref="announcements_updated", lazy="selectin"
    )

    # -------------------- Derivatives --------------------
    @hybrid_property
    def is_published(self) -> bool:
        return self.status == AnnouncementStatus.published and not self.is_deleted

    @hybrid_property
    def is_active_now(self) -> bool:
        if self.is_deleted:
            return False
        now = dt.datetime.now(dt.timezone.utc)
        if self.status == AnnouncementStatus.published:
            if self.expires_at and now >= self.expires_at:
                return False
            return True
        if self.status == AnnouncementStatus.scheduled:
            if self.scheduled_for and self.scheduled_for <= now:
                return not self.expires_at or now < self.expires_at
        return False

    # -------------------- Actions / Helpers --------------------
    def publish(self) -> None:
        now = dt.datetime.now(dt.timezone.utc)
        self.status = AnnouncementStatus.published
        self.published_at = now
        # kama expiry ilikuwa nyuma ya sasa, iondoe
        if self.expires_at and self.expires_at <= now:
            self.expires_at = None
        # mara tu inapochapishwa, schedule inapoteza maana
        self.scheduled_for = None

    def schedule_for_time(self, when: dt.datetime) -> None:
        """Weka ratiba ya kuchapisha baadaye (UTC-aware recommended)."""
        self.status = AnnouncementStatus.scheduled
        self.scheduled_for = when
        # kuanzia sasa haijachapishwa bado
        self.published_at = None

    def cancel_schedule(self) -> None:
        """Ondoa ratiba na rudisha kuwa rasimu."""
        self.status = AnnouncementStatus.draft
        self.scheduled_for = None

    def archive(self) -> None:
        self.status = AnnouncementStatus.archived

    def unpublish(self) -> None:
        """Rudisha kutoka 'published' kwenda 'archived' bila kufuta content."""
        self.status = AnnouncementStatus.archived
        self.published_at = None

    def set_expiry(self, when: Optional[dt.datetime]) -> None:
        """Weka/ondoa expiry kwa tangazo."""
        self.expires_at = when

    def soft_delete(self) -> None:
        self.is_deleted = True
        self.deleted_at = dt.datetime.now(dt.timezone.utc)

    def add_tags(self, *items: str) -> None:
        s = {x.strip().lower() for x in (self.tags or []) if x}
        for it in items or []:
            it = (it or "").strip().lower()[:48]
            if it:
                s.add(it)
        self.tags = sorted(s) or None

    def set_tags(self, items: list[str] | tuple[str, ...]) -> None:
        s = {(x or "").strip().lower()[:48] for x in (items or []) if x}
        self.tags = sorted(s) or None

    def retarget(self, *, audience: AnnouncementAudience | str, tags: Optional[list[str]] = None) -> None:
        self.audience = AnnouncementAudience(str(audience))
        if tags is not None:
            self.set_tags(tags)

    def merge_meta(self, **kv: Any) -> None:
        cur = dict(self.meta or {})
        cur.update(kv)
        self.meta = cur

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Announcement id={self.id} slug={self.slug!r} status={self.status}>"

    # -------------------- Indexing & Constraints --------------------
    __table_args__ = (
        # len/format guards
        CheckConstraint("length(trim(title)) >= 3", name="ck_annc_title_len_min"),
        CheckConstraint("length(trim(slug)) >= 3", name="ck_annc_slug_len_min"),
        # chronology
        CheckConstraint(
            "(expires_at IS NULL) OR (published_at IS NULL) OR (expires_at > published_at)",
            name="ck_annc_expires_after_publish",
        ),
        CheckConstraint(
            "(expires_at IS NULL) OR (scheduled_for IS NULL) OR (expires_at > scheduled_for)",
            name="ck_annc_expires_after_schedule",
        ),
        # handy composite indexes
        Index("ix_annc_status_schedule", "status", "scheduled_for"),
        Index("ix_annc_channel_priority", "channel", "priority"),
        Index("ix_annc_audience_created", "audience", "created_at"),
    )


# -------------------- Normalizers / Guards --------------------
from sqlalchemy.event import listens_for  # isafishe import order kwa mypy

@listens_for(Announcement, "before_insert")
def _ann_before_insert(_m, _c, t: Announcement) -> None:
    if t.slug:
        t.slug = t.slug.strip().lower()[:160]
    if t.title:
        t.title = t.title.strip()[:140]
    if t.summary:
        t.summary = t.summary.strip()[:280]
    # normalize tags
    if t.tags:
        t.tags = sorted({(x or "").strip().lower()[:48] for x in t.tags if x}) or None


@listens_for(Announcement, "before_update")
def _ann_before_update(_m, _c, t: Announcement) -> None:
    if t.slug:
        t.slug = t.slug.strip().lower()[:160]
    if t.title:
        t.title = t.title.strip()[:140]
    if t.summary:
        t.summary = t.summary.strip()[:280]
    if t.tags:
        t.tags = sorted({(x or "").strip().lower()[:48] for x in t.tags if x}) or None
