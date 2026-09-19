"""SQLAlchemy model for ``webinar_video_jobs`` — one row per Zoom recording.

See ``states.py`` for what the ``state`` column may hold and how it may change.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, ForeignKey, Index, Integer, Numeric, Text, Uuid, text
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from src.db.base import Base
from src.video_pipeline.states import JobState

if TYPE_CHECKING:
    from src.workshops.models import Webinar


class WebinarVideoJob(Base):
    __tablename__ = "webinar_video_jobs"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    # Null for an audit run, which has no webinar to publish to. A placeholder
    # webinar would be worse than a null: it would appear in every admin list
    # that reads the table as a session that never happened.
    webinar_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("webinars.id", ondelete="CASCADE"), nullable=True
    )
    # Idempotency key: Zoom's per-instance recording UUID. The UNIQUE constraint
    # is what makes webhook replay and the reconcile sweep safe — never relax it
    # to a plain index. A source that is not a Zoom recording carries a
    # synthetic key here rather than being allowed to skip it.
    zoom_recording_uuid: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    # Set when the source was a download URL rather than a Zoom recording.
    # Recorded, not re-derived: it is the only account of where the bytes came
    # from, and a presigned URL is expired by the time anyone reads it back.
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    # An operator-supplied transcript, which takes precedence over Zoom's own.
    # A pasted download URL carries no captions with it, and without a
    # transcript the trim falls back to silence detection and the chapters come
    # from frames alone — so for a URL source this is the difference between an
    # audit run that exercises the real chaptering path and one that does not.
    transcript_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    # An audit run stops after Vimeo: it never writes
    # `webinars.video_embed_code` and never deletes the Zoom recording, so
    # auditing what the pipeline produces cannot change what families see.
    audit_only: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    state: Mapped[str] = mapped_column(
        Text, nullable=False, default=JobState.PENDING.value, server_default=JobState.PENDING.value
    )
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    ecs_task_arn: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Fractional seconds — the trim point comes from a caption cue timestamp.
    trim_offset_seconds: Mapped[Decimal | None] = mapped_column(Numeric(10, 3), nullable=True)
    source_duration_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Wall-clock instant the Zoom recording started, straight from Zoom's
    # `start_time`. It is the origin the transcript's own clock hangs off:
    # `recording_start + trim_offset_seconds + cue_start` is when a line was
    # actually spoken. Nothing else supplies it — the webinar's scheduled start
    # is not it (the host opens the room early) and Zoom's "actual start time"
    # in the UI report disagrees with both. Nullable: rows created before this
    # column existed have no way to learn it.
    recording_start: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    vimeo_video_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    vimeo_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Read back from Vimeo rather than string-built: whether an embed-only video
    # carries a privacy hash is Vimeo's decision, not ours.
    vimeo_player_embed_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    # S3 prefix of the untrimmed source. Recorded, not derived — a path
    # convention that drifts turns a retryable job into an unretryable one.
    archive_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    # When the lifecycle rule removes that source. Past it, retry cannot work.
    archive_expires_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    # S3 prefix holding candidate frames plus candidates.json and transcript.json.
    frames_prefix: Mapped[str | None] = mapped_column(Text, nullable=True)
    # True when the model's trim point was rejected and the first cue was used.
    # Not a failure, so it never alerts — but it is worth a human glance.
    trim_fallback_used: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    # True when the chapter list hit the cap and was cut short.
    chapters_truncated: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    # `[{"timecode": <seconds>, "title": ..., "source": ..., "confidence": ...}]`.
    # `timecode` and `title` are exactly what was published to Vimeo; `source`
    # names the rule that produced the chapter and `confidence` the transcript
    # cross-check, both for the admin view and neither sent upstream.
    chapters: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    # `[{"stage": ..., "at": <iso8601>}]`, appended as the pipeline crosses each
    # boundary. See `stage_progress.py` for the vocabulary and why the current
    # stage is derived from this list rather than stored beside it.
    stage_events: Mapped[list] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'")
    )
    # Translated captions are made after the replay is live, so their progress
    # cannot live in `state` — `published` is terminal, and it is terminal
    # because it means a school's page has a working player on it. See
    # `caption_task.py` for the vocabulary these four hold.
    captions_state: Mapped[str] = mapped_column(
        Text, nullable=False, default="pending", server_default="pending"
    )
    captions_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    captions_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    captions_completed_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Stamped when the ops alert for the CURRENT failure has been sent. Cleared
    # on retry, so failing twice alerts twice while re-reading the same failed
    # row on every sweep alerts once.
    failed_notified_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    webinar: Mapped[Webinar | None] = relationship("Webinar")

    __table_args__ = (
        Index("idx_webinar_video_jobs_state", "state"),
        Index("idx_webinar_video_jobs_webinar_id", "webinar_id"),
    )

    @property
    def job_state(self) -> JobState:
        """``state`` as the enum, for transition checks."""
        return JobState(self.state)
