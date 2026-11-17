"""M10 social account label.

Revision ID: 0010
Revises: 0009
Create Date: 2026-11-17

Adds an optional human label to ``social_accounts`` so ``heraldesk account add --label``
and ``heraldesk account list`` can distinguish throwaway and real accounts. The column is
nullable and additive; no existing row changes.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the optional account label."""
    op.add_column(
        "social_accounts",
        sa.Column("label", sa.String(length=120), nullable=True),
    )


def downgrade() -> None:
    """Remove the account label."""
    op.drop_column("social_accounts", "label")
