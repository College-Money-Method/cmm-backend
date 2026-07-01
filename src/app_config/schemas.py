"""Pydantic schemas for the global app config API."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel


class AppConfigUpdate(BaseModel):
    """PATCH payload — all fields optional. Pass null to clear a value."""

    welcome_video_embed_code: str | None = None
    welcome_video_title: str | None = None
    welcome_video_caption: str | None = None
    survey_enabled: bool | None = None


class AppConfigOut(BaseModel):
    id: uuid.UUID
    welcome_video_embed_code: str | None
    welcome_video_title: str | None
    welcome_video_caption: str | None
    survey_enabled: bool
    updated_at: datetime | None

    model_config = {"from_attributes": True}
