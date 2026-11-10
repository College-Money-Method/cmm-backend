"""Assistant chunks: which schools, cohorts and states may see each chunk

Resources an admin restricted to schools, cohorts or states can now be indexed.
The scope sits on the chunk, outside its content hash, so changing who may see
a resource never re-embeds it. All three empty means visible to everyone, which
is what every existing chunk is, so there is nothing to backfill.

Revision ID: 0146
Revises: 0145
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0146"
down_revision = "0145"
branch_labels = None
depends_on = None

_COLUMNS = ("scope_school_ids", "scope_cohort_ids", "scope_states")


def upgrade() -> None:
    for name in _COLUMNS:
        op.add_column(
            "assistant_chunks",
            sa.Column(name, sa.ARRAY(sa.Text()), nullable=False, server_default="{}"),
        )


def downgrade() -> None:
    for name in reversed(_COLUMNS):
        op.drop_column("assistant_chunks", name)
