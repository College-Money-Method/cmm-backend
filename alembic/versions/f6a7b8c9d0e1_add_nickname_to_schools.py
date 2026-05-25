"""add nickname to schools

Revision ID: f6a7b8c9d0e1
Revises: aa1bb2cc3dd4
Create Date: 2026-05-25
"""
from alembic import op
import sqlalchemy as sa

revision = "f6a7b8c9d0e1"
down_revision = "aa1bb2cc3dd4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("schools", sa.Column("nickname", sa.Text, nullable=True))


def downgrade() -> None:
    op.drop_column("schools", "nickname")
