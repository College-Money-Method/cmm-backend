"""SQLAlchemy model for search analytics logging."""

import uuid
from datetime import datetime

from sqlalchemy import Integer, String, Text, TIMESTAMP
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.db.base import Base


class SearchLog(Base):
    __tablename__ = "search_logs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    school_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    school_slug: Mapped[str | None] = mapped_column(Text, nullable=True)
    search_type: Mapped[str] = mapped_column(String(50), nullable=False)
    query: Mapped[str | None] = mapped_column(Text, nullable=True)
    grade: Mapped[int | None] = mapped_column(Integer, nullable=True)
    category_ids: Mapped[list[uuid.UUID] | None] = mapped_column(ARRAY(UUID(as_uuid=True)), nullable=True)
    asset_buckets: Mapped[list[str] | None] = mapped_column(ARRAY(Text), nullable=True)
    results_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    searched_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default="NOW()")
