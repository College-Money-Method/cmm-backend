"""SQLAlchemy model for ``webinar_video_reels`` — one ~60 second trailer reel of a job.

A reel is cut from a published job's archived recordings by the ECS reel task
(``reel_task``), stored in S3, previewed on the admin screen and, when an admin
says so, uploaded to Vimeo. A job can have any number of them: each is one
orientation and one editorial direction ("Focus on merit aid").
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import ForeignKey, Index, Numeric, Text, Uuid, text
from sqlalchemy.dialects.postgresql import TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from src.db.base import Base

# The whole lifecycle. `pending` is a reel whose task has not been launched
# (ECS not configured, or over capacity); `rendering` one whose task is running.
PENDING, RENDERING, READY, FAILED = "pending", "rendering", "ready", "failed"
ACTIVE_STATES = (PENDING, RENDERING)
ORIENTATIONS = ("landscape", "portrait")
ACTIVE_WHERE = "state IN ('pending', 'rendering')"


class WebinarVideoReel(Base):
    __tablename__ = "webinar_video_reels"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    job_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("webinar_video_jobs.id", ondelete="CASCADE"), nullable=False
    )
    orientation: Mapped[str] = mapped_column(Text, nullable=False)
    # The admin's extra direction for the clip picker, verbatim. Null for none.
    prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    state: Mapped[str] = mapped_column(Text, nullable=False, default=PENDING, server_default=PENDING)
    # The step a rendering reel is on, for the admin screen.
    stage: Mapped[str | None] = mapped_column(Text, nullable=True)
    ecs_task_arn: Mapped[str | None] = mapped_column(Text, nullable=True)
    hook_title: Mapped[str | None] = mapped_column(Text, nullable=True)
    duration_seconds: Mapped[Decimal | None] = mapped_column(Numeric(8, 2), nullable=True)
    # Object key of the finished mp4, set when the reel is ready.
    s3_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    vimeo_video_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    vimeo_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        Index("idx_webinar_video_reels_job_id", "job_id"),
        # One reel in flight per job, enforced by the database: two admins (or
        # one double click) asking at once must not both launch a render.
        Index("uq_webinar_video_reels_one_active_per_job", "job_id", unique=True,
              postgresql_where=text(ACTIVE_WHERE), sqlite_where=text(ACTIVE_WHERE)),
    )
