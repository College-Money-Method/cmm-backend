"""Pydantic schemas for the global app config API."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel

from src.schools.display_timezone import DisplayTimezoneField


class AppConfigUpdate(BaseModel):
    """PATCH payload — all fields optional. Pass null to clear a value."""

    welcome_video_embed_code: str | None = None
    welcome_video_title: str | None = None
    welcome_video_caption: str | None = None
    topic_overview_video_url: str | None = None
    # Blank clears the app-wide default, falling back to the env seed.
    workshop_display_timezone: DisplayTimezoneField = None
    survey_enabled: bool | None = None
    email_sandbox_mode: bool | None = None


class AppConfigOut(BaseModel):
    id: uuid.UUID
    welcome_video_embed_code: str | None
    welcome_video_title: str | None
    welcome_video_caption: str | None
    topic_overview_video_url: str | None
    workshop_display_timezone: str | None
    survey_enabled: bool
    email_sandbox_mode: bool
    updated_at: datetime | None

    model_config = {"from_attributes": True}
