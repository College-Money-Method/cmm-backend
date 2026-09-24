"""Request and response shapes for the trailer reel endpoints."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class VideoReelCreate(BaseModel):
    orientation: Literal["landscape", "portrait"]
    # Extra direction for the clip picker, e.g. "Focus on merit aid".
    prompt: str | None = Field(None, max_length=500)


class VideoReel(BaseModel):
    id: uuid.UUID
    job_id: uuid.UUID
    orientation: str
    prompt: str | None = None
    state: str
    stage: str | None = None
    hook_title: str | None = None
    duration_seconds: float | None = None
    # Presigned GET of the finished mp4; null until the reel is ready.
    preview_url: str | None = None
    preview_expires_in: int | None = None
    vimeo_video_id: str | None = None
    vimeo_url: str | None = None
    error: str | None = None
    created_at: datetime
    updated_at: datetime


class VideoReelList(BaseModel):
    items: list[VideoReel]
    # Why no new reel can be made of this job, or null when one can.
    blocked_reason: str | None = None
