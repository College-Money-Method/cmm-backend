"""Request/response shapes for calls with Paul.

Mirrors ``app/types/paul-booking.ts`` in cmm-frontend; keep the two in step.
All datetimes serialise as UTC ISO strings, all dates as ``YYYY-MM-DD`` in
Paul's booking timezone.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import (
    AwareDatetime,
    BaseModel,
    EmailStr,
    Field,
    ValidationInfo,
    field_validator,
    model_validator,
)

Duration = Literal[30, 60]
Status = Literal["pending", "approving", "approved", "declined"]


class SlotsOut(BaseModel):
    host_timezone: str
    duration: Duration
    slots: list[datetime]


class RequestCreate(BaseModel):
    start: AwareDatetime
    duration: Duration
    first_name: str = Field(min_length=1, max_length=120)
    last_name: str = Field(min_length=1, max_length=120)
    email: EmailStr
    school_name: str = Field(min_length=1, max_length=200)
    role: str | None = Field(default=None, max_length=120)
    message: str | None = Field(default=None, max_length=5000)
    time_zone: str = Field(min_length=1, max_length=64)
    # Honeypot: the real form submits it empty; anything here means a script.
    website: str | None = Field(default=None, max_length=200, exclude=True)
    # Cloudflare Turnstile token from the form's widget; checked before a hold is taken.
    captcha_token: str | None = Field(default=None, max_length=2048, exclude=True)

    @field_validator("first_name", "last_name", "school_name", "role")
    @classmethod
    def _single_line(cls, value: str | None, info: ValidationInfo) -> str | None:
        """Collapse whitespace and drop control characters: these end up in email
        subjects and table cells, where a newline would break the message."""
        if value is None:
            return None
        cleaned = " ".join("".join(ch for ch in value if ch.isprintable() or ch.isspace()).split())
        if cleaned:
            return cleaned
        if info.field_name == "role":
            return None
        raise ValueError("Must contain visible text")

    @field_validator("time_zone")
    @classmethod
    def _known_zone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError, OSError) as exc:
            raise ValueError("Unknown time zone") from exc
        return value


class RequestReceipt(BaseModel):
    """What the public endpoint returns. A quarantined request looks identical."""

    status: Literal["pending"] = "pending"
    start: datetime
    duration: Duration
    expires_at: datetime


class ContactOut(BaseModel):
    first_name: str
    last_name: str | None
    email: str
    school_name: str | None
    role: str | None
    message: str | None
    is_parent: bool


class RequestOut(BaseModel):
    id: uuid.UUID
    start_at: datetime
    duration_minutes: Duration
    visitor_timezone: str
    status: Status
    # Pending and past expires_at: slot is free again, can no longer be approved.
    lapsed: bool
    expires_at: datetime
    created_at: datetime
    decided_at: datetime | None
    decline_note: str | None
    zoom_join_url: str | None
    contact: ContactOut


class ReviewOut(RequestOut):
    host_timezone: str


class DeclineIn(BaseModel):
    note: str | None = Field(default=None, max_length=1000)


class TimeOffCreate(BaseModel):
    starts_on: date
    ends_on: date
    note: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def _ordered(self) -> "TimeOffCreate":
        if self.ends_on < self.starts_on:
            raise ValueError("ends_on must not be before starts_on")
        return self


class TimeOffOut(BaseModel):
    model_config = {"from_attributes": True}

    id: uuid.UUID
    starts_on: date
    ends_on: date
    note: str | None
    created_at: datetime


class TimeOffCreated(BaseModel):
    item: TimeOffOut
    overlapping_requests: list[RequestOut]
