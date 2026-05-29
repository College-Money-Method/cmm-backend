"""Add content asset tables: asset_types, objectives, content_assets, and join tables.

@05
@04
Create Date: 2026-03-22
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── asset_types ──────────────────────────────────────────────────────────
    op.create_table(
        "asset_types",
        sa.Column(
            "id",
            sa.Uuid(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("airtable_id", sa.Text(), nullable=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("color", sa.Text(), nullable=True),
        sa.Column("icon_url", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("airtable_id", name="uq_asset_types_airtable_id"),
        sa.UniqueConstraint("name", name="uq_asset_types_name"),
    )
    op.create_index("idx_asset_types_airtable_id", "asset_types", ["airtable_id"])

    # ── objectives ───────────────────────────────────────────────────────────
    op.create_table(
        "objectives",
        sa.Column(
            "id",
            sa.Uuid(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("airtable_id", sa.Text(), nullable=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("airtable_id", name="uq_objectives_airtable_id"),
    )
    op.create_index("idx_objectives_airtable_id", "objectives", ["airtable_id"])

    # ── objective_workshops (M:M objectives ↔ workshops) ─────────────────────
    op.create_table(
        "objective_workshops",
        sa.Column("objective_id", sa.Uuid(), nullable=False),
        sa.Column("workshop_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["objective_id"],
            ["objectives.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workshop_id"],
            ["workshops.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("objective_id", "workshop_id"),
    )
    op.create_index(
        "idx_objective_workshops_workshop_id",
        "objective_workshops",
        ["workshop_id"],
    )

    # ── content_assets ───────────────────────────────────────────────────────
    op.create_table(
        "content_assets",
        sa.Column(
            "id",
            sa.Uuid(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("airtable_id", sa.Text(), nullable=True),
        sa.Column("asset_type_id", sa.Uuid(), nullable=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("content", sa.Text(), nullable=True),
        sa.Column("link", sa.Text(), nullable=True),
        sa.Column("embed_code", sa.Text(), nullable=True),
        sa.Column("image_url", sa.Text(), nullable=True),
        sa.Column("file_url", sa.Text(), nullable=True),
        sa.Column(
            "is_featured",
            sa.Boolean(),
            nullable=False,
            server_default="false",
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["asset_type_id"],
            ["asset_types.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("airtable_id", name="uq_content_assets_airtable_id"),
    )
    op.create_index(
        "idx_content_assets_airtable_id", "content_assets", ["airtable_id"]
    )
    op.create_index(
        "idx_content_assets_asset_type_id", "content_assets", ["asset_type_id"]
    )

    # ── content_asset_objectives (M:M content_assets ↔ objectives) ───────────
    op.create_table(
        "content_asset_objectives",
        sa.Column("content_asset_id", sa.Uuid(), nullable=False),
        sa.Column("objective_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["content_asset_id"],
            ["content_assets.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["objective_id"],
            ["objectives.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("content_asset_id", "objective_id"),
    )
    op.create_index(
        "idx_content_asset_objectives_objective_id",
        "content_asset_objectives",
        ["objective_id"],
    )

    # ── content_asset_workshops (M:M content_assets ↔ workshops) ─────────────
    op.create_table(
        "content_asset_workshops",
        sa.Column("content_asset_id", sa.Uuid(), nullable=False),
        sa.Column("workshop_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["content_asset_id"],
            ["content_assets.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workshop_id"],
            ["workshops.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("content_asset_id", "workshop_id"),
    )
    op.create_index(
        "idx_content_asset_workshops_workshop_id",
        "content_asset_workshops",
        ["workshop_id"],
    )


def downgrade() -> None:
    op.drop_table("content_asset_workshops")
    op.drop_table("content_asset_objectives")
    op.drop_table("content_assets")
    op.drop_table("objective_workshops")
    op.drop_table("objectives")
    op.drop_table("asset_types")
