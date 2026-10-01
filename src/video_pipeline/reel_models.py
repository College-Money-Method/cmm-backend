"""SQLAlchemy model for ``webinar_video_reels`` — one ~60 second trailer reel of a job.

A reel starts as a **draft**: Sonnet picks the clips (``trailer_select``), an
admin reads the merged transcript and refines it in conversation
(``trailer_revise``) or by hand, then asks for it to be rendered. The ECS reel
task (``reel_task``) cuts exactly those clips from the job's archived
recordings, stores the mp4 in S3 for preview and, when an admin says so, it is
uploaded to Vimeo. A job can have any number of reels.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import ForeignKey, Index, Numeric, Text, Uuid, text
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from src.db.base import Base

# The whole lifecycle. `draft` is a reel an admin is still reviewing; `drafting`
# one whose next clip selection the model is working on. `pending` is a reel
# whose task has not been launched (ECS not configured, or over capacity);
# `rendering` one whose task is running.
DRAFT, DRAFTING = "draft", "drafting"
PENDING, RENDERING, READY, FAILED = "pending", "rendering", "ready", "failed"
DRAFT_STATES = (DRAFT, DRAFTING)
# Renders in flight. Drafts are not: they cost one Bedrock call a turn, not a
# Fargate task, so any number may be open while a reel renders.
ACTIVE_STATES = (PENDING, RENDERING)
ORIENTATIONS = ("landscape", "portrait")
ACTIVE_WHERE = "state IN ('pending', 'rendering')"


class WebinarVideoReel(Base):
    __tablename__ = "webinar_video_reels"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    job_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("webinar_video_jobs.id", ondelete="CASCADE"), nullable=False
    )
    # Chosen when the draft is sent to render; null while it is a draft.
    orientation: Mapped[str | None] = mapped_column(Text, nullable=True)
    # The admin's direction for the first cut, verbatim. Null for none.
    prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    # The clips to cut, ``trailer_select.Selection.as_dict()``. Set by the first
    # model turn, changed by each revision or edit, fixed once rendering starts.
    selection: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # The review conversation, oldest first: ``{"role": "admin" | "assistant" |
    # "edit", "text": str, "at": iso8601}``. `edit` records a change made by hand.
    messages: Mapped[list | None] = mapped_column(JSONB, nullable=True)
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
