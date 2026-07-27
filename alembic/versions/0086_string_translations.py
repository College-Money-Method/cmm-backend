"""add string_translations cache table

Per-string translation cache for site-wide DOM string translation. Keyed by
(content_hash, locale) so every unique visible string is translated once per
locale and reused across all pages.

Revision ID: 0086
Revises: 0085
Create Date: 2026-07-27
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0086"
down_revision: Union[str, None] = "0085"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "string_translations",
        sa.Column("id", sa.Uuid(), nullable=False, server_default=sa.text("gen_random_uuid()")),
        sa.Column("content_hash", sa.Text(), nullable=False),
        sa.Column("locale", sa.Text(), nullable=False),
        sa.Column("source_text", sa.Text(), nullable=False),
        sa.Column("translated_text", sa.Text(), nullable=False),
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
            "content_hash", "locale",
            name="uq_string_translations_hash_locale",
        ),
    )
    op.create_index(
        "idx_string_translations_hash_locale",
        "string_translations",
        ["content_hash", "locale"],
    )


def downgrade() -> None:
    op.drop_index("idx_string_translations_hash_locale", table_name="string_translations")
    op.drop_table("string_translations")
