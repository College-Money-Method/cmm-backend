"""College Aid Explorer: federal IPEDS UNITID on colleges

``colleges.unitid`` is nullable (filled over time) and unique when set. Admin
only; the import matches on it after ``college_id`` and before name + state.

Revision ID: 0144
Revises: 0143
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0144"
down_revision = "0143"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("colleges", sa.Column("unitid", sa.Integer(), nullable=True))
    op.create_unique_constraint("uq_colleges_unitid", "colleges", ["unitid"])


def downgrade() -> None:
    op.drop_constraint("uq_colleges_unitid", "colleges", type_="unique")
    op.drop_column("colleges", "unitid")
