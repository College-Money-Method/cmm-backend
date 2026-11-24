"""Response shapes for the admin conversation viewer (``admin_conversations_router``)."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel


class ConversationRowOut(BaseModel):
    id: uuid.UUID
    title: str
    audience: str
    school_id: uuid.UUID | None
    school_name: str | None
    user_email: str | None
    card_type: str | None
    created_at: datetime
    updated_at: datetime
    deleted: bool
    questions: int
    helpful: int
    unhelpful: int
    flagged: int
    topics: list[str]


class ConversationListOut(BaseModel):
    items: list[ConversationRowOut]
    total: int


class FeedbackOut(BaseModel):
    rating: int | None
    flag: str | None
    comment: str | None


class MessageOut(BaseModel):
    id: uuid.UUID
    role: str
    content: str
    sources: list
    created_at: datetime
    topic: str | None
    feedback: FeedbackOut | None


class SummaryOut(BaseModel):
    text: str
    at: datetime | None
    messages: int | None
    stale: bool


class ConversationDetailOut(BaseModel):
    conversation: ConversationRowOut
    messages: list[MessageOut]
    summary: SummaryOut | None


class TopicOut(BaseModel):
    slug: str
    label: str
    group: str
    description: str
    count: int


class TopicListOut(BaseModel):
    items: list[TopicOut]
    untagged: int


class BackfillOut(BaseModel):
    tagged: int
    failed: int
    remaining: int
