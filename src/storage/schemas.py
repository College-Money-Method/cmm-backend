"""Pydantic schemas for storage_files."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel


class StorageFileOut(BaseModel):
    model_config = {"from_attributes": True}

    id: uuid.UUID
    s3_key: str
    s3_url: str
    original_filename: str
    extension: str | None
    mime_type: str | None
    file_size_bytes: int | None
    created_at: datetime
