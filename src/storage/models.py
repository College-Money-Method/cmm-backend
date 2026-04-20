"""SQLAlchemy model for the storage_files registry."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, Text, UniqueConstraint, Uuid
from sqlalchemy.dialects.postgresql import TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from src.db.base import Base


class StorageFile(Base):
    __tablename__ = "storage_files"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    s3_key: Mapped[str] = mapped_column(Text, nullable=False)
    s3_url: Mapped[str] = mapped_column(Text, nullable=False)
    original_filename: Mapped[str] = mapped_column(Text, nullable=False)
    extension: Mapped[str | None] = mapped_column(Text, nullable=True)
    mime_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    file_size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now()
    )

    __table_args__ = (UniqueConstraint("s3_key", name="uq_storage_files_s3_key"),)
