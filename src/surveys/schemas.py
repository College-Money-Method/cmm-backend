"""Pydantic schemas for the surveys API."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, field_validator


class SurveyResponseCreate(BaseModel):
    """POST body — submitted by the frontend widget."""

    page_type: str
    page_url: str
    resource_id: str | None = None
    resource_name: str | None = None
    school_id: str | None = None

    question_type: str   # thumbs | stars | text
    question_text: str

    # Exactly one of these should be set per response
    rating_thumbs: bool | None = None
    rating_stars: int | None = None
    comment: str | None = None

    # PostHog session bridge — provided by the client via posthog.get_distinct_id()
    posthog_distinct_id: str | None = None

    @field_validator("rating_stars")
    @classmethod
    def validate_stars(cls, v: int | None) -> int | None:
        if v is not None and not (1 <= v <= 5):
            raise ValueError("rating_stars must be between 1 and 5")
        return v

    @field_validator("question_type")
    @classmethod
    def validate_question_type(cls, v: str) -> str:
        allowed = {"thumbs", "stars", "text"}
        if v not in allowed:
            raise ValueError(f"question_type must be one of {allowed}")
        return v


class SurveyResponseOut(BaseModel):
    """Single survey response — returned by GET list."""

    id: uuid.UUID
    created_at: datetime
    page_type: str
    page_url: str
    resource_id: str | None
    resource_name: str | None
    school_id: str | None
    question_type: str
    question_text: str
    rating_thumbs: bool | None
    rating_stars: int | None
    comment: str | None
    posthog_distinct_id: str | None
    user_id: str | None

    model_config = {"from_attributes": True}


class SurveyListResponse(BaseModel):
    """Paginated list of survey responses."""

    items: list[SurveyResponseOut]
    total: int
    skip: int
    limit: int


class SurveyPageTypeSummary(BaseModel):
    """Aggregated stats for a single page type."""

    page_type: str
    total: int
    thumbs_up: int
    thumbs_down: int
    thumbs_up_pct: float | None   # None when no thumbs responses
    avg_stars: float | None       # None when no star responses
    with_comment: int


class SurveySummaryResponse(BaseModel):
    """Overall summary + per-page-type breakdown — for the admin dashboard."""

    total: int
    by_page_type: list[SurveyPageTypeSummary]
