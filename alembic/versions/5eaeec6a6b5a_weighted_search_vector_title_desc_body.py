"""Weighted search vectors: title=A, description=B, body=D.

Improves relevance by giving title/name matches (weight A) much higher
ts_rank than description matches (weight B) and body-text matches (weight D).
Re-run scripts/backfill_search_text.py after this migration so that
search_text is repopulated (body-only) and each trigger rebuilds the vector.

Revision ID: 5eaeec6a6b5a
Revises: fdc3753a8fc7
Create Date: 2026-05-19
"""

from __future__ import annotations

from alembic import op

revision = "5eaeec6a6b5a"
down_revision = "fdc3753a8fc7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE OR REPLACE FUNCTION topics_search_vector_update() RETURNS trigger AS $$
        BEGIN
            NEW.search_vector :=
                setweight(to_tsvector('english', coalesce(NEW.title, '')), 'A') ||
                setweight(to_tsvector('english', coalesce(NEW.description, '')), 'B') ||
                to_tsvector('english', coalesce(NEW.search_text, ''));
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
    """)

    op.execute("""
        CREATE OR REPLACE FUNCTION workshops_search_vector_update() RETURNS trigger AS $$
        BEGIN
            NEW.search_vector :=
                setweight(to_tsvector('english', coalesce(NEW.name, '')), 'A') ||
                setweight(to_tsvector('english', coalesce(NEW.description, '')), 'B') ||
                to_tsvector('english', coalesce(NEW.search_text, ''));
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
    """)

    op.execute("""
        CREATE OR REPLACE FUNCTION content_assets_search_vector_update() RETURNS trigger AS $$
        BEGIN
            NEW.search_vector :=
                setweight(to_tsvector('english', coalesce(NEW.name, '')), 'A') ||
                setweight(to_tsvector('english', coalesce(NEW.description, '')), 'B') ||
                to_tsvector('english', coalesce(NEW.search_text, ''));
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
    """)


def downgrade() -> None:
    # Restore original flat (unweighted) triggers
    for table in ("topics", "workshops", "content_assets"):
        fn = f"{table}_search_vector_update"
        op.execute(f"""
            CREATE OR REPLACE FUNCTION {fn}() RETURNS trigger AS $$
            BEGIN
                IF NEW.search_text IS NOT NULL THEN
                    NEW.search_vector := to_tsvector('english', NEW.search_text);
                END IF;
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql;
        """)
