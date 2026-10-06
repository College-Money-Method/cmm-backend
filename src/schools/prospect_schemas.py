"""Request/response models for the public school fit-check intake."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, EmailStr, Field, field_validator

from src.schools.prospect_matching import ProspectStatus
from src.storage.asset_url import CdnUrl


class ProspectForm(BaseModel):
    """Validated multipart fields of ``POST /schools/prospects``."""

    school_name: str = Field(min_length=1, max_length=200)
    city: str | None = Field(default=None, max_length=120)
    state: str = Field(pattern=r"^[A-Za-z]{2}$")
    first_name: str = Field(min_length=1, max_length=120)
    last_name: str = Field(min_length=1, max_length=120)
    email: EmailStr
    role: str | None = Field(default=None, max_length=120)
    quiz_answers: dict | None = None
    website: str | None = Field(default=None, max_length=200)  # honeypot

    @field_validator("school_name", "city", "first_name", "last_name", "role", mode="before")
    @classmethod
    def _strip(cls, value):
        if isinstance(value, str):
            value = value.strip()
            return value or None
        return value

    @field_validator("state")
    @classmethod
    def _upper(cls, value: str) -> str:
        return value.upper()


class ProspectCheckResponse(BaseModel):
    status: ProspectStatus
    slug: str | None = None
    expires_at: datetime | None = None


class ProspectCreateResponse(BaseModel):
    status: Literal["preview_ready", "existing_partner", "preview_exists", "preview_expired"]
    school_id: uuid.UUID | None = None
    slug: str | None = None
    school_name: str | None = None
    password: str | None = None
    expires_at: datetime | None = None
    session_token: str | None = None


class RecentProspect(BaseModel):
    school_id: uuid.UUID
    school_name: str
    slug: str | None = None
    logo_thumb_url: CdnUrl = None
    city: str | None = None
    state: str | None = None
    contact_name: str | None = None
    email: str | None = None
    role: str | None = None
    created_at: datetime | None = None
    src_preview_expires_at: datetime | None = None
    is_src_preview: bool = False
