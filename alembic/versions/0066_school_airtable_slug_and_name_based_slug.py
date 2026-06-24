"""add airtable_slug to schools and backfill null slugs from name

@66
@65
Create Date: 2026-06-24
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0066"
down_revision: Union[str, None] = "0065"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Store the raw Airtable slug separately from the app-owned slug
    op.add_column("schools", sa.Column("airtable_slug", sa.Text(), nullable=True))

    # Backfill slug for any school that still has NULL — derive from name.
    # Uses a DO block to guarantee uniqueness against existing slugs.
    op.execute("""
        DO $$
        DECLARE
            rec RECORD;
            base_slug TEXT;
            final_slug TEXT;
            counter INT;
        BEGIN
            FOR rec IN SELECT id, name FROM schools WHERE slug IS NULL ORDER BY id LOOP
                base_slug := trim(both '-' from
                    regexp_replace(
                        regexp_replace(
                            regexp_replace(lower(rec.name), '[^a-z0-9\\s-]', '', 'g'),
                            '\\s+', '-', 'g'),
                        '-+', '-', 'g'));

                final_slug := base_slug;
                counter := 2;
                WHILE EXISTS (SELECT 1 FROM schools WHERE slug = final_slug) LOOP
                    final_slug := base_slug || '-' || counter;
                    counter := counter + 1;
                END LOOP;

                UPDATE schools SET slug = final_slug WHERE id = rec.id;
            END LOOP;
        END $$;
    """)


def downgrade() -> None:
    op.drop_column("schools", "airtable_slug")
    # Intentionally do NOT null out slugs on downgrade — they are still valid routing keys
