"""M2 agent run traces.

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-16

Adds ``agent_runs`` so every AgentRunner execution records its model, tokens and cost.
``pipeline_run_id`` has no foreign key yet; the ``pipeline_runs`` table arrives with the
first pipeline in M3.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the agent_runs table."""
    op.create_table(
        "agent_runs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("profile_id", sa.Integer(), nullable=False),
        sa.Column("agent", sa.String(length=32), nullable=False),
        sa.Column("pipeline_run_id", sa.Integer(), nullable=True),
        sa.Column("input", sa.JSON(), nullable=False),
        sa.Column("output", sa.JSON(), nullable=True),
        sa.Column("model", sa.String(length=128), nullable=False, server_default=""),
        sa.Column("tokens_in", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("tokens_out", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("cost_usd", sa.Float(), nullable=False, server_default=sa.text("0")),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="running"),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('running','succeeded','failed')",
            name="ck_agent_runs_status",
        ),
        sa.ForeignKeyConstraint(["profile_id"], ["profiles.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_agent_runs_profile_id", "agent_runs", ["profile_id"], unique=False)
    op.create_index("ix_agent_runs_started_at", "agent_runs", ["started_at"], unique=False)


def downgrade() -> None:
    """Drop the agent_runs table."""
    op.drop_index("ix_agent_runs_started_at", table_name="agent_runs")
    op.drop_index("ix_agent_runs_profile_id", table_name="agent_runs")
    op.drop_table("agent_runs")
