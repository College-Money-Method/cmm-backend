"""add survey_configs table with seeded defaults

Revision ID: 0073
Revises: 0072
Create Date: 2026-07-02
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0073"
down_revision: Union[str, None] = "0072"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "survey_configs",
        sa.Column("id", sa.Uuid, primary_key=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("page_type", sa.Text, nullable=False),
        sa.Column("question_text", sa.Text, nullable=False),
        sa.Column("question_type", sa.Text, nullable=False),
        sa.Column("comment_prompt", sa.Text, nullable=True),
        sa.Column("trigger_type", sa.Text, nullable=False),
        sa.Column("trigger_value", sa.Integer, nullable=False, server_default="3"),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default="true"),
    )

    # Seed the four defaults that were previously hardcoded in feedback-config.ts.
    op.execute(
        """
        INSERT INTO survey_configs
            (id, name, page_type, question_text, question_type, comment_prompt,
             trigger_type, trigger_value, is_active)
        VALUES
            (gen_random_uuid(), 'Resource Feedback',
             'resource', 'Was this resource helpful?',
             'thumbs', 'What could be better? (optional)',
             'engagement', 3, true),

            (gen_random_uuid(), 'Topic Relevance',
             'topic', 'Were these topics relevant to where you are in the process?',
             'stars', 'Any topics you wish were covered? (optional)',
             'engagement', 3, true),

            (gen_random_uuid(), 'Workshop Experience',
             'workshop', 'How was this workshop for you?',
             'stars', 'Tell us more (optional)',
             'registration', 1, true),

            (gen_random_uuid(), 'Counselor Hub Resource',
             'hub_resource', 'Is this resource meeting your students'' needs?',
             'thumbs', 'What would make it better? (optional)',
             'engagement', 3, true)
        """
    )


def downgrade() -> None:
    op.drop_table("survey_configs")
