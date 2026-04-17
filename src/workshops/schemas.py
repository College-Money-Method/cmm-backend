"""Schemas for workshops, webinars, registrations, and portal mapping."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from src.content.schemas import ContentAssetSummary
from src.db.enums import RegistrationStatus


# ── Admin: Workshop schemas ──────────────────────────────────────────────────


class WorkshopCreate(BaseModel):
    name: str
    description: str | None = None
    key_actions: str | None = None
    body: str | None = None
    sequence_number: int | None = None
    suggested_grades: str | None = None
    resource_center_slug: str | None = None
    workshop_art_url: str | None = None


class WorkshopUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    key_actions: str | None = None
    body: str | None = None
    sequence_number: int | None = None
    suggested_grades: str | None = None
    resource_center_slug: str | None = None
    workshop_art_url: str | None = None


class WebinarSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    webinar_name: str | None
    cohort_id: uuid.UUID | None
    start_datetime: datetime | None
    end_datetime: datetime | None
    zoom_webinar_id: str | None
    registration_url: str | None
    zoom_link: str | None
    registration_count: int


class WorkshopSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    description: str | None
    suggested_grades: str | None
    workshop_art_url: str | None
    sequence_number: int | None
    created_at: datetime
    webinar_count: int
    next_webinar_date: datetime | None


class ObjectiveSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    description: str | None


class ObjectiveIdsBody(BaseModel):
    ids: list[uuid.UUID] = []


class WorkshopResourceItem(BaseModel):
    content_asset_id: uuid.UUID
    sort_order: int


class WorkshopResourcesUpdate(BaseModel):
    items: list[WorkshopResourceItem] = []


class WorkshopOut(BaseModel):
    """Workshop detail without webinars (webinars loaded separately)."""
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    description: str | None
    key_actions: str | None
    body: str | None
    sequence_number: int | None
    suggested_grades: str | None
    resource_center_slug: str | None
    workshop_art_url: str | None
    created_at: datetime
    webinar_count: int
    objectives: list[ObjectiveSummary] = []
    resources: list[ContentAssetSummary] = []


# ── Admin: Webinar schemas ───────────────────────────────────────────────────


class WebinarCreate(BaseModel):
    cohort_id: uuid.UUID | None = None
    cycle_id: uuid.UUID | None = None
    school_ids: list[uuid.UUID] = []
    webinar_name: str | None = None
    zoom_webinar_id: str | None = None
    start_datetime: datetime | None = None
    end_datetime: datetime | None = None
    join_url: str | None = None
    start_url: str | None = None
    registration_url: str | None = None
    zoom_link: str | None = None
    video_embed_code: str | None = None
    track_registrations: bool = True


class WebinarUpdate(BaseModel):
    cohort_id: uuid.UUID | None = None
    cycle_id: uuid.UUID | None = None
    webinar_name: str | None = None
    zoom_webinar_id: str | None = None
    start_datetime: datetime | None = None
    end_datetime: datetime | None = None
    join_url: str | None = None
    start_url: str | None = None
    registration_url: str | None = None
    zoom_link: str | None = None
    video_embed_code: str | None = None
    audio_transcript: str | None = None
    track_registrations: bool | None = None


class WebinarOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    workshop_id: uuid.UUID
    cohort_id: uuid.UUID | None
    cycle_id: uuid.UUID | None
    webinar_name: str | None
    zoom_webinar_id: str | None
    start_datetime: datetime | None
    end_datetime: datetime | None
    duration_minutes: int | None
    join_url: str | None
    start_url: str | None
    registration_url: str | None
    zoom_link: str | None
    video_embed_code: str | None
    audio_transcript: str | None
    track_registrations: bool
    created_at: datetime
    workshop_name: str
    cohort_name: str | None
    registration_count: int


# ── Admin: Registration schemas ──────────────────────────────────────────────


class RegistrationCreate(BaseModel):
    email: str
    first_name: str | None = None
    last_name: str | None = None
    school_id: uuid.UUID | None = None
    grade: str | None = None
    status: RegistrationStatus = RegistrationStatus.APPROVED
    questions: str | None = None


class RegistrationUpdate(BaseModel):
    status: RegistrationStatus | None = None
    attended: bool | None = None
    first_name: str | None = None
    last_name: str | None = None
    grade: str | None = None


class RegistrationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    webinar_id: uuid.UUID
    school_id: uuid.UUID | None
    first_name: str | None
    last_name: str | None
    full_name: str | None
    email: str
    grade: str | None
    status: RegistrationStatus
    attended: bool
    join_time: datetime | None
    leave_time: datetime | None
    zoom_registrant_id: str | None
    questions: str | None
    registration_time: datetime | None
    created_at: datetime
    school_name: str | None


# ── Admin: Portal mapping schemas ────────────────────────────────────────────


class PortalMappingOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    school_id: uuid.UUID
    school_name: str
    webinar_id: uuid.UUID
    show_zoom: bool
    created_at: datetime


class PortalMappingCreate(BaseModel):
    school_id: uuid.UUID
    show_zoom: bool = True


# ── Public: Portal schemas ───────────────────────────────────────────────────


class WorkshopPortalItem(BaseModel):
    """A webinar+workshop merged for the school portal."""

    model_config = ConfigDict(from_attributes=True)

    # Webinar fields
    webinar_id: uuid.UUID
    start_datetime: datetime | None
    end_datetime: datetime | None
    registration_url: str | None
    zoom_link: str | None
    video_embed_code: str | None
    join_url: str | None
    show_zoom: bool

    # Workshop fields
    workshop_id: uuid.UUID
    name: str
    description: str | None
    key_actions: str | None
    body: str | None
    suggested_grades: str | None
    workshop_art_url: str | None
    sequence_number: int | None


class SchoolWorkshopsResponse(BaseModel):
    upcoming: list[WorkshopPortalItem]
    past: list[WorkshopPortalItem]
