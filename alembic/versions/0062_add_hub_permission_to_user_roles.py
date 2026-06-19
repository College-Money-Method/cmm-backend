"""add hub_permission to user_roles

@62
@61
Create Date: 2026-06-19
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0062"
down_revision: Union[str, None] = "0061"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_ENUM_NAME = "hub_permission_enum"
_ENUM_VALUES = ("admin", "user")


def upgrade() -> None:
    hub_permission_enum = sa.Enum(*_ENUM_VALUES, name=_ENUM_NAME)
    hub_permission_enum.create(op.get_bind(), checkfirst=True)

    op.add_column(
        "user_roles",
        sa.Column(
            "hub_permission",
            sa.Enum(*_ENUM_VALUES, name=_ENUM_NAME, create_type=False),
            nullable=True,  # temporarily nullable for backfill
        ),
    )

    # Backfill: viewer → user, everyone else → admin
    op.execute(
        """
        UPDATE user_roles
        SET hub_permission = CASE
            WHEN role = 'viewer' THEN 'user'::hub_permission_enum
            ELSE 'admin'::hub_permission_enum
        END
        """
    )

    # Now enforce NOT NULL
    op.alter_column("user_roles", "hub_permission", nullable=False)


def downgrade() -> None:
    op.drop_column("user_roles", "hub_permission")
    sa.Enum(name=_ENUM_NAME).drop(op.get_bind(), checkfirst=True)
