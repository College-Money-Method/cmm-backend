"""SQLAlchemy models for communication templates (standalone educational email templates)."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, Integer, Text
from sqlalchemy.dialects.postgresql import TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from src.db.base import Base


class CommunicationTemplate(Base):
    __tablename__ = "communication_templates"

    id:              Mapped[uuid.UUID]       = mapped_column(primary_key=True, default=uuid.uuid4)
    name:            Mapped[str]             = mapped_column(Text, nullable=False)
    description:     Mapped[str | None]      = mapped_column(Text, nullable=True)
    subject:         Mapped[str | None]      = mapped_column(Text, nullable=True)
    format:          Mapped[str]             = mapped_column(Text, nullable=False, default="rich_text", server_default="rich_text")
    # format: "rich_text" | "google_docs"
    content:         Mapped[str | None]      = mapped_column(Text, nullable=True)   # Tiptap JSON
    google_docs_url: Mapped[str | None]      = mapped_column(Text, nullable=True)
    is_active:       Mapped[bool]            = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    sort_order:      Mapped[int]             = mapped_column(Integer, nullable=False, default=0, server_default="0")
    created_at:      Mapped[datetime]        = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())
    updated_at:      Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True, onupdate=func.now())
