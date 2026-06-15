"""Pydantic schemas for communication template API requests and responses."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict


class CommunicationTemplateOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    description: str | None
    subject: str | None
    format: str
    content: str | None
    google_docs_url: str | None
    is_active: bool
    sort_order: int
    created_at: datetime
    updated_at: datetime | None


class CommunicationTemplateCreate(BaseModel):
    name: str
    description: str | None = None
    subject: str | None = None
    format: str = "rich_text"
    content: str | None = None
    google_docs_url: str | None = None
    sort_order: int = 0


class CommunicationTemplateUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    subject: str | None = None
    format: str | None = None
    content: str | None = None
    google_docs_url: str | None = None
    is_active: bool | None = None
    sort_order: int | None = None
