"""SQLAlchemy model for the global application config (singleton).

A single-row table holding site-wide settings that are not scoped to any
school — currently the global "welcome video" shown on school home pages.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, Text, Uuid
from sqlalchemy.dialects.postgresql import TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from src.db.base import Base


class AppConfig(Base):
    __tablename__ = "app_config"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)

    # Global welcome video — a Vimeo iframe embed code (same convention as
    # workshops/content video_embed_code). Null = no video configured.
    welcome_video_embed_code: Mapped[str | None] = mapped_column(Text)
    welcome_video_title: Mapped[str | None] = mapped_column(Text)
    welcome_video_caption: Mapped[str | None] = mapped_column(Text)

    # Global "topic overview" video shown on the school topics/journey page.
    # A plain video URL (e.g. Vimeo/YouTube link). Null = no video configured
    # (the "Watch video overview" button is hidden).
    topic_overview_video_url: Mapped[str | None] = mapped_column(Text)

    # Feature flags
    survey_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true", default=True)

    # Email sandbox mode. When True, outbound email only reaches recipients on
    # the team domain (collegemoneymethod.com); every other recipient is logged
    # (status="sandboxed") but never sent — on ANY environment. Off = normal
    # production sending to real recipients. Default False; typically turned on
    # in local/dev and left off in production.
    email_sandbox_mode: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false", default=False
    )

    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
