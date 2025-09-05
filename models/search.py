# backend/models/search.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import enum
import datetime as dt
from typing import Optional, Dict, Any, List

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum as SQLEnum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    CheckConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.db import Base
from backend.models._types import JSON_VARIANT, as_mutable_json

# NB: JSON_VARIANT tayari hutolewa na _types; hakuna fallback za ziada hapa.


# ---------------- Enums ----------------
class Platform(str, enum.Enum):
    android = "android"
    ios     = "ios"
    web     = "web"
    desktop = "desktop"


class SearchSource(str, enum.Enum):
    dashboard  = "dashboard"
    assistant  = "assistant"
    mobile     = "mobile"
    web        = "web"
    livestream = "livestream"
    store      = "store"


# ---------------- Models ----------------
class SearchLog(Base):
    """Rich search telemetry per query."""
    __tablename__ = "search_logs"
    __mapper_args__ = {"eager_defaults": True}
    __table_args__ = (
        Index("ix_search_logs_ts", "timestamp"),
        Index("ix_search_logs_user", "user_id"),
        Index("ix_search_logs_source_ts", "source", "timestamp"),
        CheckConstraint("tokens_count >= 0", name="ck_search_tokens_nonneg"),
        CheckConstraint("results_count >= 0", name="ck_search_results_nonneg"),
        CheckConstraint("latency_ms >= 0", name="ck_search_latency_nonneg"),
        CheckConstraint("country IS NULL OR length(country) = 2", name="ck_search_country_iso2"),
        CheckConstraint("region IS NULL OR length(region) <= 3", name="ck_search_region_len"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)

    # FK lazima isiwe NULL ili iendane na delete-orphan ya User.search_logs
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Query anatomy
    query: Mapped[str] = mapped_column(String(500), nullable=False)
    normalized_query: Mapped[Optional[str]] = mapped_column(String(500))
    query_language: Mapped[Optional[str]] = mapped_column(String(8))   # "en", "sw"
    tokens_count: Mapped[int] = mapped_column(Integer, server_default=text("0"), nullable=False)

    # Context
    source: Mapped[SearchSource] = mapped_column(
        SQLEnum(SearchSource, name="search_source", native_enum=False, validate_strings=True),
        nullable=False,
        server_default=SearchSource.dashboard.value,
        index=True,
    )
    search_type: Mapped[Optional[str]] = mapped_column(String(32))     # products/streams/users/...
    filters: Mapped[Optional[Dict[str, Any]]] = mapped_column(as_mutable_json(JSON_VARIANT))

    # Client/device
    platform: Mapped[Platform] = mapped_column(
        SQLEnum(Platform, name="search_platform", native_enum=False, validate_strings=True),
        nullable=False,
        server_default=Platform.web.value,
        index=True,
    )
    app_version: Mapped[Optional[str]] = mapped_column(String(32))
    device_id: Mapped[Optional[str]] = mapped_column(String(128))
    session_id: Mapped[Optional[str]] = mapped_column(String(64))
    request_id: Mapped[Optional[str]] = mapped_column(String(64))
    network_type: Mapped[Optional[str]] = mapped_column(String(16))    # wifi/4g/5g
    is_metered: Mapped[bool] = mapped_column(Boolean, server_default=text("false"), nullable=False)

    # Region/locale hints
    country: Mapped[Optional[str]] = mapped_column(String(2))
    region:  Mapped[Optional[str]] = mapped_column(String(3))
    timezone: Mapped[Optional[str]] = mapped_column(String(64))

    # Engine telemetry
    results_count: Mapped[int] = mapped_column(Integer, server_default=text("0"), nullable=False)
    latency_ms:    Mapped[int] = mapped_column(Integer, server_default=text("0"), nullable=False)
    no_results:    Mapped[bool] = mapped_column(Boolean, server_default=text("false"), nullable=False)
    vector_used:   Mapped[bool] = mapped_column(Boolean, server_default=text("false"), nullable=False)
    embedding_model: Mapped[Optional[str]] = mapped_column(String(64))
    reranker_model:  Mapped[Optional[str]] = mapped_column(String(64))
    did_you_mean:    Mapped[Optional[str]] = mapped_column(String(255))

    # Error trace (redacted)
    error_code:    Mapped[Optional[str]] = mapped_column(String(64))
    error_message: Mapped[Optional[str]] = mapped_column(String(255))

    # Input modality & flags
    is_voice:       Mapped[bool] = mapped_column(Boolean, server_default=text("false"), nullable=False)
    is_suggestion:  Mapped[bool] = mapped_column(Boolean, server_default=text("false"), nullable=False)

    # Extra room
    extras: Mapped[Optional[Dict[str, Any]]] = mapped_column(as_mutable_json(JSON_VARIANT))

    # Tumia jina la column 'timestamp' ili kulinda DB iliyopo
    timestamp: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        index=True,
    )

    # Relationships
    user: Mapped["User"] = relationship(
        "User",
        back_populates="search_logs",
        foreign_keys=[user_id],
        lazy="selectin",
        passive_deletes=True,
    )
    clicks: Mapped[List["SearchClick"]] = relationship(
        "SearchClick",
        back_populates="log",
        cascade="all, delete-orphan",
        lazy="selectin",
    )
    feedback: Mapped[List["SearchFeedback"]] = relationship(
        "SearchFeedback",
        back_populates="log",
        cascade="all, delete-orphan",
        lazy="selectin",
    )

    # ------------ Helpers ------------
    def set_normalized(self, *, language: Optional[str] = None) -> None:
        """Weka normalized_query (lower/strip) na takriban idadi ya tokens."""
        q = (self.query or "").strip()
        self.normalized_query = q.lower() if q else None
        # tokenization rahisi—badili kulingana na engine yako
        self.tokens_count = len(q.split()) if q else 0
        if language:
            self.query_language = language[:8]

    def record_results(self, *, count: int, latency_ms: int, did_you_mean: Optional[str] = None) -> None:
        self.results_count = max(0, int(count))
        self.latency_ms = max(0, int(latency_ms))
        self.no_results = self.results_count == 0
        self.did_you_mean = (did_you_mean or None)

    def mark_error(self, code: Optional[str], message: Optional[str]) -> None:
        self.error_code = code
        self.error_message = message
        self.no_results = True

    def add_filter(self, key: str, value: Any) -> None:
        f = dict(self.filters or {})
        f[key] = value
        self.filters = f

    def __repr__(self) -> str:  # pragma: no cover
        q = (self.query or "")[:32].replace("\n", " ")
        return f"<SearchLog id={self.id} user_id={self.user_id} q='{q}...' results={self.results_count}>"


class SearchClick(Base):
    """Per-result click telemetry for a given SearchLog."""
    __tablename__ = "search_clicks"
    __mapper_args__ = {"eager_defaults": True}
    __table_args__ = (
        Index("ix_search_clicks_log", "log_id"),
        Index("ix_search_clicks_at", "click_at"),
        CheckConstraint("dwell_ms IS NULL OR dwell_ms >= 0", name="ck_click_dwell_nonneg"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    log_id: Mapped[int] = mapped_column(ForeignKey("search_logs.id", ondelete="CASCADE"), nullable=False, index=True)

    rank: Mapped[Optional[int]] = mapped_column(Integer)
    item_id: Mapped[Optional[str]] = mapped_column(String(64))
    item_type: Mapped[Optional[str]] = mapped_column(String(32))
    title: Mapped[Optional[str]] = mapped_column(String(255))
    opened_in: Mapped[Optional[str]] = mapped_column(String(16))  # same_tab/new_tab/modal
    dwell_ms: Mapped[Optional[int]] = mapped_column(Integer)
    extras: Mapped[Optional[Dict[str, Any]]] = mapped_column(as_mutable_json(JSON_VARIANT))

    click_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), index=True
    )

    log: Mapped["SearchLog"] = relationship("SearchLog", back_populates="clicks", lazy="selectin")

    # Helper
    def bump_dwell(self, ms: int) -> None:
        cur = int(self.dwell_ms or 0)
        self.dwell_ms = max(0, cur + max(0, int(ms)))

    def __repr__(self) -> str:  # pragma: no cover
        return f"<SearchClick id={self.id} log_id={self.log_id} rank={self.rank}>"


class SearchFeedback(Base):
    """User feedback for a given SearchLog (one per user per log)."""
    __tablename__ = "search_feedback"
    __mapper_args__ = {"eager_defaults": True}
    __table_args__ = (
        UniqueConstraint("log_id", "user_id", name="uq_feedback_per_user_per_log"),
        Index("ix_search_feedback_at", "created_at"),
        CheckConstraint("rating IS NULL OR (rating BETWEEN 1 AND 5)", name="ck_feedback_rating_1_5"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    log_id: Mapped[int] = mapped_column(ForeignKey("search_logs.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)

    rating: Mapped[Optional[int]] = mapped_column(Integer)  # 1..5
    helpful: Mapped[Optional[bool]] = mapped_column(Boolean)
    reason: Mapped[Optional[str]] = mapped_column(String(64))  # too_slow/irrelevant/...
    comment: Mapped[Optional[str]] = mapped_column(Text)

    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), index=True
    )

    log: Mapped["SearchLog"] = relationship("SearchLog", back_populates="feedback", lazy="selectin")

    def __repr__(self) -> str:  # pragma: no cover
        return f"<SearchFeedback id={self.id} log_id={self.log_id} user_id={self.user_id}>"
