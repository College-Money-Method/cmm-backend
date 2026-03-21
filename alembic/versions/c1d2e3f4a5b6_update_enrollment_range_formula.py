"""Update enrollment_range formula to use compact labels.

Revision ID: c1d2e3f4a5b6
Revises: b9e1f2a3c4d5
Create Date: 2026-03-21 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op

revision: str = "c1d2e3f4a5b6"
down_revision: Union[str, None] = "b9e1f2a3c4d5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_NEW_EXPR = (
    "CASE "
    "WHEN enrollment_9_12 IS NULL THEN NULL "
    "WHEN enrollment_9_12 < 250 THEN '< 250' "
    "WHEN enrollment_9_12 <= 500 THEN '250-500' "
    "ELSE '>500' "
    "END"
)

_OLD_EXPR = (
    "CASE "
    "WHEN enrollment_9_12 IS NULL THEN NULL "
    "WHEN enrollment_9_12 < 250 THEN '< 250' "
    "WHEN enrollment_9_12 <= 500 THEN '250 - 500' "
    "ELSE '> 500' "
    "END"
)


def upgrade() -> None:
    op.execute("ALTER TABLE schools DROP COLUMN enrollment_range")
    op.execute(
        f"ALTER TABLE schools ADD COLUMN enrollment_range TEXT GENERATED ALWAYS AS ({_NEW_EXPR}) STORED"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE schools DROP COLUMN enrollment_range")
    op.execute(
        f"ALTER TABLE schools ADD COLUMN enrollment_range TEXT GENERATED ALWAYS AS ({_OLD_EXPR}) STORED"
    )
