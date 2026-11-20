"""M29 Draft Room threads and messages.

Revision ID: 0011
Revises: 0010
Create Date: 2026-11-20

Adds the planning tables behind the disabled-by-default Draft Room. They are independent
of the content and approval state machines; a draft created from a thread later enters
``content_items`` through the existing approval service.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the thread and message tables."""
    op.create_table(
        "studio_threads",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column(
            "profile_id",
            sa.Integer(),
            sa.ForeignKey("profiles.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column(
            "status",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'open'"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint("status IN ('open','archived')", name="ck_studio_threads_status"),
    )
    op.create_index("ix_studio_threads_profile_id", "studio_threads", ["profile_id"])
    op.create_index("ix_studio_threads_updated_at", "studio_threads", ["updated_at"])

    op.create_table(
        "studio_messages",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column(
            "profile_id",
            sa.Integer(),
            sa.ForeignKey("profiles.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "thread_id",
            sa.Integer(),
            sa.ForeignKey("studio_threads.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("artifact_type", sa.String(length=24), nullable=True),
        sa.Column("artifact_json", sa.JSON(), nullable=True),
        sa.Column(
            "agent_run_id",
            sa.Integer(),
            sa.ForeignKey("agent_runs.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "content_item_id",
            sa.Integer(),
            sa.ForeignKey("content_items.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("cost_usd", sa.Float(), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "role IN ('user','editor','writer','compliance','system')",
            name="ck_studio_messages_role",
        ),
        sa.CheckConstraint(
            "artifact_type IS NULL OR "
            "artifact_type IN ('idea','draft','compliance','profile_patch')",
            name="ck_studio_messages_artifact_type",
        ),
    )
    op.create_index("ix_studio_messages_profile_id", "studio_messages", ["profile_id"])
    op.create_index("ix_studio_messages_thread_id", "studio_messages", ["thread_id"])
    op.create_index("ix_studio_messages_created_at", "studio_messages", ["created_at"])


def downgrade() -> None:
    """Drop the Draft Room tables."""
    op.drop_index("ix_studio_messages_created_at", table_name="studio_messages")
    op.drop_index("ix_studio_messages_thread_id", table_name="studio_messages")
    op.drop_index("ix_studio_messages_profile_id", table_name="studio_messages")
    op.drop_table("studio_messages")
    op.drop_index("ix_studio_threads_updated_at", table_name="studio_threads")
    op.drop_index("ix_studio_threads_profile_id", table_name="studio_threads")
    op.drop_table("studio_threads")
