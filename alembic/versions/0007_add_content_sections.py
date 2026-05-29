"""Add content sections: summary, action_items, faqs, content_asset_resources, reader_questions.

@07
@06
Create Date: 2026-03-24
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── New columns on content_assets ─────────────────────────────────────────
    op.add_column(
        "content_assets",
        sa.Column("summary", sa.Text(), nullable=True),
    )
    op.add_column(
        "content_assets",
        sa.Column("action_items", JSONB(), nullable=False, server_default="[]"),
    )

    # ── faqs ──────────────────────────────────────────────────────────────────
    op.create_table(
        "faqs",
        sa.Column("id", sa.UUID(), nullable=False, server_default=sa.text("gen_random_uuid()")),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("answer", sa.Text(), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint("id"),
    )

    # ── content_asset_faqs (M:M with sort_order) ──────────────────────────────
    op.create_table(
        "content_asset_faqs",
        sa.Column("content_asset_id", sa.UUID(), nullable=False),
        sa.Column("faq_id", sa.UUID(), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.ForeignKeyConstraint(["content_asset_id"], ["content_assets.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["faq_id"], ["faqs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("content_asset_id", "faq_id"),
    )
    op.create_index("ix_content_asset_faqs_asset", "content_asset_faqs", ["content_asset_id"])

    # ── content_asset_resources (self-join, ordered) ───────────────────────────
    op.create_table(
        "content_asset_resources",
        sa.Column("content_asset_id", sa.UUID(), nullable=False),
        sa.Column("resource_id", sa.UUID(), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.ForeignKeyConstraint(["content_asset_id"], ["content_assets.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["resource_id"], ["content_assets.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("content_asset_id", "resource_id"),
    )
    op.create_index("ix_content_asset_resources_asset", "content_asset_resources", ["content_asset_id"])

    # ── reader_questions ───────────────────────────────────────────────────────
    op.create_table(
        "reader_questions",
        sa.Column("id", sa.UUID(), nullable=False, server_default=sa.text("gen_random_uuid()")),
        sa.Column("content_asset_id", sa.UUID(), nullable=False),
        sa.Column("email", sa.Text(), nullable=False),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="pending"),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("answered_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["content_asset_id"], ["content_assets.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_reader_questions_asset", "reader_questions", ["content_asset_id"])


def downgrade() -> None:
    op.drop_table("reader_questions")
    op.drop_table("content_asset_resources")
    op.drop_table("content_asset_faqs")
    op.drop_table("faqs")
    op.drop_column("content_assets", "action_items")
    op.drop_column("content_assets", "summary")
