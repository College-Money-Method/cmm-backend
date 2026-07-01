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

    # Feature flags
    survey_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true", default=True)

    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
