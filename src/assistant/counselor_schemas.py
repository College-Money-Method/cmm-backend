"""Request body for the counselor assistant chat."""

from __future__ import annotations

import uuid

from pydantic import BaseModel, Field, field_validator


class CounselorChatIn(BaseModel):
    conversation_id: uuid.UUID | None = None
    message: str = Field(min_length=1, max_length=2000)
    # Honoured only for a super_admin; hub users always use their own school.
    school_id: uuid.UUID | None = None

    @field_validator("message")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("message is blank")
        return value
