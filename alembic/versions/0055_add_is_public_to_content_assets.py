"""add is_public to content_assets

@55
@54
Create Date: 2026-06-04
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0055"
down_revision: Union[str, None] = "0054"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "content_assets",
        sa.Column("is_public", sa.Boolean(), nullable=False, server_default="false"),
    )
    op.create_index("idx_content_assets_is_public", "content_assets", ["is_public"])


def downgrade() -> None:
    op.drop_index("idx_content_assets_is_public", table_name="content_assets")
    op.drop_column("content_assets", "is_public")
