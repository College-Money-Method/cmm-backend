"""add survey_responses table

@71
@70
Create Date: 2026-07-01
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0071"
down_revision: Union[str, None] = "0070"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "survey_responses",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("page_type", sa.Text(), nullable=False),
        sa.Column("page_url", sa.Text(), nullable=False),
        sa.Column("resource_id", sa.Text(), nullable=True),
        sa.Column("resource_name", sa.Text(), nullable=True),
        sa.Column("school_id", sa.Text(), nullable=True),
        sa.Column("question_type", sa.Text(), nullable=False),
        sa.Column("question_text", sa.Text(), nullable=False),
        sa.Column("rating_thumbs", sa.Boolean(), nullable=True),
        sa.Column("rating_stars", sa.Integer(), nullable=True),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("posthog_distinct_id", sa.Text(), nullable=True),
        sa.Column("user_id", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_survey_responses_page_type", "survey_responses", ["page_type"])
    op.create_index("ix_survey_responses_school_id", "survey_responses", ["school_id"])
    op.create_index("ix_survey_responses_created_at", "survey_responses", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_survey_responses_created_at", "survey_responses")
    op.drop_index("ix_survey_responses_school_id", "survey_responses")
    op.drop_index("ix_survey_responses_page_type", "survey_responses")
    op.drop_table("survey_responses")
