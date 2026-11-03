"""Request/response shapes for the assistant API (``/api/v1/assistant``)."""

from __future__ import annotations

import re
import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

# Bounds on a calculator result the browser sends back. The SAI calculator
# returns about 50 flat fields today; the caps only stop abuse of the prompt.
CALCULATOR_RESULT_MAX_FIELDS = 80
CALCULATOR_RESULT_MAX_TEXT = 40
CALCULATOR_RESULT_MAX_ABS = 1e12
_CALCULATOR_KEY = re.compile(r"[a-z][a-z0-9_]{0,47}")
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


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


class AssistantCalculatorResultIn(BaseModel):
    """What CMM's own calculator computed in the family's browser.

    Kept on the device with its conversation and sent with each question, like
    the profile; it is put in the prompt and never stored here.
    """

    type: Literal["fafsa_sai"]
    values: dict[str, bool | int | float | str | None]

    @field_validator("values")
    @classmethod
    def _bounded(cls, values: dict) -> dict:
        if not values or len(values) > CALCULATOR_RESULT_MAX_FIELDS:
            raise ValueError("calculator result has no fields or too many")
        for key, value in values.items():
            if not _CALCULATOR_KEY.fullmatch(key):
                raise ValueError("bad calculator field name")
            if isinstance(value, str) and (
                len(value) > CALCULATOR_RESULT_MAX_TEXT or _CONTROL_CHARS.search(value)
            ):
                raise ValueError("calculator text value too long or not one line")
            # Each value becomes one prompt line. The comparison is False for NaN and
            # infinity, and exact (no float overflow) for huge ints.
            if isinstance(value, int | float) and not isinstance(value, bool):
                if not abs(value) <= CALCULATOR_RESULT_MAX_ABS:
                    raise ValueError("calculator number out of range")
        sai = values.get("sai")
        if isinstance(sai, bool) or not isinstance(sai, int | float):
            raise ValueError("calculator result has no sai")
        return values


class AssistantChatIn(BaseModel):
    conversation_id: uuid.UUID | None = None
    message: str = Field(min_length=1, max_length=2000)
    profile: AssistantProfileIn = Field(default_factory=AssistantProfileIn)
    calculator_result: AssistantCalculatorResultIn | None = None

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
