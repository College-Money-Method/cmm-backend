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


# ── Communications calendar schedule schemas ──────────────────────────────────


class ScheduleItemCreate(BaseModel):
    cycle_id: uuid.UUID
    event_type: str  # 'announcement' | 'followup' | 'communication'
    webinar_id: uuid.UUID | None = None
    template_id: uuid.UUID | None = None
    scheduled_at: datetime
    is_auto_generated: bool = True
    notes: str | None = None


class ScheduleItemUpdate(BaseModel):
    scheduled_at: datetime | None = None
    is_auto_generated: bool | None = None
    notes: str | None = None


class ScheduleItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    school_id: uuid.UUID
    cycle_id: uuid.UUID
    event_type: str
    webinar_id: uuid.UUID | None
    template_id: uuid.UUID | None
    template_name: str | None  # denormalized from template
    scheduled_at: datetime
    is_auto_generated: bool
    notes: str | None
    created_at: datetime
    updated_at: datetime | None


class WebinarCalendarItem(BaseModel):
    webinar_id: uuid.UUID
    webinar_name: str | None
    workshop_name: str | None
    start_datetime: datetime | None
    end_datetime: datetime | None
    cycle_id: uuid.UUID | None


class CalendarResponse(BaseModel):
    webinars: list[WebinarCalendarItem]
    schedule_items: list[ScheduleItemOut]
    template_defaults: list["TemplateDefaultDateOut"] = []


# ── Admin template default dates ──────────────────────────────────────────────


class TemplateDefaultDateOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    template_id: uuid.UUID
    template_name: str | None
    cycle_id: uuid.UUID
    suggested_at: datetime
    notes: str | None
    created_at: datetime
    updated_at: datetime | None


class TemplateDefaultDateUpsert(BaseModel):
    suggested_at: datetime
    notes: str | None = None
