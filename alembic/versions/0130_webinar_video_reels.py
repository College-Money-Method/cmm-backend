"""Trailer reels cut from a webinar video job

One row per ~60 second reel an admin asks for: its orientation, the extra
direction given to the clip picker, where the render stands, the finished file
in S3 and, once uploaded, its Vimeo video.

Revision ID: 0130
Revises: 0129
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0130"
down_revision = "0129"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "webinar_video_reels",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "job_id",
            sa.Uuid(),
            sa.ForeignKey("webinar_video_jobs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("orientation", sa.Text(), nullable=False),
        sa.Column("prompt", sa.Text(), nullable=True),
        sa.Column("state", sa.Text(), nullable=False, server_default="pending"),
        sa.Column("stage", sa.Text(), nullable=True),
        sa.Column("ecs_task_arn", sa.Text(), nullable=True),
        sa.Column("hook_title", sa.Text(), nullable=True),
        sa.Column("duration_seconds", sa.Numeric(8, 2), nullable=True),
        sa.Column("s3_key", sa.Text(), nullable=True),
        sa.Column("vimeo_video_id", sa.Text(), nullable=True),
        sa.Column("vimeo_hash", sa.Text(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            postgresql.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index("idx_webinar_video_reels_job_id", "webinar_video_reels", ["job_id"])
    # One reel in flight per job: concurrent requests cannot both launch a render.
    op.create_index(
        "uq_webinar_video_reels_one_active_per_job",
        "webinar_video_reels",
        ["job_id"],
        unique=True,
        postgresql_where=sa.text("state IN ('pending', 'rendering')"),
    )


def downgrade() -> None:
    op.drop_index("uq_webinar_video_reels_one_active_per_job", table_name="webinar_video_reels")
    op.drop_index("idx_webinar_video_reels_job_id", table_name="webinar_video_reels")
    op.drop_table("webinar_video_reels")
