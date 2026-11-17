"""M6 Telegram inbox and notification tables.

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-17

Adds the minimal inbound inbox (``telegram_messages``) and the idempotent outbound
notification ledger (``telegram_notifications``). Bot tokens are not stored here: they
live in ``.env`` or in an encrypted file protected by ``secret.key``.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the Telegram inbox and notification tables."""
    op.create_table(
        "telegram_messages",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("profile_id", sa.Integer(), nullable=True),
        sa.Column("chat_id", sa.Integer(), nullable=False),
        sa.Column("sender_id", sa.Integer(), nullable=True),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("command", sa.String(length=32), nullable=True),
        sa.Column("content_item_id", sa.Integer(), nullable=True),
        sa.Column("response", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(["profile_id"], ["profiles.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["content_item_id"],
            ["content_items.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_telegram_messages_profile_id",
        "telegram_messages",
        ["profile_id"],
        unique=False,
    )
    op.create_index(
        "ix_telegram_messages_created_at",
        "telegram_messages",
        ["created_at"],
        unique=False,
    )

    op.create_table(
        "telegram_notifications",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("profile_id", sa.Integer(), nullable=True),
        sa.Column("event", sa.String(length=32), nullable=False),
        sa.Column("idempotency_key", sa.String(length=200), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("message_preview", sa.Text(), nullable=True),
        sa.Column("recipients", sa.JSON(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "event IN ('approval_requested','published','failed_final','killswitch')",
            name="ck_telegram_notifications_event",
        ),
        sa.CheckConstraint(
            "status IN ('sent','suppressed','failed')",
            name="ck_telegram_notifications_status",
        ),
        sa.ForeignKeyConstraint(["profile_id"], ["profiles.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "idempotency_key",
            name="uq_telegram_notifications_idempotency_key",
        ),
    )
    op.create_index(
        "ix_telegram_notifications_profile_id",
        "telegram_notifications",
        ["profile_id"],
        unique=False,
    )
    op.create_index(
        "ix_telegram_notifications_event",
        "telegram_notifications",
        ["event"],
        unique=False,
    )


def downgrade() -> None:
    """Drop the Telegram tables."""
    op.drop_index(
        "ix_telegram_notifications_event",
        table_name="telegram_notifications",
    )
    op.drop_index(
        "ix_telegram_notifications_profile_id",
        table_name="telegram_notifications",
    )
    op.drop_table("telegram_notifications")
    op.drop_index("ix_telegram_messages_created_at", table_name="telegram_messages")
    op.drop_index("ix_telegram_messages_profile_id", table_name="telegram_messages")
    op.drop_table("telegram_messages")
