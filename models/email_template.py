# backend/models/email_template.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import re
import enum
import datetime as dt
from typing import Optional, Dict, Any, Iterable, TYPE_CHECKING

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
)
from sqlalchemy.orm import Mapped, mapped_column, relationship, validates
from sqlalchemy.ext.mutable import MutableDict
from sqlalchemy.event import listens_for

from backend.db import Base
from backend.models._types import JSON_VARIANT  # PG: JSONB, others: JSON

if TYPE_CHECKING:
    from .user import User


# ---------- Enums ----------
class TemplateType(str, enum.Enum):
    transactional = "transactional"
    marketing     = "marketing"
    notification  = "notification"
    other         = "other"


class VersionState(str, enum.Enum):
    draft     = "draft"
    approved  = "approved"
    published = "published"
    archived  = "archived"


# =========================
# Parent: EmailTemplate
# Child : EmailTemplateVersion
# =========================
class EmailTemplate(Base):
    """Kichwa cha template (identity + i18n + pointer ya toleo la sasa)."""
    __tablename__ = "email_templates"
    __mapper_args__ = {"eager_defaults": True}
    __table_args__ = (
        UniqueConstraint("name", "locale", name="uq_email_template_name_locale"),
        Index("ix_email_template_slug", "slug"),
        Index("ix_email_template_type_active", "template_type", "is_active"),
        CheckConstraint("length(trim(name)) >= 3", name="ck_email_tmpl_name_len"),
        CheckConstraint(
            "locale IS NULL OR length(trim(locale)) BETWEEN 2 AND 12",
            name="ck_email_tmpl_locale_len",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)

    # Identity
    name: Mapped[str] = mapped_column(String(100), nullable=False)   # e.g. "payment_success"
    slug: Mapped[Optional[str]] = mapped_column(String(120), unique=True)

    # Classification
    template_type: Mapped[TemplateType] = mapped_column(
        SQLEnum(TemplateType, name="email_template_type", native_enum=False, validate_strings=True),
        default=TemplateType.transactional,
        nullable=False,
        index=True,
    )

    # i18n
    locale: Mapped[Optional[str]] = mapped_column(String(12), index=True, doc="e.g. en, sw, en-US")

    # Flags
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, index=True)

    # Pointer ya toleo la sasa
    current_version_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("email_template_versions.id", ondelete="SET NULL"), index=True
    )

    # Audit
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP"), nullable=False
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP"),
        onupdate=text("CURRENT_TIMESTAMP"), nullable=False
    )

    # Relationships
    # MUHIMU: tumeweka foreign_keys + primaryjoin ili kuondoa AmbiguousForeignKeysError
    versions: Mapped[list["EmailTemplateVersion"]] = relationship(
        "EmailTemplateVersion",
        back_populates="template",
        foreign_keys="EmailTemplateVersion.template_id",
        primaryjoin="EmailTemplate.id == EmailTemplateVersion.template_id",
        cascade="all, delete-orphan",
        passive_deletes=True,
        lazy="selectin",
        order_by="EmailTemplateVersion.version.desc()",
        overlaps="current_version,template",
    )

    current_version: Mapped[Optional["EmailTemplateVersion"]] = relationship(
        "EmailTemplateVersion",
        foreign_keys=[current_version_id],
        viewonly=True,
        lazy="selectin",
        overlaps="versions,template",
    )

    # Helpers
    def set_locale(self, code: Optional[str]) -> None:
        self.locale = (code or "").strip()[:12] or None

    def deactivate(self) -> None: self.is_active = False
    def activate(self) -> None:   self.is_active = True

    def __repr__(self) -> str:  # pragma: no cover
        return f"<EmailTemplate id={self.id} name={self.name!r} locale={self.locale} active={self.is_active}>"


class EmailTemplateVersion(Base):
    """
    Toleo la template (subject + HTML + text + metadata).
    - A/B test weight (0..100)
    - `meta` hushika placeholders: {"required_vars": [...], "defaults": {...}, ...}
    """
    __tablename__ = "email_template_versions"
    __mapper_args__ = {"eager_defaults": True}
    __table_args__ = (
        UniqueConstraint("template_id", "version", name="uq_email_tmpl_version"),
        Index("ix_email_tmpl_version_state", "template_id", "state"),
        CheckConstraint("version >= 1", name="ck_email_tmpl_version_min"),
        CheckConstraint("ab_weight BETWEEN 0 AND 100", name="ck_email_tmpl_ab_weight_range"),
        CheckConstraint("length(trim(subject)) >= 3", name="ck_email_tmpl_subject_len"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)

    template_id: Mapped[int] = mapped_column(
        ForeignKey("email_templates.id", ondelete="CASCADE"), nullable=False, index=True
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))

    # Content
    subject: Mapped[str] = mapped_column(String(200), nullable=False)
    html_content: Mapped[str] = mapped_column(Text, nullable=False)
    text_content: Mapped[Optional[str]] = mapped_column(Text)

    # Meta (mutable JSON)
    meta: Mapped[Optional[Dict[str, Any]]] = mapped_column(MutableDict.as_mutable(JSON_VARIANT))

    # State
    state: Mapped[VersionState] = mapped_column(
        SQLEnum(VersionState, name="email_template_version_state", native_enum=False, validate_strings=True),
        default=VersionState.draft,
        nullable=False,
        index=True,
    )

    # A/B
    ab_weight: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))

    # Audit
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP"), nullable=False
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP"),
        onupdate=text("CURRENT_TIMESTAMP"), nullable=False
    )
    author_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), index=True)
    changelog: Mapped[Optional[str]] = mapped_column(String(400))

    # Relationships
    template: Mapped["EmailTemplate"] = relationship(
        "EmailTemplate",
        back_populates="versions",
        foreign_keys=[template_id],
        lazy="selectin",
        overlaps="current_version,versions",
    )
    author: Mapped[Optional["User"]] = relationship("User", lazy="selectin")

    # -------- Rendering & Validation --------
    _VAR_RE = re.compile(r"{{\s*([a-zA-Z_][a-zA-Z0-9_.\-]*)\s*}}")

    def _scan_vars(self, content: str) -> set[str]:
        return {m.group(1) for m in self._VAR_RE.finditer(content or "")}

    def missing_vars(self, context: Dict[str, Any]) -> list[str]:
        req = set((self.meta or {}).get("required_vars", []) or [])
        if not req:
            req = self._scan_vars(self.subject) | self._scan_vars(self.html_content)
            if self.text_content:
                req |= self._scan_vars(self.text_content)
        defaults = dict((self.meta or {}).get("defaults", {}) or {})
        ctx_keys = set((context or {}).keys()) | set(defaults.keys())
        return sorted(req - ctx_keys)

    def _apply_context(self, content: str, context: Dict[str, Any]) -> str:
        defaults = dict((self.meta or {}).get("defaults", {}) or {})
        data = {**defaults, **(context or {})}
        out = content or ""
        for key, val in data.items():
            out = out.replace(f"{{{{{key}}}}}", str(val))
        return out

    def render_subject(self, **kwargs) -> str:        return self._apply_context(self.subject, kwargs)
    def render_html(self, **kwargs) -> str:           return self._apply_context(self.html_content, kwargs)
    def render_text(self, **kwargs) -> Optional[str]: return None if self.text_content is None else self._apply_context(self.text_content, kwargs)

    def render_all(self, **kwargs) -> dict[str, Optional[str]]:
        miss = self.missing_vars(kwargs)
        if miss:
            raise ValueError(f"Missing template variables: {', '.join(miss)}")
        return {"subject": self.render_subject(**kwargs), "html": self.render_html(**kwargs), "text": self.render_text(**kwargs)}

    # -------- Lifecycle helpers --------
    def approve(self, changelog: Optional[str] = None) -> None:
        self.state = VersionState.approved
        if changelog: self.changelog = changelog[:400]

    def publish(self, parent: EmailTemplate, *, make_current: bool = True, changelog: Optional[str] = None) -> None:
        self.state = VersionState.published
        if make_current: parent.current_version_id = self.id
        if changelog:     self.changelog = changelog[:400]

    def archive(self, changelog: Optional[str] = None) -> None:
        self.state = VersionState.archived
        if changelog: self.changelog = changelog[:400]

    def set_ab_weight(self, weight: int) -> None:
        iw = int(weight)
        if not (0 <= iw <= 100):
            raise ValueError("ab_weight must be within 0..100")
        self.ab_weight = iw

    def __repr__(self) -> str:  # pragma: no cover
        return f"<EmailTemplateVersion id={self.id} tmpl={self.template_id} v={self.version} state={self.state}>"

    # -------- Validators --------
    @validates("subject")
    def _v_subject(self, _k, v: str) -> str:
        s = (v or "").strip()
        if len(s) < 3:
            raise ValueError("subject must have at least 3 characters")
        return s[:200]

    @validates("changelog")
    def _v_changelog(self, _k, v: Optional[str]) -> Optional[str]:
        return None if v is None else v.strip()[:400]


# ---------- Event normalizers ----------
@listens_for(EmailTemplate, "before_insert")
def _tmpl_before_insert(_m, _c, t: EmailTemplate) -> None:
    t.name = (t.name or "").strip()[:100]
    if t.slug:   t.slug = t.slug.strip()[:120]
    if t.locale: t.locale = t.locale.strip()[:12]

@listens_for(EmailTemplate, "before_update")
def _tmpl_before_update(_m, _c, t: EmailTemplate) -> None:
    _tmpl_before_insert(_m, _c, t)

@listens_for(EmailTemplateVersion, "before_insert")
def _tmplver_before_insert(_m, _c, v: EmailTemplateVersion) -> None:
    v.subject = (v.subject or "").strip()[:200]
    v.html_content = (v.html_content or "").strip()
    if v.text_content is not None:
        v.text_content = v.text_content.strip()

@listens_for(EmailTemplateVersion, "before_update")
def _tmplver_before_update(_m, _c, v: EmailTemplateVersion) -> None:
    _tmplver_before_insert(_m, _c, v)
