"""M3 content pipeline tables.

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-16

Adds ``pipeline_runs``, ``content_ideas`` and ``content_items``, and links agent runs to
the pipeline run that triggered them. ``pipeline_run_id`` was a plain integer in 0003
because the table did not exist yet.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the M3 content pipeline tables and link agent runs."""
    op.create_table(
        "pipeline_runs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("profile_id", sa.Integer(), nullable=False),
        sa.Column("pipeline", sa.String(length=64), nullable=False),
        sa.Column("trigger", sa.String(length=16), nullable=False, server_default="cli"),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="running"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "status IN ('running','succeeded','failed')",
            name="ck_pipeline_runs_status",
        ),
        sa.CheckConstraint(
            "trigger IN ('cli','dry_run','schedule','manual')",
            name="ck_pipeline_runs_trigger",
        ),
        sa.ForeignKeyConstraint(["profile_id"], ["profiles.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_pipeline_runs_profile_id", "pipeline_runs", ["profile_id"], unique=False)
    op.create_index("ix_pipeline_runs_started_at", "pipeline_runs", ["started_at"], unique=False)

    op.create_table(
        "content_ideas",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("profile_id", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(length=64), nullable=False, server_default="research"),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("angle", sa.Text(), nullable=False, server_default=""),
        sa.Column("evidence", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="proposed"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "status IN ('proposed','used','discarded')",
            name="ck_content_ideas_status",
        ),
        sa.ForeignKeyConstraint(["profile_id"], ["profiles.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_content_ideas_profile_id", "content_ideas", ["profile_id"], unique=False)
    op.create_index("ix_content_ideas_created_at", "content_ideas", ["created_at"], unique=False)

    op.create_table(
        "content_items",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("profile_id", sa.Integer(), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=True),
        sa.Column("idea_id", sa.Integer(), nullable=True),
        sa.Column("platform", sa.String(length=32), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("media", sa.JSON(), nullable=False),
        sa.Column("state", sa.String(length=24), nullable=False, server_default="drafted"),
        sa.Column("risk_level", sa.String(length=16), nullable=True),
        sa.Column("risk_notes", sa.Text(), nullable=True),
        sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("external_id", sa.String(length=200), nullable=True),
        sa.Column("parent_id", sa.Integer(), nullable=True),
        sa.Column("created_by_run_id", sa.Integer(), nullable=True),
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
        sa.CheckConstraint(
            "state IN ("
            "'drafted','reviewed','pending_approval','approved','scheduled','published',"
            "'blocked','rejected','cancelled','failed','needs_review'"
            ")",
            name="ck_content_items_state",
        ),
        sa.CheckConstraint(
            "risk_level IS NULL OR risk_level IN ('low','medium','high','blocked')",
            name="ck_content_items_risk_level",
        ),
        sa.ForeignKeyConstraint(["account_id"], ["social_accounts.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["idea_id"], ["content_ideas.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["parent_id"], ["content_items.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["profile_id"], ["profiles.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by_run_id"], ["agent_runs.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_content_items_profile_id", "content_items", ["profile_id"], unique=False)
    op.create_index("ix_content_items_state", "content_items", ["state"], unique=False)
    op.create_index("ix_content_items_created_at", "content_items", ["created_at"], unique=False)

    with op.batch_alter_table("agent_runs") as batch_op:
        batch_op.create_foreign_key(
            "fk_agent_runs_pipeline_run_id",
            "pipeline_runs",
            ["pipeline_run_id"],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    """Drop the M3 tables and restore the plain integer pipeline run id."""
    with op.batch_alter_table("agent_runs") as batch_op:
        batch_op.drop_constraint("fk_agent_runs_pipeline_run_id", type_="foreignkey")

    op.drop_index("ix_content_items_created_at", table_name="content_items")
    op.drop_index("ix_content_items_state", table_name="content_items")
    op.drop_index("ix_content_items_profile_id", table_name="content_items")
    op.drop_table("content_items")

    op.drop_index("ix_content_ideas_created_at", table_name="content_ideas")
    op.drop_index("ix_content_ideas_profile_id", table_name="content_ideas")
    op.drop_table("content_ideas")

    op.drop_index("ix_pipeline_runs_started_at", table_name="pipeline_runs")
    op.drop_index("ix_pipeline_runs_profile_id", table_name="pipeline_runs")
    op.drop_table("pipeline_runs")
