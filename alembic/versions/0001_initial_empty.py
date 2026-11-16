"""Initial empty baseline.

Revision ID: 0001
Revises:
Create Date: 2026-11-16

The schema is introduced milestone by milestone: M1 adds profiles, social_accounts,
audit_log and the business jobs table, and later milestones add content tables.
"""

from __future__ import annotations

from collections.abc import Sequence

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """No tables yet."""


def downgrade() -> None:
    """Nothing to drop."""
