"""embeddable calculators

Backs the embeddable calculator platform that replaces Calculator Studio.

Each row is an admin-authored HTML/CSS/JS document (``html``) plus a JSON
payload of type-specific data (``config``). The markup is stored unsanitized by
design — it is first-party super_admin content and every render surface isolates
it, so stripping its <script> would remove the calculator itself.

``config`` and ``embed_allowed_origins`` are JSONB rather than Text/ARRAY:
JSONB already has a SQLite compile shim in the test suite, while ARRAY has no
SQLite equivalent at all.

Revision ID: 0105
Revises: 0104
Create Date: 2026-08-20
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0105"
down_revision: Union[str, None] = "0104"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "calculators",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("slug", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("html", sa.Text(), nullable=True),
        sa.Column("deps", sa.Text(), nullable=True),
        sa.Column(
            "config",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="{}",
            nullable=False,
        ),
        sa.Column(
            "embed_allowed_origins",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="[]",
            nullable=False,
        ),
        sa.Column("meta_title", sa.Text(), nullable=True),
        sa.Column("meta_description", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), server_default="draft", nullable=False),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("updated_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("slug"),
    )
    op.create_index("idx_calculators_slug", "calculators", ["slug"])
    op.create_index("idx_calculators_status", "calculators", ["status"])
    op.create_index("idx_calculators_type", "calculators", ["type"])


def downgrade() -> None:
    op.drop_index("idx_calculators_type", table_name="calculators")
    op.drop_index("idx_calculators_status", table_name="calculators")
    op.drop_index("idx_calculators_slug", table_name="calculators")
    op.drop_table("calculators")
