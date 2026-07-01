"""SQLAlchemy model for user survey/feedback responses.

Each row is one submitted response — a thumbs rating, star rating, or text
comment — tied to a specific page type (resource, topic, workshop, hub_resource).
The posthog_distinct_id bridges this record to PostHog heatmap session data.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, Integer, Text, Uuid
from sqlalchemy.dialects.postgresql import TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from src.db.base import Base


class SurveyResponse(Base):
    __tablename__ = "survey_responses"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())

    # Page context
    page_type: Mapped[str] = mapped_column(Text, nullable=False)   # resource | topic | workshop | hub_resource
    page_url: Mapped[str] = mapped_column(Text, nullable=False)
    resource_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    resource_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    school_id: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Question definition (stored so the admin view is self-contained)
    question_type: Mapped[str] = mapped_column(Text, nullable=False)   # thumbs | stars | text
    question_text: Mapped[str] = mapped_column(Text, nullable=False)

    # Exactly one rating field is populated per response
    rating_thumbs: Mapped[bool | None] = mapped_column(Boolean, nullable=True)   # True = up, False = down
    rating_stars: Mapped[int | None] = mapped_column(Integer, nullable=True)     # 1–5
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Identity — bridges to PostHog heatmap session data
    posthog_distinct_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    user_id: Mapped[str | None] = mapped_column(Text, nullable=True)   # populated for authenticated hub users
