"""add content_translations cache table

Stores Bedrock-generated translations of content entities (topic, page, asset).
Keyed by (entity_type, entity_key, locale) with a source_hash for cache
invalidation when source content changes.

Revision ID: 0085
Revises: 0084
Create Date: 2026-07-27
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0085"
down_revision: Union[str, None] = "0084"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "content_translations",
        sa.Column("id", sa.Uuid(), nullable=False, server_default=sa.text("gen_random_uuid()")),
        sa.Column("entity_type", sa.Text(), nullable=False),
        sa.Column("entity_key", sa.Text(), nullable=False),
        sa.Column("locale", sa.Text(), nullable=False),
        sa.Column("translated_fields", JSONB(), nullable=False),
        sa.Column("source_hash", sa.Text(), nullable=False),
        sa.Column("model_id", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "entity_type", "entity_key", "locale",
            name="uq_content_translations_entity_locale",
        ),
    )
    op.create_index(
        "idx_content_translations_entity_locale",
        "content_translations",
        ["entity_type", "entity_key", "locale"],
    )


def downgrade() -> None:
    op.drop_index("idx_content_translations_entity_locale", table_name="content_translations")
    op.drop_table("content_translations")
