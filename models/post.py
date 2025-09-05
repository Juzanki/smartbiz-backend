# backend/models/post.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import datetime as dt
import enum
import math
import re
from typing import TYPE_CHECKING, List, Optional, Dict, Any

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
    text,
    func,
)
from sqlalchemy.event import listens_for
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.orm import Mapped, mapped_column, relationship, validates

from backend.db import Base
from backend.models._types import JSON_VARIANT, as_mutable_json

if TYPE_CHECKING:
    from .user import User

# --------- Enums ---------
class PostStatus(str, enum.Enum):
    draft     = "draft"
    scheduled = "scheduled"
    published = "published"
    archived  = "archived"


class Visibility(str, enum.Enum):
    public   = "public"
    unlisted = "unlisted"
    private  = "private"


class SocialPlatform(str, enum.Enum):
    telegram  = "telegram"
    whatsapp  = "whatsapp"
    twitter   = "twitter"
    facebook  = "facebook"
    instagram = "instagram"
    tiktok    = "tiktok"
    youtube   = "youtube"
    other     = "other"


class SocialStatus(str, enum.Enum):
    draft     = "draft"
    queued    = "queued"
    scheduled = "scheduled"
    posted    = "posted"
    failed    = "failed"
    canceled  = "canceled"


# --------- Utils ---------
_slug_re = re.compile(r"[^a-z0-9]+")


def _slugify(s: str, *, max_len: int = 120) -> str:
    s = (s or "").strip().lower()
    s = _slug_re.sub("-", s).strip("-")
    return s[:max_len] or "post"


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


# --------- Post ---------
class Post(Base):
    """
    Post ya ndani (blog/article/announcement):
    - Unique slug per owner
    - SEO/meta + canonical_url + cover_image
    - Lifecycle (draft/scheduled/published/archived) + guards
    - Visibility (public/unlisted/private)
    - Metrics (views/likes/shares/comments) + pin/feature
    - Idempotency & request correlation kwa dedupe
    """
    __tablename__ = "posts"
    __mapper_args__ = {"eager_defaults": True}

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)

    # Owner
    owner_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True, nullable=True
    )
    owner: Mapped[Optional["User"]] = relationship(
        "User", lazy="selectin", foreign_keys=[owner_id], passive_deletes=True
    )

    # Identity
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    slug: Mapped[Optional[str]] = mapped_column(String(140), index=True)

    # Content & presentation
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    excerpt: Mapped[Optional[str]] = mapped_column(String(320))
    reading_minutes: Mapped[Optional[int]] = mapped_column(Integer)  # ETA ~200wpm
    lang: Mapped[Optional[str]] = mapped_column(String(8), index=True)
    cover_image_url: Mapped[Optional[str]] = mapped_column(String(512))
    canonical_url: Mapped[Optional[str]] = mapped_column(String(512), index=True)

    # Classification
    status: Mapped[PostStatus] = mapped_column(
        SQLEnum(PostStatus, name="post_status", native_enum=False, validate_strings=True),
        default=PostStatus.published, nullable=False, index=True,
    )
    visibility: Mapped[Visibility] = mapped_column(
        SQLEnum(Visibility, name="post_visibility", native_enum=False, validate_strings=True),
        default=Visibility.public, nullable=False, index=True,
    )

    # Metadata (mutable JSON for in-place diffs)
    tags: Mapped[Optional[List[str]]] = mapped_column(as_mutable_json(JSON_VARIANT))
    attachments: Mapped[Optional[List[Dict[str, Any]]]] = mapped_column(as_mutable_json(JSON_VARIANT))  # [{"type":"image","url":"..."}]
    meta: Mapped[Optional[Dict[str, Any]]] = mapped_column(as_mutable_json(JSON_VARIANT))  # {"seo_title":"...","og_image":"..."}

    # Moderation / flags
    is_published_flag: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, index=True)
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)
    is_pinned: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)
    is_featured: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)
    moderation_reason: Mapped[Optional[str]] = mapped_column(String(200))

    # Metrics (denormalized for fast reads)
    views_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    likes_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    shares_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    comments_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))

    # Dedupe/correlation
    idempotency_key: Mapped[Optional[str]] = mapped_column(String(120), unique=True, index=True)
    request_id: Mapped[Optional[str]] = mapped_column(String(64), index=True)

    # Times
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    published_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True), index=True)
    scheduled_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True), index=True)
    deleted_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))

    # Relations
    social_posts: Mapped[List["SocialMediaPost"]] = relationship(
        "SocialMediaPost",
        back_populates="source_post",
        cascade="all, delete-orphan",
        passive_deletes=True,
        lazy="selectin",
    )

    # ---------- Indexes / Guards ----------
    __table_args__ = (
        UniqueConstraint("owner_id", "slug", name="uq_posts_owner_slug"),
        Index("ix_posts_owner_created", "owner_id", "created_at"),
        Index("ix_posts_status_pub_at", "status", "published_at"),
        Index("ix_posts_visibility", "visibility"),
        Index("ix_posts_pinned_featured", "is_pinned", "is_featured"),
        CheckConstraint("length(title) >= 2", name="ck_posts_title_len"),
        CheckConstraint("(views_count >= 0) AND (likes_count >= 0) AND (shares_count >= 0) AND (comments_count >= 0)",
                        name="ck_posts_counts_nonneg"),
        CheckConstraint(
            "(published_at IS NULL) OR (published_at >= created_at)",
            name="ck_posts_published_after_created",
        ),
        CheckConstraint(
            "(scheduled_at IS NULL) OR (status = 'scheduled')",
            name="ck_posts_schedule_requires_status",
        ),
    )

    # ---------- Hybrids ----------
    @hybrid_property
    def is_published(self) -> bool:
        return self.status == PostStatus.published and not self.is_deleted

    @hybrid_property
    def is_visible(self) -> bool:
        """Visible to public feeds? (public & published & not deleted)."""
        return self.visibility == Visibility.public and self.is_published

    @hybrid_property
    def permalink_path(self) -> Optional[str]:
        if not self.slug:
            return None
        # mfano wa path; UI/routers zinaweza kujenga URL kikamilifu
        return f"/u/{self.owner_id}/posts/{self.slug}" if self.owner_id else f"/posts/{self.slug}"

    @hybrid_property
    def can_publish_now(self) -> bool:
        return (not self.is_deleted) and self.status in (PostStatus.draft, PostStatus.scheduled)

    # ---------- Domain helpers ----------
    def publish(self) -> None:
        if self.is_deleted:
            return
        self.status = PostStatus.published
        self.is_published_flag = True
        self.published_at = self.published_at or _utcnow()
        self.scheduled_at = None

    def unpublish(self, *, reason: Optional[str] = None) -> None:
        self.status = PostStatus.draft
        self.is_published_flag = False
        self.published_at = None
        if reason:
            self.moderation_reason = reason

    def schedule(self, when: dt.datetime) -> None:
        self.status = PostStatus.scheduled
        self.is_published_flag = False
        self.scheduled_at = when

    def archive(self) -> None:
        self.status = PostStatus.archived
        self.is_published_flag = False

    def soft_delete(self, *, reason: Optional[str] = None) -> None:
        self.is_deleted = True
        self.deleted_at = _utcnow()
        if reason:
            self.moderation_reason = reason

    # Metrics helpers
    def bump_views(self, n: int = 1) -> None:
        self.views_count = max(0, (self.views_count or 0) + max(0, int(n)))

    def add_like(self, n: int = 1) -> None:
        self.likes_count = max(0, (self.likes_count or 0) + max(0, int(n)))

    def add_share(self, n: int = 1) -> None:
        self.shares_count = max(0, (self.shares_count or 0) + max(0, int(n)))

    def add_comment(self, n: int = 1) -> None:
        self.comments_count = max(0, (self.comments_count or 0) + max(0, int(n)))

    # ---------- Validation ----------
    @validates("slug")
    def _normalize_slug(self, _k: str, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        return _slugify(v)

    @validates("title", "canonical_url", "cover_image_url", "idempotency_key", "request_id", "moderation_reason")
    def _trim_texts(self, _k: str, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        s = v.strip()
        return s or None

    # ---------- Repr ----------
    def __repr__(self) -> str:  # pragma: no cover
        return f"<Post id={self.id} owner={self.owner_id} status={self.status} slug={self.slug!r}>"


# --------- SocialMediaPost ---------
class SocialMediaPost(Base):
    """
    Rekodi ya post ya mitandao ya kijamii (cross-post/scheduler):
    - Links: user + chanzo (Post)
    - Lifecycle: draft/queued/scheduled/posted/failed/canceled
    - Idempotency, request_id, na error details
    - Media/tags/link + external IDs/URLs
    """
    __tablename__ = "social_posts"
    __mapper_args__ = {"eager_defaults": True}

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)

    user_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
    )
    # endelea kutumia back_populates="posts" endapo User ina hilo
    user: Mapped[Optional["User"]] = relationship("User", back_populates="posts", lazy="selectin", foreign_keys=[user_id])

    # Chanzo cha ndani (optional)
    post_id: Mapped[Optional[int]] = mapped_column(ForeignKey("posts.id", ondelete="SET NULL"), index=True)
    source_post: Mapped[Optional["Post"]] = relationship("Post", back_populates="social_posts", lazy="selectin")

    platform: Mapped[SocialPlatform] = mapped_column(
        SQLEnum(SocialPlatform, name="social_platform", native_enum=False, validate_strings=True),
        nullable=False, index=True,
    )
    status: Mapped[SocialStatus] = mapped_column(
        SQLEnum(SocialStatus, name="social_status", native_enum=False, validate_strings=True),
        default=SocialStatus.draft, nullable=False, index=True,
    )

    content: Mapped[str] = mapped_column(Text, nullable=False)
    media: Mapped[Optional[List[Dict[str, Any]]]] = mapped_column(as_mutable_json(JSON_VARIANT))
    tags: Mapped[Optional[List[str]]] = mapped_column(as_mutable_json(JSON_VARIANT))
    link_url: Mapped[Optional[str]] = mapped_column(String(512))

    # External references
    external_post_id: Mapped[Optional[str]] = mapped_column(String(160), index=True)
    external_url: Mapped[Optional[str]] = mapped_column(String(512))
    idempotency_key: Mapped[Optional[str]] = mapped_column(String(120), unique=True, index=True)
    request_id: Mapped[Optional[str]] = mapped_column(String(64), index=True)

    # Errors
    error_code: Mapped[Optional[str]] = mapped_column(String(80), index=True)
    error_detail: Mapped[Optional[str]] = mapped_column(Text)

    # Times
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    scheduled_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True), index=True)
    posted_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    failed_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    canceled_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))

    meta: Mapped[Optional[Dict[str, Any]]] = mapped_column(as_mutable_json(JSON_VARIANT))

    # Indexes / Guards
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_social_posts_idem"),
        UniqueConstraint("user_id", "platform", "external_post_id", name="uq_social_ext_per_platform"),
        Index("ix_soc_posts_user_created", "user_id", "created_at"),
        Index("ix_soc_posts_status_sched", "status", "scheduled_at"),
        Index("ix_soc_posts_platform_status", "platform", "status"),
        CheckConstraint("length(content) >= 1", name="ck_social_content_len"),
    )

    # ---------- Helpers ----------
    def queue(self, when: Optional[dt.datetime] = None) -> None:
        self.status = SocialStatus.scheduled if when else SocialStatus.queued
        self.scheduled_at = when

    def mark_posted(self, *, external_id: Optional[str] = None, url: Optional[str] = None) -> None:
        self.status = SocialStatus.posted
        self.posted_at = _utcnow()
        if external_id:
            self.external_post_id = external_id
        if url:
            self.external_url = url
        self.error_code = None
        self.error_detail = None

    def mark_failed(self, *, code: Optional[str] = None, detail: Optional[str] = None) -> None:
        self.status = SocialStatus.failed
        self.failed_at = _utcnow()
        self.error_code = code
        self.error_detail = detail

    def cancel(self) -> None:
        self.status = SocialStatus.canceled
        self.canceled_at = _utcnow()

    def __repr__(self) -> str:  # pragma: no cover
        return f"<SocialMediaPost id={self.id} platform={self.platform} status={self.status} user={self.user_id}>"


# --------- Event listeners (auto-fill & normalize) ---------
@listens_for(Post, "before_insert")
def _post_before_insert(_m, _c, t: Post) -> None:  # pragma: no cover
    # slug
    if not t.slug and t.title:
        t.slug = _slugify(t.title)

    # excerpt
    if t.content and not t.excerpt:
        txt = re.sub(r"\s+", " ", t.content).strip()
        t.excerpt = (txt[:277] + "…") if len(txt) > 280 else txt

    # reading minutes
    if t.content and not t.reading_minutes:
        words = len(re.findall(r"\w+", t.content))
        t.reading_minutes = max(1, math.ceil(words / 200.0))

    # publish flag sync
    if t.status == PostStatus.published:
        t.is_published_flag = True
        t.published_at = t.published_at or _utcnow()
        t.scheduled_at = None
    else:
        t.is_published_flag = False


@listens_for(Post, "before_update")
def _post_before_update(_m, _c, t: Post) -> None:  # pragma: no cover
    # slug
    if t.slug is None and t.title:
        t.slug = _slugify(t.title)

    # excerpt
    if (t.excerpt is None) and t.content:
        txt = re.sub(r"\s+", " ", t.content).strip()
        t.excerpt = (txt[:277] + "…") if len(txt) > 280 else txt

    # reading minutes
    if (t.reading_minutes is None) and t.content:
        words = len(re.findall(r"\w+", t.content))
        t.reading_minutes = max(1, math.ceil(words / 200.0))

    # publish flag sync
    if t.status == PostStatus.published:
        t.is_published_flag = True
        t.published_at = t.published_at or _utcnow()
        t.scheduled_at = None
    else:
        t.is_published_flag = False
