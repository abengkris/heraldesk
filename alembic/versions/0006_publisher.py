"""M5 publisher metadata on content items.

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-17

Adds the retry and autonomy bookkeeping the publisher and scheduler need. No new table is
introduced: the content item row already owns its state, schedule and external id.

``publish_mode`` records the policy decision that produced the item so the approval
service can schedule it without reloading profile files. ``publish_attempts`` and
``last_error`` make retries durable across restarts. ``publish_delay_minutes`` preserves
the cancellation window promised by ``auto_after_delay``.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add publisher columns and the due-item index."""
    op.add_column(
        "content_items",
        sa.Column("publish_mode", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "content_items",
        sa.Column("publish_delay_minutes", sa.Integer(), nullable=True),
    )
    op.add_column(
        "content_items",
        sa.Column(
            "publish_attempts",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.add_column("content_items", sa.Column("last_error", sa.Text(), nullable=True))
    op.create_index(
        "ix_content_items_due",
        "content_items",
        ["state", "scheduled_at"],
        unique=False,
    )


def downgrade() -> None:
    """Remove the publisher metadata."""
    op.drop_index("ix_content_items_due", table_name="content_items")
    op.drop_column("content_items", "last_error")
    op.drop_column("content_items", "publish_attempts")
    op.drop_column("content_items", "publish_delay_minutes")
    op.drop_column("content_items", "publish_mode")
