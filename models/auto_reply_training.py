# backend/models/auto_reply_training.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import enum
import datetime as dt
from typing import Optional, Iterable, Dict, Any, TYPE_CHECKING, List

from sqlalchemy import (
    Enum as SQLEnum,
    Integer,
    String,
    Text,
    DateTime,
    ForeignKey,
    Index,
    CheckConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship, validates, Query, Session
from sqlalchemy.event import listens_for
from sqlalchemy.ext.mutable import MutableList, MutableDict

from backend.db import Base
from backend.models._types import JSON_VARIANT  # PG: JSONB, others: JSON

if TYPE_CHECKING:
    from .user import User


# ---------------- Enums ----------------
class ReplyPlatform(str, enum.Enum):
    all = "all"
    whatsapp = "whatsapp"
    telegram = "telegram"
    sms = "sms"
    web = "web"  # ongeza kadri ya mahitaji


class TrainingSource(str, enum.Enum):
    manual = "manual"          # added by user/admin
    imported = "imported"      # seeded from external data
    ai_suggested = "ai_suggested"  # created from ML suggestion


# ---------------- Model ----------------
class AutoReplyTraining(Base):
    """
    Per-user keyword → reply training for auto-responder.

    • Uniqueness (portable): (user_id, lower(keyword), platform)
    • Normalization: keyword ni lowercase + whitespace collapsed
    • Ufuatiliaji: hits, last_used_at, feedback (up/down), confidence (0..100)
    • Urahisi: synonyms/tags (Mutable JSON), language, enabled/cooldown/throttle
    """
    __tablename__ = "auto_reply_training"
    __mapper_args__ = {"eager_defaults": True}

    # ── Keys
    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # ── Data
    keyword: Mapped[str] = mapped_column(String(100), nullable=False)  # normalized (lower/space-collapsed)
    reply:   Mapped[str] = mapped_column(Text, nullable=False)

    platform: Mapped[ReplyPlatform] = mapped_column(
        SQLEnum(ReplyPlatform, name="reply_platform_enum", native_enum=False, validate_strings=True),
        default=ReplyPlatform.all,
        nullable=False,
        index=True,
    )

    # Language hint (ISO like "en", "sw", "en-US")
    language: Mapped[Optional[str]] = mapped_column(String(12))

    # Controls
    enabled: Mapped[bool] = mapped_column(default=True, nullable=False)
    cooldown_sec: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))  # min 0
    throttle_per_min: Mapped[Optional[int]] = mapped_column(Integer)  # NULL=unlimited

    # Learning & ranking
    confidence: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("50"))  # 0..100
    hits: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    upvotes: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    downvotes: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))

    # Usage
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    last_used_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True), index=True)

    # Extras (portable & mutable)
    synonyms: Mapped[Optional[List[str]]] = mapped_column(MutableList.as_mutable(JSON_VARIANT))
    tags:     Mapped[Optional[List[str]]] = mapped_column(MutableList.as_mutable(JSON_VARIANT))
    meta:     Mapped[Optional[Dict[str, Any]]] = mapped_column(MutableDict.as_mutable(JSON_VARIANT))

    # ── Relationship (no need to edit user.py)
    user: Mapped["User"] = relationship(
        "User",
        backref="auto_reply_training",              # ← auto-creates User.auto_reply_training
        foreign_keys=lambda: [AutoReplyTraining.user_id],
        passive_deletes=True,
        lazy="selectin",
    )

    # ── Constraints & Indexes
    __table_args__ = (
        # Uniqueness per (user, normalized keyword, platform)
        Index(
            "uq_auto_reply_training_user_keyword_platform",
            "user_id",
            func.lower(keyword),  # type: ignore[name-defined]
            "platform",
            unique=True,
        ),
        Index("ix_auto_reply_training_user_platform", "user_id", "platform"),
        Index("ix_auto_reply_training_keyword", func.lower(keyword)),
        Index("ix_auto_reply_training_enabled_conf", "enabled", "confidence"),
        CheckConstraint("length(trim(keyword)) >= 1", name="ck_art_keyword_len_min"),
        CheckConstraint("length(trim(reply)) >= 1",   name="ck_art_reply_len_min"),
        CheckConstraint("confidence >= 0 AND confidence <= 100", name="ck_art_conf_bounds"),
        CheckConstraint("cooldown_sec >= 0", name="ck_art_cooldown_nonneg"),
        CheckConstraint("throttle_per_min IS NULL OR throttle_per_min >= 0", name="ck_art_throttle_nonneg"),
    )

    # ── Normalization helpers ────────────────────────────────────────────────────
    @staticmethod
    def _norm_keyword(value: str) -> str:
        v = (value or "").strip()
        v = " ".join(v.split())
        return v.lower()

    @staticmethod
    def _norm_list(items: Iterable[str] | None, *, max_len: int, lower: bool = True) -> list[str]:
        out = []
        if items:
            for it in items:
                if not it:
                    continue
                t = str(it).strip()
                if lower:
                    t = t.lower()
                if not t:
                    continue
                out.append(t[:max_len])
        # unique + stable order
        return sorted(set(out))

    @validates("keyword")
    def _normalize_keyword(self, _key: str, value: str) -> str:
        return self._norm_keyword(value)

    # ── Learning / runtime helpers ───────────────────────────────────────────────
    def touch_use(self) -> None:
        """Mark usage (hit counter + timestamp)."""
        self.hits = (self.hits or 0) + 1
        self.last_used_at = dt.datetime.now(dt.timezone.utc)

    def vote(self, *, up: bool) -> None:
        """Record user feedback and nudge confidence."""
        if up:
            self.upvotes += 1
            self.confidence = min(100, (self.confidence or 0) + 2)
        else:
            self.downvotes += 1
            self.confidence = max(0, (self.confidence or 0) - 3)

    def set_reply(self, text: str) -> None:
        self.reply = (text or "").strip()

    def set_synonyms(self, items: Iterable[str]) -> None:
        self.synonyms = self._norm_list(items, max_len=100)

    def add_synonyms(self, items: Iterable[str]) -> None:
        current = self.synonyms or []
        self.synonyms = self._norm_list([*current, *(items or [])], max_len=100)

    def set_tags(self, items: Iterable[str]) -> None:
        self.tags = self._norm_list(items, max_len=32)

    def enable(self) -> None:
        self.enabled = True

    def disable(self) -> None:
        self.enabled = False

    # ── Matching helpers ────────────────────────────────────────────────────────
    @classmethod
    def _kw(cls, s: str) -> str:
        return cls._norm_keyword(s)

    @classmethod
    def match_for(
        cls,
        q: Query,  # Session.query(cls) au select(cls) wrapper
        *,
        user_id: int,
        keyword: str,
        platform: ReplyPlatform | str = ReplyPlatform.all,
        language: Optional[str] = None,
        require_enabled: bool = True,
    ):
        """
        Tafuta rules zinazolingana:
        1) Platform-specific, kisha fallback `all`
        2) Keyword normalized (lower/space-collapsed)
        3) (Hiari) language kama filter nyepesi
        4) (Hiari) require_enabled
        Matokeo yanaweza kupangiliwa kwa kipaumbele: platform match > confidence > last_used_at desc.
        """
        kw = cls._kw(keyword)
        plat = ReplyPlatform(str(platform))
        base = q.filter(cls.user_id == user_id, func.lower(cls.keyword) == kw)

        if require_enabled:
            base = base.filter(cls.enabled.is_(True))

        if language:
            # andika filter nyepesi ya lugha; kama huna data ya lugha, haizuii
            base = base.filter((cls.language == language) | (cls.language.is_(None)))

        return (
            base.filter(cls.platform.in_([plat, ReplyPlatform.all]))
                .order_by(
                    # prioritize platform-specific over "all"
                    func.case((cls.platform == plat, 1), else_=0).desc(),
                    cls.confidence.desc(),
                    cls.last_used_at.desc().nullslast(),
                )
        )

    # QoL: upsert ndani ya session
    @classmethod
    def upsert(
        cls,
        session: Session,
        *,
        user_id: int,
        keyword: str,
        reply: str,
        platform: ReplyPlatform | str = ReplyPlatform.all,
        language: Optional[str] = None,
        source: TrainingSource = TrainingSource.manual,
        synonyms: Iterable[str] | None = None,
        tags: Iterable[str] | None = None,
    ) -> "AutoReplyTraining":
        """
        Upsert rahisi kwa (user_id, keyword, platform).
        - Haina commit; caller aamue transaction.
        """
        plat = ReplyPlatform(str(platform))
        kw = cls._kw(keyword)
        row: AutoReplyTraining | None = (
            session.query(cls)
            .filter(cls.user_id == user_id, func.lower(cls.keyword) == kw, cls.platform == plat)
            .one_or_none()
        )
        if row is None:
            row = cls(
                user_id=user_id,
                keyword=kw,
                platform=plat,
                reply=(reply or "").strip(),
                language=(language or None),
                meta={"source": str(source)},
            )
            if synonyms:
                row.synonyms = cls._norm_list(synonyms, max_len=100)
            if tags:
                row.tags = cls._norm_list(tags, max_len=32)
            session.add(row)
        else:
            row.reply = (reply or "").strip()
            if language is not None:
                row.language = language
            if synonyms is not None:
                row.synonyms = cls._norm_list(synonyms, max_len=100)
            if tags is not None:
                row.tags = cls._norm_list(tags, max_len=32)
            meta = dict(row.meta or {})
            meta.setdefault("source", str(source))
            row.meta = meta
        return row

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<AutoReplyTraining id={self.id} user_id={self.user_id} "
            f"kw='{self.keyword}' platform={self.platform} conf={self.confidence} enabled={self.enabled}>"
        )


# ---------------- Normalizers / Guards ----------------
@listens_for(AutoReplyTraining, "before_insert")
def _art_before_insert(_m, _c, t: AutoReplyTraining) -> None:
    # sanitize basics
    t.keyword = AutoReplyTraining._kw(t.keyword or "")
    t.reply = (t.reply or "").strip()
    if t.language:
        t.language = t.language.strip()[:12]
    # lists
    if t.synonyms:
        t.synonyms = AutoReplyTraining._norm_list(t.synonyms, max_len=100)
    if t.tags:
        t.tags = AutoReplyTraining._norm_list(t.tags, max_len=32)
    # bounds
    if t.confidence is None:
        t.confidence = 50
    t.confidence = min(100, max(0, int(t.confidence)))
    t.cooldown_sec = max(0, int(t.cooldown_sec or 0))
    if t.throttle_per_min is not None:
        t.throttle_per_min = max(0, int(t.throttle_per_min))


@listens_for(AutoReplyTraining, "before_update")
def _art_before_update(_m, _c, t: AutoReplyTraining) -> None:
    t.keyword = AutoReplyTraining._kw(t.keyword or "")
    t.reply = (t.reply or "").strip()
    if t.language:
        t.language = t.language.strip()[:12]
    if t.synonyms:
        t.synonyms = AutoReplyTraining._norm_list(t.synonyms, max_len=100)
    if t.tags:
        t.tags = AutoReplyTraining._norm_list(t.tags, max_len=32)
    t.confidence = min(100, max(0, int(t.confidence or 0)))
    t.cooldown_sec = max(0, int(t.cooldown_sec or 0))
    if t.throttle_per_min is not None:
        t.throttle_per_min = max(0, int(t.throttle_per_min))
