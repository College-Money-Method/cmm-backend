"""calculator authoring documentation

Adds ``calculators.documentation``: the markdown brief for whoever changes a
calculator next — human or AI agent. It records where the calculator's numbers
come from, the ``window.__CALC_CONFIG__`` / ``__CALC_RUN__`` / ``__CALC_SELFTEST__``
contract it has to keep, and the cases that lock its arithmetic.

Nullable with no default: an existing row simply has no brief written yet, and
the column is never read at render time.

Revision ID: 0106
Revises: 0105
Create Date: 2026-08-24
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0106"
down_revision: Union[str, None] = "0105"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("calculators", sa.Column("documentation", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("calculators", "documentation")
