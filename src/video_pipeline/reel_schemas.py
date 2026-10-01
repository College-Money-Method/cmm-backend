"""Request and response shapes for the trailer reel endpoints."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class VideoReelCreate(BaseModel):
    # Direction for the first cut, e.g. "Focus on merit aid".
    prompt: str | None = Field(None, max_length=500)


class VideoReelMessageCreate(BaseModel):
    # What the admin wants changed, e.g. "Swap clip 2 for the FAFSA deadline part".
    text: str = Field(..., min_length=1, max_length=1000)


class VideoReelDraftEdit(BaseModel):
    """A change made by hand, no model call. Either field may be left out."""

    hook_title: str | None = Field(None, min_length=1, max_length=80)
    # Indexes into the current clips, in the new play order. Leaving one out
    # removes it; each may appear once.
    segment_order: list[int] | None = Field(None, min_length=1)


class VideoReelRender(BaseModel):
    orientation: Literal["landscape", "portrait"]


class ReelLine(BaseModel):
    """One sentence of a clip, on the replay's clock."""

    start: float
    end: float
    text: str


class ReelSegment(BaseModel):
    first_cue: int
    last_cue: int
    start: float
    end: float
    duration: float
    why: str = ""
    flags: list[str] = []
    lines: list[ReelLine] = []


class ReelSelection(BaseModel):
    hook_title: str
    total_seconds: float
    segments: list[ReelSegment]


class ReelMessage(BaseModel):
    role: Literal["admin", "assistant", "edit"]
    text: str
    at: datetime


class VideoReel(BaseModel):
    id: uuid.UUID
    job_id: uuid.UUID
    # Null while the reel is a draft.
    orientation: str | None = None
    prompt: str | None = None
    state: str
    stage: str | None = None
    hook_title: str | None = None
    duration_seconds: float | None = None
    # The clips the reel is (or will be) cut from; null before the first cut.
    selection: ReelSelection | None = None
    messages: list[ReelMessage] = []
    # Length rules the selection breaks after hand edits. Warnings, not refusals.
    problems: list[str] = []
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
