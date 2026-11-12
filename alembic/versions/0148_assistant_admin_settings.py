"""Assistant settings: per-school counselor assistant switch and admin prompt sections

``schools.counselor_assistant_enabled`` shows the counselor assistant in a
school's Counselor Hub; every school starts off. ``assistant_prompt_sections``
holds admin-written instructions appended to the family and/or counselor
system prompt.

Revision ID: 0148
Revises: 0147
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0148"
down_revision = "0147"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "schools",
        sa.Column("counselor_assistant_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_table(
        "assistant_prompt_sections",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("audience", sa.Text(), nullable=False, server_default="both"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.CheckConstraint(
            "audience IN ('family', 'counselor', 'both')", name="ck_assistant_prompt_sections_audience"
        ),
    )


def downgrade() -> None:
    op.drop_table("assistant_prompt_sections")
    op.drop_column("schools", "counselor_assistant_enabled")
