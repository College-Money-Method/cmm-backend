"""video caption records — registry of videos with translated captions

Backs the Video CC admin list: which videos have been processed, when, into
which languages, and where the archived transcripts live in S3.

Revision ID: 0092
Revises: 0091
Create Date: 2026-07-30
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0092"
down_revision: Union[str, None] = "0091"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "video_caption_records",
        sa.Column("id", sa.Uuid(), primary_key=True),
        # Numeric Vimeo id without the privacy hash — a video's stable identity.
        sa.Column("video_id", sa.Text(), nullable=False),
        sa.Column("privacy_hash", sa.Text(), nullable=True),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("vimeo_created_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("duration_seconds", sa.Integer(), nullable=True),
        sa.Column("source_origin", sa.Text(), nullable=True),
        sa.Column("source_name", sa.Text(), nullable=True),
        sa.Column("source_cue_count", sa.Integer(), nullable=True),
        sa.Column("source_s3_key", sa.Text(), nullable=True),
        # locale -> {language, vimeo_code, track_uri, cue_count, missing_cues,
        #            s3_key, replaced, translated_at}
        sa.Column(
            "translations",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="{}",
        ),
        sa.Column("last_status", sa.Text(), nullable=True),
        sa.Column("last_run_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint("video_id", name="uq_video_caption_records_video_id"),
    )
    # The list is ordered most-recently-processed first.
    op.create_index(
        "idx_video_caption_records_last_run_at",
        "video_caption_records",
        ["last_run_at"],
    )


def downgrade() -> None:
    op.drop_index("idx_video_caption_records_last_run_at", table_name="video_caption_records")
    op.drop_table("video_caption_records")
