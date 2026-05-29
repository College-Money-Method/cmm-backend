"""Add page_title, page_description, banner_image_url to grade_configs.

@13
@11
Create Date: 2026-04-09
"""

from alembic import op
import sqlalchemy as sa


revision = "0013"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("grade_configs", sa.Column("page_title", sa.Text(), nullable=True))
    op.add_column("grade_configs", sa.Column("page_description", sa.Text(), nullable=True))
    op.add_column("grade_configs", sa.Column("banner_image_url", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("grade_configs", "banner_image_url")
    op.drop_column("grade_configs", "page_description")
    op.drop_column("grade_configs", "page_title")
