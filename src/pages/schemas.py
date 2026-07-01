"""Pydantic schemas for the CMS pages API."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, field_validator


class PageCreate(BaseModel):
    slug: str
    title: str
    content: str | None = None
    meta_title: str | None = None
    meta_description: str | None = None
    status: str = "draft"

    @field_validator("status")
    @classmethod
    def validate_status(cls, v: str) -> str:
        if v not in ("draft", "published"):
            raise ValueError("status must be draft or published")
        return v


class PageUpdate(BaseModel):
    slug: str | None = None
    title: str | None = None
    content: str | None = None
    meta_title: str | None = None
    meta_description: str | None = None
    status: str | None = None

    @field_validator("status")
    @classmethod
    def validate_status(cls, v: str | None) -> str | None:
        if v is not None and v not in ("draft", "published"):
            raise ValueError("status must be draft or published")
        return v


class PageOut(BaseModel):
    id: uuid.UUID
    slug: str
    title: str
    content: str | None
    meta_title: str | None
    meta_description: str | None
    status: str
    created_at: datetime
    updated_at: datetime | None

    model_config = {"from_attributes": True}


class PageListItem(BaseModel):
    id: uuid.UUID
    slug: str
    title: str
    status: str
    updated_at: datetime | None

    model_config = {"from_attributes": True}


class PageListResponse(BaseModel):
    items: list[PageListItem]
    total: int
