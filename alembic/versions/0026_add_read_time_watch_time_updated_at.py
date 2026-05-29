"""add updated_at, read_time_minutes, video_duration_seconds to content_assets

@26
@25
Create Date: 2026-04-17

"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "content_assets",
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=True,
            server_default=sa.text("NOW()"),
        ),
    )
    op.add_column(
        "content_assets",
        sa.Column("read_time_minutes", sa.Integer(), nullable=True),
    )
    op.add_column(
        "content_assets",
        sa.Column("video_duration_seconds", sa.Integer(), nullable=True),
    )

    # Backfill updated_at = created_at for existing rows
    op.execute("UPDATE content_assets SET updated_at = created_at")

    # Trigger to keep updated_at current on every UPDATE
    op.execute("""
        CREATE OR REPLACE FUNCTION set_content_assets_updated_at()
        RETURNS TRIGGER AS $$
        BEGIN
            NEW.updated_at = NOW();
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
    """)
    op.execute("""
        CREATE TRIGGER content_assets_set_updated_at
        BEFORE UPDATE ON content_assets
        FOR EACH ROW EXECUTE FUNCTION set_content_assets_updated_at();
    """)


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS content_assets_set_updated_at ON content_assets")
    op.execute("DROP FUNCTION IF EXISTS set_content_assets_updated_at")
    op.drop_column("content_assets", "video_duration_seconds")
    op.drop_column("content_assets", "read_time_minutes")
    op.drop_column("content_assets", "updated_at")
