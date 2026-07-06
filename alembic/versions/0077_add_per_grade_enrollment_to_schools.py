"""schools: add self-reported per-grade (9-12) enrollment columns

Counselors self-report upper-school enrollment by grade in the Hub;
enrollment_9_12 stays the total (kept in sync on update) so the existing
% reach metric and enrollment_range computed column keep working.

Revision ID: 0077
Revises: 0076
Create Date: 2026-07-06
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0077"
down_revision: Union[str, None] = "0076"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_GRADE_COLUMNS = (
    "enrollment_grade_9",
    "enrollment_grade_10",
    "enrollment_grade_11",
    "enrollment_grade_12",
)


def upgrade() -> None:
    for col in _GRADE_COLUMNS:
        op.add_column("schools", sa.Column(col, sa.Integer(), nullable=True))


def downgrade() -> None:
    for col in reversed(_GRADE_COLUMNS):
        op.drop_column("schools", col)
