"""Add search_text and search_vector columns for global search.

Revision ID: t8u9v0w1x2y3
Revises: s7t8u9v0w1x2
Create Date: 2026-04-17
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import TSVECTOR

# revision identifiers, used by Alembic.
revision = "t8u9v0w1x2y3"
down_revision = "s7t8u9v0w1x2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── topics ──────────────────────────────────────────────────────────────
    op.add_column("topics", sa.Column("search_text", sa.Text(), nullable=True))
    op.add_column("topics", sa.Column("search_vector", TSVECTOR(), nullable=True))

    op.execute("""
        CREATE OR REPLACE FUNCTION topics_search_vector_update() RETURNS trigger AS $$
        BEGIN
            IF NEW.search_text IS NOT NULL THEN
                NEW.search_vector := to_tsvector('english', NEW.search_text);
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
    """)
    op.execute("""
        CREATE TRIGGER topics_search_vector_trig
            BEFORE INSERT OR UPDATE ON topics
            FOR EACH ROW EXECUTE FUNCTION topics_search_vector_update();
    """)
    op.create_index("idx_topics_search_vector", "topics", ["search_vector"], postgresql_using="gin")

    # ── workshops ────────────────────────────────────────────────────────────
    op.add_column("workshops", sa.Column("search_text", sa.Text(), nullable=True))
    op.add_column("workshops", sa.Column("search_vector", TSVECTOR(), nullable=True))

    op.execute("""
        CREATE OR REPLACE FUNCTION workshops_search_vector_update() RETURNS trigger AS $$
        BEGIN
            IF NEW.search_text IS NOT NULL THEN
                NEW.search_vector := to_tsvector('english', NEW.search_text);
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
    """)
    op.execute("""
        CREATE TRIGGER workshops_search_vector_trig
            BEFORE INSERT OR UPDATE ON workshops
            FOR EACH ROW EXECUTE FUNCTION workshops_search_vector_update();
    """)
    op.create_index("idx_workshops_search_vector", "workshops", ["search_vector"], postgresql_using="gin")

    # ── content_assets ───────────────────────────────────────────────────────
    op.add_column("content_assets", sa.Column("search_text", sa.Text(), nullable=True))
    op.add_column("content_assets", sa.Column("search_vector", TSVECTOR(), nullable=True))

    op.execute("""
        CREATE OR REPLACE FUNCTION content_assets_search_vector_update() RETURNS trigger AS $$
        BEGIN
            IF NEW.search_text IS NOT NULL THEN
                NEW.search_vector := to_tsvector('english', NEW.search_text);
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
    """)
    op.execute("""
        CREATE TRIGGER content_assets_search_vector_trig
            BEFORE INSERT OR UPDATE ON content_assets
            FOR EACH ROW EXECUTE FUNCTION content_assets_search_vector_update();
    """)
    op.create_index("idx_content_assets_search_vector", "content_assets", ["search_vector"], postgresql_using="gin")


def downgrade() -> None:
    # ── content_assets ───────────────────────────────────────────────────────
    op.execute("DROP TRIGGER IF EXISTS content_assets_search_vector_trig ON content_assets;")
    op.execute("DROP FUNCTION IF EXISTS content_assets_search_vector_update();")
    op.drop_index("idx_content_assets_search_vector", table_name="content_assets")
    op.drop_column("content_assets", "search_vector")
    op.drop_column("content_assets", "search_text")

    # ── workshops ────────────────────────────────────────────────────────────
    op.execute("DROP TRIGGER IF EXISTS workshops_search_vector_trig ON workshops;")
    op.execute("DROP FUNCTION IF EXISTS workshops_search_vector_update();")
    op.drop_index("idx_workshops_search_vector", table_name="workshops")
    op.drop_column("workshops", "search_vector")
    op.drop_column("workshops", "search_text")

    # ── topics ──────────────────────────────────────────────────────────────
    op.execute("DROP TRIGGER IF EXISTS topics_search_vector_trig ON topics;")
    op.execute("DROP FUNCTION IF EXISTS topics_search_vector_update();")
    op.drop_index("idx_topics_search_vector", table_name="topics")
    op.drop_column("topics", "search_vector")
    op.drop_column("topics", "search_text")
