"""College Aid Explorer: configurable "Find your SAI" calculator link

``app_config.college_aid_sai_calculator_url`` is nullable; null hides the link.

Revision ID: 0142
Revises: 0141
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0142"
down_revision = "0141"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("app_config", sa.Column("college_aid_sai_calculator_url", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("app_config", "college_aid_sai_calculator_url")
