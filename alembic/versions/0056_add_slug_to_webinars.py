"""add slug to webinars

@56
@55
Create Date: 2026-06-09
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0056"
down_revision: Union[str, None] = "0055"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Trigger function: auto-computes slug from workshop name + first 8 hex chars of webinar UUID.
# Pattern matches frontend: slugify(workshopName) + "-" + webinarId.replace(/-/g,"").slice(0,8)
_CREATE_TRIGGER_FN = """
CREATE OR REPLACE FUNCTION set_webinar_slug()
RETURNS TRIGGER AS $$
BEGIN
    NEW.slug := (
        SELECT regexp_replace(lower(w.name), '[^a-z0-9]+', '-', 'g')
               || '-'
               || left(replace(NEW.id::text, '-', ''), 8)
        FROM workshops w
        WHERE w.id = NEW.workshop_id
    );
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""

_CREATE_TRIGGER = """
CREATE TRIGGER trg_webinar_slug
BEFORE INSERT OR UPDATE ON webinars
FOR EACH ROW EXECUTE FUNCTION set_webinar_slug();
"""

_DROP_TRIGGER = "DROP TRIGGER IF EXISTS trg_webinar_slug ON webinars;"
_DROP_TRIGGER_FN = "DROP FUNCTION IF EXISTS set_webinar_slug();"


def upgrade() -> None:
    # 1. Add nullable slug column
    op.add_column("webinars", sa.Column("slug", sa.Text(), nullable=True))

    # 2. Backfill existing rows via JOIN to workshops
    op.execute(
        """
        UPDATE webinars
        SET slug = regexp_replace(lower(w.name), '[^a-z0-9]+', '-', 'g')
                   || '-'
                   || left(replace(webinars.id::text, '-', ''), 8)
        FROM workshops w
        WHERE w.id = webinars.workshop_id
        """
    )

    # 3. Unique index (nullable column — partial unique is fine, but full unique on text is OK too)
    op.create_index("idx_webinars_slug", "webinars", ["slug"], unique=True)

    # 4. Trigger function + trigger for future inserts/updates
    op.execute(_CREATE_TRIGGER_FN)
    op.execute(_CREATE_TRIGGER)


def downgrade() -> None:
    op.execute(_DROP_TRIGGER)
    op.execute(_DROP_TRIGGER_FN)
    op.drop_index("idx_webinars_slug", table_name="webinars")
    op.drop_column("webinars", "slug")
