"""Request/response shapes for the assistant API (``/api/v1/assistant``)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator


class AssistantAccessIn(BaseModel):
    password: str = Field(max_length=200, repr=False)


class AssistantAccessOut(BaseModel):
    token: str
    expires_at: datetime


class AssistantProfileIn(BaseModel):
    """Who is asking — kept in the browser, sent per question, never tied to a name."""

    role: Literal["family", "counselor"] = "family"
    grade: int | None = Field(default=None, ge=9, le=12)
    state: str | None = Field(default=None, pattern=r"^[A-Za-z]{2}$")

    @field_validator("state")
    @classmethod
    def _upper_state(cls, value: str | None) -> str | None:
        return value.upper() if value else value


class AssistantChatIn(BaseModel):
    conversation_id: uuid.UUID | None = None
    message: str = Field(min_length=1, max_length=2000)
    profile: AssistantProfileIn = Field(default_factory=AssistantProfileIn)

    @field_validator("message")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("message is blank")
        return value


class AssistantSourceOut(BaseModel):
    n: int
    title: str
    url: str | None
    source_type: str
    heading: str | None


class AssistantConversationSummaryOut(BaseModel):
    id: uuid.UUID
    title: str
    updated_at: datetime


class AssistantConversationListOut(BaseModel):
    items: list[AssistantConversationSummaryOut]


class AssistantMessageOut(BaseModel):
    id: uuid.UUID
    role: str
    content: str
    sources: list[AssistantSourceOut]
    created_at: datetime


class AssistantConversationOut(BaseModel):
    id: uuid.UUID
    title: str
    messages: list[AssistantMessageOut]
