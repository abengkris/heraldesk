"""SQLAlchemy models for the core domain, agent runtime and content pipeline.

Tables owned by M1: profiles, social accounts, the audit log and the business jobs table.
M2 adds ``agent_runs``. M3 adds ``pipeline_runs``, ``content_ideas`` and ``content_items``.
M4 adds ``approvals``. M5 adds publisher metadata to ``content_items``. M6 adds
``telegram_messages`` and ``telegram_notifications``. M7 adds the unique Telegram
``update_id`` used by the server poller. Metrics arrive later.

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
    Float,
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
    label: Mapped[str | None] = mapped_column(String(120), nullable=True)
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


class AgentRun(Base):
    """One AgentRunner execution, including every retry it needed.

    ``tokens_in``, ``tokens_out`` and ``cost_usd`` are totals across all LLM calls made for
    this run. ``pipeline_run_id`` stays a plain integer until the ``pipeline_runs`` table
    arrives with the first pipeline in M3.
    """

    __tablename__ = "agent_runs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('running','succeeded','failed')",
            name="ck_agent_runs_status",
        ),
        Index("ix_agent_runs_profile_id", "profile_id"),
        Index("ix_agent_runs_started_at", "started_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    profile_id: Mapped[int] = mapped_column(
        ForeignKey("profiles.id", ondelete="CASCADE"), nullable=False
    )
    agent: Mapped[str] = mapped_column(String(32), nullable=False)
    pipeline_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("pipeline_runs.id", ondelete="SET NULL"), nullable=True
    )
    input: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    output: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    model: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    tokens_in: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="running", server_default="running"
    )
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


CONTENT_STATES = (
    "drafted",
    "reviewed",
    "pending_approval",
    "approved",
    "scheduled",
    "published",
    "blocked",
    "rejected",
    "cancelled",
    "failed",
    "needs_review",
)


class PipelineRun(Base):
    """One execution of a deterministic pipeline such as ``daily_content``."""

    __tablename__ = "pipeline_runs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('running','succeeded','failed')",
            name="ck_pipeline_runs_status",
        ),
        CheckConstraint(
            "trigger IN ('cli','dry_run','schedule','manual')",
            name="ck_pipeline_runs_trigger",
        ),
        Index("ix_pipeline_runs_profile_id", "profile_id"),
        Index("ix_pipeline_runs_started_at", "started_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    profile_id: Mapped[int] = mapped_column(
        ForeignKey("profiles.id", ondelete="CASCADE"), nullable=False
    )
    pipeline: Mapped[str] = mapped_column(String(64), nullable=False)
    trigger: Mapped[str] = mapped_column(
        String(16), nullable=False, default="cli", server_default="cli"
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="running", server_default="running"
    )
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class ContentIdea(Base):
    """A research idea that may become one or more content items."""

    __tablename__ = "content_ideas"
    __table_args__ = (
        CheckConstraint(
            "status IN ('proposed','used','discarded')",
            name="ck_content_ideas_status",
        ),
        Index("ix_content_ideas_profile_id", "profile_id"),
        Index("ix_content_ideas_created_at", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    profile_id: Mapped[int] = mapped_column(
        ForeignKey("profiles.id", ondelete="CASCADE"), nullable=False
    )
    source: Mapped[str] = mapped_column(String(64), nullable=False, default="research")
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    angle: Mapped[str] = mapped_column(Text, nullable=False, default="")
    evidence: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="proposed", server_default="proposed"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, server_default=func.now()
    )


class ContentItem(Base):
    """One draft or published post. State changes go through ``domain.content.transition``."""

    __tablename__ = "content_items"
    __table_args__ = (
        CheckConstraint(
            "state IN ("
            "'drafted','reviewed','pending_approval','approved','scheduled','published',"
            "'blocked','rejected','cancelled','failed','needs_review'"
            ")",
            name="ck_content_items_state",
        ),
        CheckConstraint(
            "risk_level IS NULL OR risk_level IN ('low','medium','high','blocked')",
            name="ck_content_items_risk_level",
        ),
        Index("ix_content_items_profile_id", "profile_id"),
        Index("ix_content_items_state", "state"),
        Index("ix_content_items_created_at", "created_at"),
        Index("ix_content_items_due", "state", "scheduled_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    profile_id: Mapped[int] = mapped_column(
        ForeignKey("profiles.id", ondelete="CASCADE"), nullable=False
    )
    account_id: Mapped[int | None] = mapped_column(
        ForeignKey("social_accounts.id", ondelete="SET NULL"), nullable=True
    )
    idea_id: Mapped[int | None] = mapped_column(
        ForeignKey("content_ideas.id", ondelete="SET NULL"), nullable=True
    )
    platform: Mapped[str] = mapped_column(String(32), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    media: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    state: Mapped[str] = mapped_column(
        String(24), nullable=False, default="drafted", server_default="drafted"
    )
    risk_level: Mapped[str | None] = mapped_column(String(16), nullable=True)
    risk_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    external_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    publish_mode: Mapped[str | None] = mapped_column(String(32), nullable=True)
    publish_delay_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    publish_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    parent_id: Mapped[int | None] = mapped_column(
        ForeignKey("content_items.id", ondelete="SET NULL"), nullable=True
    )
    created_by_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="SET NULL"), nullable=True
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


APPROVAL_STATES = (
    "pending",
    "approved",
    "rejected",
    "expired",
)


class Approval(Base):
    """One approval request and its outcome for a content item.

    A content item may be approved, edited and sent back for approval more than once, so
    ``approvals`` keeps one row per request. The partial unique index
    ``uq_approvals_pending_item`` guarantees at most one pending request per item.
    """

    __tablename__ = "approvals"
    __table_args__ = (
        CheckConstraint(
            "state IN ('pending','approved','rejected','expired')",
            name="ck_approvals_state",
        ),
        CheckConstraint(
            "decision IS NULL OR decision IN ('approve','reject','edit')",
            name="ck_approvals_decision",
        ),
        CheckConstraint(
            "channel IN ('web','telegram','cli')",
            name="ck_approvals_channel",
        ),
        Index("ix_approvals_profile_id", "profile_id"),
        Index("ix_approvals_content_item_id", "content_item_id"),
        Index("ix_approvals_state", "state"),
        Index(
            "uq_approvals_pending_item",
            "content_item_id",
            unique=True,
            sqlite_where=text("state = 'pending'"),
            postgresql_where=text("state = 'pending'"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    profile_id: Mapped[int] = mapped_column(
        ForeignKey("profiles.id", ondelete="CASCADE"), nullable=False
    )
    content_item_id: Mapped[int] = mapped_column(
        ForeignKey("content_items.id", ondelete="CASCADE"), nullable=False
    )
    state: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending", server_default="pending"
    )
    decision: Mapped[str | None] = mapped_column(String(16), nullable=True)
    edited_body: Mapped[str | None] = mapped_column(Text, nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    channel: Mapped[str] = mapped_column(
        String(16), nullable=False, default="cli", server_default="cli"
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
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


TELEGRAM_NOTIFICATION_EVENTS = (
    "approval_requested",
    "published",
    "failed_final",
    "killswitch",
)

TELEGRAM_NOTIFICATION_STATUSES = (
    "sent",
    "suppressed",
    "failed",
)


TELEGRAM_MESSAGE_STATUSES = (
    "pending",
    "handled",
    "ignored",
)


class TelegramMessage(Base):
    """One inbound Telegram message, stored redacted.

    ``profile_id`` is nullable because a message can arrive before its command links it
    to a profile or content item. The text is already redacted when it reaches this table.

    M8 adds manual triage fields: ``status`` starts as ``pending`` and becomes ``handled``
    or ``ignored`` through the top-level ``heraldesk inbox`` commands. ``response`` holds
    the latest redacted outbound reply. The raw text never leaves this table through audit
    payloads or CLI output.
    """

    __tablename__ = "telegram_messages"
    __table_args__ = (
        Index("ix_telegram_messages_profile_id", "profile_id"),
        Index("ix_telegram_messages_created_at", "created_at"),
        Index("ix_telegram_messages_update_id", "update_id", unique=True),
        Index("ix_telegram_messages_status", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    profile_id: Mapped[int | None] = mapped_column(
        ForeignKey("profiles.id", ondelete="CASCADE"), nullable=True
    )
    update_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    chat_id: Mapped[int] = mapped_column(Integer, nullable=False)
    sender_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    command: Mapped[str | None] = mapped_column(String(32), nullable=True)
    content_item_id: Mapped[int | None] = mapped_column(
        ForeignKey("content_items.id", ondelete="SET NULL"), nullable=True
    )
    response: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending", server_default="pending"
    )
    triaged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ignore_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, server_default=func.now()
    )


class TelegramNotification(Base):
    """One outbound notification event.

    ``idempotency_key`` is unique, so a repeated scheduler run or a manual publisher call
    cannot create a second row or send a second message for the same event.
    """

    __tablename__ = "telegram_notifications"
    __table_args__ = (
        CheckConstraint(
            "event IN ('approval_requested','published','failed_final','killswitch')",
            name="ck_telegram_notifications_event",
        ),
        CheckConstraint(
            "status IN ('sent','suppressed','failed')",
            name="ck_telegram_notifications_status",
        ),
        UniqueConstraint(
            "idempotency_key",
            name="uq_telegram_notifications_idempotency_key",
        ),
        Index("ix_telegram_notifications_profile_id", "profile_id"),
        Index("ix_telegram_notifications_event", "event"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    profile_id: Mapped[int | None] = mapped_column(
        ForeignKey("profiles.id", ondelete="CASCADE"), nullable=True
    )
    event: Mapped[str] = mapped_column(String(32), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    message_preview: Mapped[str | None] = mapped_column(Text, nullable=True)
    recipients: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, server_default=func.now()
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class StudioThread(Base):
    """One Draft Room conversation for a profile.

    A thread is planning state only. It never owns content or approval state; drafts
    created from a message enter the normal ``content_items`` pipeline.
    """

    __tablename__ = "studio_threads"
    __table_args__ = (
        CheckConstraint(
            "status IN ('open','archived')",
            name="ck_studio_threads_status",
        ),
        Index("ix_studio_threads_profile_id", "profile_id"),
        Index("ix_studio_threads_updated_at", "updated_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    profile_id: Mapped[int] = mapped_column(
        ForeignKey("profiles.id", ondelete="CASCADE"), nullable=False
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False, default="New thread")
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="open", server_default="open"
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


class StudioMessage(Base):
    """One turn in a Draft Room thread.

    ``content`` is already redacted when it reaches this table. Artifact columns are
    filled by later phases; ``agent_run_id`` links the turn to its trace and cost.
    """

    __tablename__ = "studio_messages"
    __table_args__ = (
        CheckConstraint(
            "role IN ('user','editor','writer','compliance','system')",
            name="ck_studio_messages_role",
        ),
        CheckConstraint(
            "artifact_type IS NULL OR "
            "artifact_type IN ('idea','draft','compliance','profile_patch')",
            name="ck_studio_messages_artifact_type",
        ),
        Index("ix_studio_messages_profile_id", "profile_id"),
        Index("ix_studio_messages_thread_id", "thread_id"),
        Index("ix_studio_messages_created_at", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    profile_id: Mapped[int] = mapped_column(
        ForeignKey("profiles.id", ondelete="CASCADE"), nullable=False
    )
    thread_id: Mapped[int] = mapped_column(
        ForeignKey("studio_threads.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    artifact_type: Mapped[str | None] = mapped_column(String(24), nullable=True)
    artifact_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    agent_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="SET NULL"), nullable=True
    )
    content_item_id: Mapped[int | None] = mapped_column(
        ForeignKey("content_items.id", ondelete="SET NULL"), nullable=True
    )
    cost_usd: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, server_default=func.now()
    )
