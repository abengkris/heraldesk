"""M7 server state: deduplicate inbound Telegram updates.

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-17

Adds ``telegram_messages.update_id`` with a unique index. Telegram retries a webhook
or replays ``getUpdates`` after a restart, so the server records the platform update id
and refuses to process the same update twice.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the unique Telegram update id used for deduplication."""
    op.add_column(
        "telegram_messages",
        sa.Column("update_id", sa.Integer(), nullable=True),
    )
    op.create_index(
        "ix_telegram_messages_update_id",
        "telegram_messages",
        ["update_id"],
        unique=True,
    )


def downgrade() -> None:
    """Drop the Telegram update id column and its unique index."""
    op.drop_index("ix_telegram_messages_update_id", table_name="telegram_messages")
    op.drop_column("telegram_messages", "update_id")
