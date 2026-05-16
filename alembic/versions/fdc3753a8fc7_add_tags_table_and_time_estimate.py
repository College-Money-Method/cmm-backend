"""add_tags_table_and_time_estimate

Revision ID: fdc3753a8fc7
Revises: eeadff60d52d
Create Date: 2026-05-16 13:04:16.507440
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'fdc3753a8fc7'
down_revision: Union[str, None] = 'eeadff60d52d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Drop the simple tags array column added in the previous migration
    op.drop_column("content_assets", "tags")

    # Dedicated tags lookup table
    op.create_table(
        "tags",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("slug", sa.Text(), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("name", name="uq_tags_name"),
        sa.UniqueConstraint("slug", name="uq_tags_slug"),
    )
    op.create_index("idx_tags_slug", "tags", ["slug"])

    # Many-to-many join
    op.create_table(
        "content_asset_tags",
        sa.Column("content_asset_id", sa.Uuid(), sa.ForeignKey("content_assets.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("tag_id", sa.Uuid(), sa.ForeignKey("tags.id", ondelete="CASCADE"), primary_key=True),
    )

    # Time estimate field (distinct from auto-calculated read_time_minutes)
    op.add_column("content_assets", sa.Column("time_estimate_minutes", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("content_assets", "time_estimate_minutes")
    op.drop_table("content_asset_tags")
    op.drop_index("idx_tags_slug", "tags")
    op.drop_table("tags")
    op.add_column("content_assets", sa.Column("tags", sa.ARRAY(sa.Text()), nullable=True, server_default="{}"))
