"""Pydantic schemas for the calculators API."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, field_validator

from src.calculators.models import CALCULATOR_STATUSES, CALCULATOR_TYPES

_STATUS_ERROR = f"status must be one of: {', '.join(CALCULATOR_STATUSES)}"
_TYPE_ERROR = f"type must be one of: {', '.join(CALCULATOR_TYPES)}"


def _check_status(v):
    if v is not None and v not in CALCULATOR_STATUSES:
        raise ValueError(_STATUS_ERROR)
    return v


def _check_type(v):
    if v is not None and v not in CALCULATOR_TYPES:
        raise ValueError(_TYPE_ERROR)
    return v


class CalculatorCreate(BaseModel):
    slug: str
    title: str
    type: str
    description: str | None = None
    html: str | None = None
    deps: str | None = None
    config: dict = {}
    embed_allowed_origins: list[str] = []
    meta_title: str | None = None
    meta_description: str | None = None
    status: str = "draft"

    @field_validator("status")
    @classmethod
    def validate_status(cls, v: str) -> str:
        return _check_status(v)

    @field_validator("type")
    @classmethod
    def validate_type(cls, v: str) -> str:
        return _check_type(v)


class CalculatorUpdate(BaseModel):
    slug: str | None = None
    title: str | None = None
    type: str | None = None
    description: str | None = None
    html: str | None = None
    deps: str | None = None
    config: dict | None = None
    embed_allowed_origins: list[str] | None = None
    meta_title: str | None = None
    meta_description: str | None = None
    status: str | None = None

    @field_validator("status")
    @classmethod
    def validate_status(cls, v: str | None) -> str | None:
        return _check_status(v)

    @field_validator("type")
    @classmethod
    def validate_type(cls, v: str | None) -> str | None:
        return _check_type(v)


class CalculatorOut(BaseModel):
    id: uuid.UUID
    slug: str
    title: str
    type: str
    description: str | None
    html: str | None
    deps: str | None
    config: dict
    embed_allowed_origins: list[str]
    meta_title: str | None
    meta_description: str | None
    status: str
    created_at: datetime
    updated_at: datetime | None

    model_config = {"from_attributes": True}


class CalculatorSlugOut(BaseModel):
    """Minimal public shape for sitemap generation and the admin embed picker."""

    slug: str
    title: str
    updated_at: datetime | None


class CalculatorListItem(BaseModel):
    """Admin list row. Omits `html`/`config` so the payload stays small."""

    id: uuid.UUID
    slug: str
    title: str
    type: str
    status: str
    updated_at: datetime | None

    model_config = {"from_attributes": True}


class CalculatorListResponse(BaseModel):
    items: list[CalculatorListItem]
    total: int
