"""make schools.slug the authoritative public URL segment

``slug`` used to be shadowed at serialization time by ``airtable_slug`` (the
API returned ``airtable_slug or slug``), so the column did not reflect the
school's real /school/<slug> URL. That made the slug un-editable in practice
and broke every direct ``School.slug ==`` lookup (grade-config and search
school scoping) for any school whose effective slug came from Airtable.

This backfills ``slug`` from ``airtable_slug`` wherever the two disagree, so
dropping the override in the schemas leaves every existing public URL byte-for-
byte identical. ``airtable_slug`` is kept and still resolves in
``_find_public_school`` as a legacy alias.

Rows whose ``airtable_slug`` is already claimed by another school's ``slug``
are skipped — the unique constraint wins and those keep their current slug.

Revision ID: 0108
Revises: 0107
Create Date: 2026-09-01
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0108"
down_revision: Union[str, None] = "0107"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE schools AS s
           SET slug = s.airtable_slug
         WHERE s.airtable_slug IS NOT NULL
           AND s.airtable_slug <> ''
           AND (s.slug IS NULL OR s.slug <> s.airtable_slug)
           AND NOT EXISTS (
                 SELECT 1 FROM schools AS o
                  WHERE o.id <> s.id
                    AND o.slug = s.airtable_slug
               )
        """
    )


def downgrade() -> None:
    # The pre-backfill slugs are not recoverable, and they were never the
    # public URL — the schema shape is unchanged, so this is a no-op.
    pass
