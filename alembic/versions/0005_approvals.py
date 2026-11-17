"""M4 approvals table.

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-16

Adds ``approvals`` with a denormalized ``profile_id`` and a partial unique index that
allows at most one pending approval per content item.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the approvals table and its indexes."""
    op.create_table(
        "approvals",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("profile_id", sa.Integer(), nullable=False),
        sa.Column("content_item_id", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False, server_default="pending"),
        sa.Column("decision", sa.String(length=16), nullable=True),
        sa.Column("edited_body", sa.Text(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("channel", sa.String(length=16), nullable=False, server_default="cli"),
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
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "state IN ('pending','approved','rejected','expired')",
            name="ck_approvals_state",
        ),
        sa.CheckConstraint(
            "decision IS NULL OR decision IN ('approve','reject','edit')",
            name="ck_approvals_decision",
        ),
        sa.CheckConstraint(
            "channel IN ('web','telegram','cli')",
            name="ck_approvals_channel",
        ),
        sa.ForeignKeyConstraint(["profile_id"], ["profiles.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["content_item_id"],
            ["content_items.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_approvals_profile_id", "approvals", ["profile_id"], unique=False)
    op.create_index(
        "ix_approvals_content_item_id",
        "approvals",
        ["content_item_id"],
        unique=False,
    )
    op.create_index("ix_approvals_state", "approvals", ["state"], unique=False)
    op.create_index(
        "uq_approvals_pending_item",
        "approvals",
        ["content_item_id"],
        unique=True,
        sqlite_where=sa.text("state = 'pending'"),
        postgresql_where=sa.text("state = 'pending'"),
    )


def downgrade() -> None:
    """Drop the approvals table."""
    op.drop_index("uq_approvals_pending_item", table_name="approvals")
    op.drop_index("ix_approvals_state", table_name="approvals")
    op.drop_index("ix_approvals_content_item_id", table_name="approvals")
    op.drop_index("ix_approvals_profile_id", table_name="approvals")
    op.drop_table("approvals")
