"""M31 Draft Room profile role.

Revision ID: 0012
Revises: 0011
Create Date: 2026-11-20

The Draft Room profile role stores a `profile_patch` artifact, so its role name must be
an accepted value on ``studio_messages``.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NEW_ROLES = "'user','editor','writer','compliance','system','profile'"
OLD_ROLES = "'user','editor','writer','compliance','system'"


def upgrade() -> None:
    """Allow the profile role."""
    with op.batch_alter_table("studio_messages") as batch:
        batch.drop_constraint("ck_studio_messages_role", type_="check")
        batch.create_check_constraint(
            "ck_studio_messages_role",
            f"role IN ({NEW_ROLES})",
        )


def downgrade() -> None:
    """Drop the profile role."""
    with op.batch_alter_table("studio_messages") as batch:
        batch.drop_constraint("ck_studio_messages_role", type_="check")
        batch.create_check_constraint(
            "ck_studio_messages_role",
            f"role IN ({OLD_ROLES})",
        )
