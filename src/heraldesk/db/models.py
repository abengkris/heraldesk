"""SQLAlchemy models for the M1 core domain.

Only tables owned by M1 live here: profiles, social accounts, the audit log and the
business jobs table. Content, approvals, inbox and metrics arrive in later milestones.

Every profile-owned table carries ``profile_id``. Access to those rows goes through
:class:`heraldesk.db.scope.ProfileScope` in the repository layer.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from heraldesk.db.base import Base


def utcnow() -> datetime:
    """Return the current time as a timezone-aware UTC datetime."""
    return datetime.now(UTC)


class Profile(Base):
    """A brand or person managed by this Heraldesk instance."""

    __tablename__ = "profiles"
    __table_args__ = (
        CheckConstraint(
            "type IN ('company','person','public_figure','other')",
            name="ck_profiles_type",
        ),
        CheckConstraint(
            "status IN ('active','paused','draft')",
            name="ck_profiles_status",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    slug: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    type: Mapped[str] = mapped_column(
        String(32), nullable=False, default="other", server_default="other"
    )
    timezone: Mapped[str] = mapped_column(
        String(64), nullable=False, default="UTC", server_default="UTC"
    )
    languages: Mapped[list[str]] = mapped_column(
        JSON, nullable=False, default=list, server_default=text("'[]'")
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="draft", server_default="draft"
    )
    kill_switch: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("0")
    )
    config_hash: Mapped[str] = mapped_column(
        String(64), nullable=False, default="", server_default=""
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utcnow,
        onupdate=utcnow,
        server_default=func.now(),
    )

    accounts: Mapped[list[SocialAccount]] = relationship(
        back_populates="profile", cascade="all, delete-orphan"
    )


class SocialAccount(Base):
    """A platform account owned by a profile. Credentials are encrypted at rest."""

    __tablename__ = "social_accounts"
    __table_args__ = (
        UniqueConstraint(
            "profile_id",
            "platform",
            "handle",
            name="uq_social_accounts_profile_platform_handle",
        ),
        CheckConstraint(
            "status IN ('unverified','active','error','disabled')",
            name="ck_social_accounts_status",
        ),
        Index("ix_social_accounts_profile_id", "profile_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    profile_id: Mapped[int] = mapped_column(
        ForeignKey("profiles.id", ondelete="CASCADE"), nullable=False
    )
    platform: Mapped[str] = mapped_column(String(32), nullable=False)
    handle: Mapped[str] = mapped_column(String(200), nullable=False)
    credentials_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="unverified", server_default="unverified"
    )
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utcnow,
        onupdate=utcnow,
        server_default=func.now(),
    )

    profile: Mapped[Profile] = relationship(back_populates="accounts")


class AuditLog(Base):
    """Append-only audit record. Writes go through :class:`heraldesk.core.audit.AuditService`."""

    __tablename__ = "audit_log"
    __table_args__ = (
        Index("ix_audit_log_profile_id", "profile_id"),
        Index("ix_audit_log_created_at", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    profile_id: Mapped[int | None] = mapped_column(
        ForeignKey("profiles.id", ondelete="SET NULL"), nullable=True
    )
    actor: Mapped[str] = mapped_column(String(64), nullable=False)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    entity: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, server_default=func.now()
    )


class Job(Base):
    """Business job persisted for the scheduler that arrives in M5."""

    __tablename__ = "jobs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending','running','done','failed','cancelled')",
            name="ck_jobs_status",
        ),
        Index("ix_jobs_profile_id", "profile_id"),
        Index("ix_jobs_status", "status"),
        Index("ix_jobs_run_at", "run_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    profile_id: Mapped[int | None] = mapped_column(
        ForeignKey("profiles.id", ondelete="CASCADE"), nullable=True
    )
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    run_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending", server_default="pending"
    )
    attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True, unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utcnow,
        onupdate=utcnow,
        server_default=func.now(),
    )
