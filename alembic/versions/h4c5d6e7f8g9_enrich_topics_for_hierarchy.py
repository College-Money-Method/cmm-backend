"""Enrich topics table for grade-based hierarchy.

Add description, icon_url, slug, suggested_grades, sort_order to topics.
Add sort_order to content_asset_topics join table.

Revision ID: h4c5d6e7f8g9
Revises: g3b4c5d6e7f8
Create Date: 2026-04-02
"""

from alembic import op
import sqlalchemy as sa

revision = "h4c5d6e7f8g9"
down_revision = "g3b4c5d6e7f8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # -- topics table --
    op.add_column("topics", sa.Column("description", sa.Text(), nullable=True))
    op.add_column("topics", sa.Column("icon_url", sa.Text(), nullable=True))
    op.add_column("topics", sa.Column("slug", sa.Text(), nullable=True))
    op.add_column("topics", sa.Column("suggested_grades", sa.Text(), nullable=True))
    op.add_column(
        "topics",
        sa.Column("sort_order", sa.Integer(), server_default="0", nullable=False),
    )

    # Backfill slugs from existing names
    op.execute(
        """
        UPDATE topics
        SET slug = LOWER(
            TRIM(BOTH '-' FROM REGEXP_REPLACE(name, '[^a-zA-Z0-9]+', '-', 'g'))
        )
        WHERE slug IS NULL
        """
    )
    # Handle potential duplicates by appending a suffix
    op.execute(
        """
        UPDATE topics t1
        SET slug = t1.slug || '-' || SUBSTRING(t1.id::text, 1, 8)
        WHERE EXISTS (
            SELECT 1 FROM topics t2
            WHERE t2.slug = t1.slug AND t2.id < t1.id
        )
        """
    )

    op.alter_column("topics", "slug", nullable=False)
    op.create_unique_constraint("uq_topics_slug", "topics", ["slug"])

    # -- content_asset_topics join table --
    op.add_column(
        "content_asset_topics",
        sa.Column("sort_order", sa.Integer(), server_default="0", nullable=False),
    )


def downgrade() -> None:
    op.drop_column("content_asset_topics", "sort_order")
    op.drop_constraint("uq_topics_slug", "topics", type_="unique")
    op.drop_column("topics", "sort_order")
    op.drop_column("topics", "suggested_grades")
    op.drop_column("topics", "slug")
    op.drop_column("topics", "icon_url")
    op.drop_column("topics", "description")
