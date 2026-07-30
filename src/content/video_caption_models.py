"""ORM model for the Video CC registry — which videos have translated captions.

One row per Vimeo video, upserted on every job, so the admin page can answer
"has this been done, when, and into what?" without calling Vimeo.

Per-language results live in a JSONB map rather than a child table: they are
always read and written as a whole with their video, never queried across rows,
and the set of languages is open-ended.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Integer, Text, UniqueConstraint, Uuid
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from src.db.base import Base


class VideoCaptionRecord(Base):
    """A video whose captions have been translated at least once.

    ``translations`` maps locale → result, e.g.::

        {"es": {"language": "Spanish", "vimeo_code": "es",
                "track_uri": "/videos/1/texttracks/2", "cue_count": 90,
                "missing_cues": 0, "s3_key": "video-cc/1/es-....vtt",
                "replaced": true, "translated_at": "2026-07-30T06:12:00+00:00"}}
    """

    __tablename__ = "video_caption_records"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)

    # Numeric Vimeo id WITHOUT the privacy hash — the stable identity of a video.
    # The hash is stored separately because it is an access credential, not part
    # of the id, and can be rotated on Vimeo without the video changing.
    video_id: Mapped[str] = mapped_column(Text, nullable=False)
    privacy_hash: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Vimeo metadata, refreshed on each run so a renamed video stays accurate.
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    vimeo_created_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    duration_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Where the English source came from: "vimeo" (existing track) | "upload".
    source_origin: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_cue_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # S3 key of the archived English source transcript.
    source_s3_key: Mapped[str | None] = mapped_column(Text, nullable=True)

    # locale → result map; see class docstring.
    translations: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default="{}"
    )

    # Outcome of the most recent run: "completed" | "failed".
    last_status: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_run_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    __table_args__ = (
        UniqueConstraint("video_id", name="uq_video_caption_records_video_id"),
    )
