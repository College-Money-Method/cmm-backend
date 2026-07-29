"""add is_cmm_website_activated to schools

Lets an admin activate the School Resource Center for a prospect school
(is_current_customer=false) so they can be shared a preview link before
becoming a customer. Public SRC access = is_current_customer OR this flag.

Revision ID: 0091
Revises: 0090
Create Date: 2026-07-29
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0091"
down_revision: Union[str, None] = "0090"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "schools",
        sa.Column(
            "is_cmm_website_activated",
            sa.Boolean(),
            nullable=False,
            server_default="false",
        ),
    )


def downgrade() -> None:
    op.drop_column("schools", "is_cmm_website_activated")
