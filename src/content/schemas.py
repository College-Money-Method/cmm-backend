"""Pydantic schemas for the content management API."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, field_validator


# ── Asset Types ───────────────────────────────────────────────────────────────

class AssetTypeOut(BaseModel):
    id: uuid.UUID
    airtable_id: str | None
    name: str
    color: str | None
    icon_url: str | None
    created_at: datetime

    model_config = {"from_attributes": True}


class AssetTypeCreate(BaseModel):
    name: str
    color: str | None = None
    icon_url: str | None = None


class AssetTypeUpdate(BaseModel):
    name: str | None = None
    color: str | None = None
    icon_url: str | None = None


# ── Topics ────────────────────────────────────────────────────────────────────

class TopicOut(BaseModel):
    id: uuid.UUID
    airtable_id: str | None
    name: str
    description: str | None
    icon_url: str | None
    slug: str
    suggested_grades: str | None
    sort_order: int
    created_at: datetime

    model_config = {"from_attributes": True}


class TopicCreate(BaseModel):
    name: str
    description: str | None = None
    icon_url: str | None = None
    slug: str | None = None  # auto-generated from name if omitted
    suggested_grades: str | None = None
    sort_order: int = 0


class TopicUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    icon_url: str | None = None
    slug: str | None = None
    suggested_grades: str | None = None
    sort_order: int | None = None


class ContentAssetSummary(BaseModel):
    """Lightweight asset for topic listings."""
    id: uuid.UUID
    name: str
    description: str | None
    image_url: str | None
    asset_type: AssetTypeOut | None

    model_config = {"from_attributes": True}


class TopicWithAssets(BaseModel):
    """Topic with nested published content assets."""
    id: uuid.UUID
    name: str
    description: str | None
    icon_url: str | None
    slug: str
    suggested_grades: str | None
    sort_order: int
    content_assets: list[ContentAssetSummary]

    model_config = {"from_attributes": True}


# ── Objectives ────────────────────────────────────────────────────────────────

class ObjectiveOut(BaseModel):
    id: uuid.UUID
    airtable_id: str | None
    name: str
    description: str | None
    created_at: datetime

    model_config = {"from_attributes": True}


class ObjectiveCreate(BaseModel):
    name: str
    description: str | None = None


class ObjectiveUpdate(BaseModel):
    name: str | None = None
    description: str | None = None


# ── Content Assets ────────────────────────────────────────────────────────────

class ContentAssetListItem(BaseModel):
    id: uuid.UUID
    name: str
    status: str
    is_featured: bool
    image_url: str | None
    link: str | None
    asset_type: AssetTypeOut | None
    created_at: datetime

    model_config = {"from_attributes": True}


class ContentAssetDetail(BaseModel):
    id: uuid.UUID
    airtable_id: str | None
    name: str
    description: str | None
    summary: str | None
    content: str | None
    action_items: list[str]
    link: str | None
    embed_code: str | None
    image_url: str | None
    file_url: str | None
    is_featured: bool
    status: str
    wp_post_id: str | None
    wp_synced_at: datetime | None
    created_at: datetime
    asset_type: AssetTypeOut | None
    objectives: list[ObjectiveOut]
    topics: list[TopicOut]
    workshops: list[WorkshopRef]
    cohorts: list[CohortRef]
    faqs: list[FaqOut]
    resources: list[ContentAssetListItem]

    model_config = {"from_attributes": True}


class WorkshopRef(BaseModel):
    id: uuid.UUID
    name: str

    model_config = {"from_attributes": True}


class CohortRef(BaseModel):
    id: uuid.UUID
    name: str

    model_config = {"from_attributes": True}


class ContentAssetCreate(BaseModel):
    name: str
    asset_type_id: uuid.UUID | None = None
    description: str | None = None
    content: str | None = None
    link: str | None = None
    embed_code: str | None = None
    image_url: str | None = None
    is_featured: bool = False
    status: str = "draft"
    wp_post_id: str | None = None

    @field_validator("status")
    @classmethod
    def validate_status(cls, v: str) -> str:
        if v not in ("draft", "published", "archived"):
            raise ValueError("status must be draft, published, or archived")
        return v


class ContentAssetUpdate(BaseModel):
    name: str | None = None
    asset_type_id: uuid.UUID | None = None
    description: str | None = None
    summary: str | None = None
    content: str | None = None
    action_items: list[str] | None = None
    link: str | None = None
    embed_code: str | None = None
    image_url: str | None = None
    is_featured: bool | None = None
    status: str | None = None
    wp_post_id: str | None = None

    @field_validator("status")
    @classmethod
    def validate_status(cls, v: str | None) -> str | None:
        if v is not None and v not in ("draft", "published", "archived"):
            raise ValueError("status must be draft, published, or archived")
        return v


class RelationshipsUpdate(BaseModel):
    ids: list[uuid.UUID]


class ContentAssetListResponse(BaseModel):
    items: list[ContentAssetListItem]
    total: int
    skip: int
    limit: int


# ── FAQs ──────────────────────────────────────────────────────────────────────

class FaqOut(BaseModel):
    id: uuid.UUID
    question: str
    answer: str
    created_at: datetime

    model_config = {"from_attributes": True}


class FaqCreate(BaseModel):
    question: str
    answer: str


class FaqUpdate(BaseModel):
    question: str | None = None
    answer: str | None = None


class FaqOrderItem(BaseModel):
    faq_id: uuid.UUID
    sort_order: int


class FaqsUpdate(BaseModel):
    items: list[FaqOrderItem]


# ── Resources ─────────────────────────────────────────────────────────────────

class ResourceOrderItem(BaseModel):
    resource_id: uuid.UUID
    sort_order: int


class ResourcesUpdate(BaseModel):
    items: list[ResourceOrderItem]


# ── Reader Questions ───────────────────────────────────────────────────────────

class ReaderQuestionCreate(BaseModel):
    email: str
    question: str


class ReaderQuestionOut(BaseModel):
    id: uuid.UUID
    email: str
    question: str
    status: str
    created_at: datetime

    model_config = {"from_attributes": True}
