"""M8 manual inbox triage state.

Revision ID: 0009
Revises: 0008
Create Date: 2026-11-17

Adds triage fields to the existing M6 inbound table. A message starts ``pending`` and a
human marks it ``handled`` after replying or ``ignored`` with a reason. No new inbox table
and no social platform API is introduced.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the triage status, timestamp and ignore reason."""
    op.add_column(
        "telegram_messages",
        sa.Column(
            "status",
            sa.String(length=16),
            nullable=False,
            server_default="pending",
        ),
    )
    op.add_column(
        "telegram_messages",
        sa.Column("triaged_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "telegram_messages",
        sa.Column("ignore_reason", sa.String(length=32), nullable=True),
    )
    op.create_index(
        "ix_telegram_messages_status",
        "telegram_messages",
        ["status"],
        unique=False,
    )


def downgrade() -> None:
    """Remove the triage columns and index."""
    op.drop_index("ix_telegram_messages_status", table_name="telegram_messages")
    op.drop_column("telegram_messages", "ignore_reason")
    op.drop_column("telegram_messages", "triaged_at")
    op.drop_column("telegram_messages", "status")
