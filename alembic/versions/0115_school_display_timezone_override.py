"""per-school workshop display timezone, as an override over the state map

Re-adds ``schools.display_timezone``, dropped in revision 0112. What has
changed since is that the zone is no longer *entered* per school — it is
derived from ``schools.state`` through ``STATE_TIMEZONES``
(src/schools/display_timezone.py), and this column exists only to correct the
schools that fall on the wrong side of a zone boundary inside their own state.

0112 removed the column because a manual per-school field is a field nobody
fills in, leaving every row NULL and the app-wide default doing all the work.
Derivation inverts that: the common case needs no data entry, and the column is
touched only for a genuine exception.

Nullable with no default, deliberately. NULL means "derive from state", which
is a live resolution, not a value worth freezing into 261 rows at migration
time. Existing rows therefore start on the derived zone, which is the intended
behaviour for all but the boundary cases.

Known exceptions in the current roster: Tennessee maps to Central (Nashville,
Memphis) while the Chattanooga and Knoxville schools are Eastern. Those rows
need this column set; the migration does not guess on their behalf.

Downgrade drops the column, and the resolution layer falls through to the
app-wide default for every school.

Revision ID: 0115
Revises: 0114
Create Date: 2026-09-03
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0115"
down_revision: Union[str, None] = "0114"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("schools", sa.Column("display_timezone", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("schools", "display_timezone")
