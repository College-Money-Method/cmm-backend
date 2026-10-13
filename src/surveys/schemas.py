"""Pydantic schemas for the surveys API."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field, computed_field, field_validator

from src.surveys.actionable import is_actionable


_PAGE_TYPES = {"resource", "topic", "workshop", "general", "hub_general"}
_TRIGGER_TYPES = {"engagement", "time", "clicks", "registration"}
_QUESTION_TYPES = {"thumbs", "stars", "text"}


class SurveyConfigCreate(BaseModel):
    name: str
    page_type: str
    question_text: str
    question_type: str   # immutable after creation
    comment_prompt: str | None = None
    trigger_type: str
    trigger_value: int = 3

    @field_validator("page_type")
    @classmethod
    def validate_page_type(cls, v: str) -> str:
        if v not in _PAGE_TYPES:
            raise ValueError(f"page_type must be one of {_PAGE_TYPES}")
        return v

    @field_validator("question_type")
    @classmethod
    def validate_config_question_type(cls, v: str) -> str:
        if v not in _QUESTION_TYPES:
            raise ValueError(f"question_type must be one of {_QUESTION_TYPES}")
        return v

    @field_validator("trigger_type")
    @classmethod
    def validate_trigger_type(cls, v: str) -> str:
        if v not in _TRIGGER_TYPES:
            raise ValueError(f"trigger_type must be one of {_TRIGGER_TYPES}")
        return v

    @field_validator("trigger_value")
    @classmethod
    def validate_trigger_value(cls, v: int) -> int:
        if v < 1:
            raise ValueError("trigger_value must be at least 1")
        return v


class SurveyConfigUpdate(BaseModel):
    name: str | None = None
    question_text: str | None = None
    comment_prompt: str | None = None
    trigger_type: str | None = None
    trigger_value: int | None = None
    is_active: bool | None = None
    # question_type intentionally excluded — immutable after creation


class SurveyConfigOut(BaseModel):
    id: uuid.UUID
    created_at: datetime
    name: str
    page_type: str
    question_text: str
    question_type: str
    comment_prompt: str | None
    trigger_type: str
    trigger_value: int
    is_active: bool

    model_config = {"from_attributes": True}


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

    @field_validator("comment")
    @classmethod
    def normalize_comment(cls, v: str | None) -> str | None:
        """Whitespace-only text is no comment at all."""
        if v is None:
            return None
        return v.strip() or None

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

    # Admin triage
    reviewed_at: datetime | None = None
    reviewed_by: str | None = None
    reviewed_by_email: str | None = None

    model_config = {"from_attributes": True}

    @computed_field
    @property
    def needs_review(self) -> bool:
        """Actionable (comment or negative rating) and not yet reviewed."""
        return self.reviewed_at is None and is_actionable(
            comment=self.comment, rating_thumbs=self.rating_thumbs, rating_stars=self.rating_stars
        )


class SurveyReviewUpdate(BaseModel):
    """PATCH body — mark one response reviewed (true) or back to unreviewed (false)."""

    reviewed: bool


class SurveyReviewBulkUpdate(BaseModel):
    """POST body — mark many responses reviewed/unreviewed in one call."""

    ids: list[uuid.UUID] = Field(min_length=1, max_length=200)
    reviewed: bool


class SurveyReviewBulkResult(BaseModel):
    updated: int


class SurveyNeedsReviewCount(BaseModel):
    count: int


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
