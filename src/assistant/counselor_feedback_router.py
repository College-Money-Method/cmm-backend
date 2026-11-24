"""Counselor feedback on one assistant answer: thumbs up/down, a flag, a comment.

One row per (answer, counselor); a later PUT replaces it. Only the owner of a
visible counselor conversation can rate its answers; anything else answers 404.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select

from src.assistant.chat_turn import owned_conversation
from src.assistant.counselor_context import COUNSELOR_AUDIENCE
from src.assistant.counselor_router import CounselorAssistantUser
from src.assistant.models import AssistantMessage, AssistantMessageFeedback
from src.assistant.schemas import AssistantFeedbackOut
from src.db.deps import DbDep

router = APIRouter(prefix="/api/v1/assistant/counselor", tags=["assistant-counselor"])

FeedbackFlag = Literal["wrong_or_outdated"]


class AssistantFeedbackIn(BaseModel):
    rating: Literal[-1, 1] | None = None
    flag: FeedbackFlag | None = None
    comment: str | None = Field(default=None, max_length=2000)

    @field_validator("comment")
    @classmethod
    def _blank_is_none(cls, value: str | None) -> str | None:
        value = (value or "").strip()
        return value or None


@router.put("/messages/{message_id}/feedback", response_model=AssistantFeedbackOut)
def put_feedback(
    message_id: uuid.UUID, body: AssistantFeedbackIn, user: CounselorAssistantUser, db: DbDep
) -> AssistantFeedbackOut:
    message = db.get(AssistantMessage, message_id)
    if message is None or message.role != "assistant":
        raise HTTPException(status_code=404, detail="assistant_message_not_found")
    owned_conversation(db, message.conversation_id, user.user_id, COUNSELOR_AUDIENCE)

    row = db.scalar(
        select(AssistantMessageFeedback).where(
            AssistantMessageFeedback.message_id == message_id,
            AssistantMessageFeedback.local_user_id == user.user_id,
        )
    )
    if row is None:
        row = AssistantMessageFeedback(message_id=message_id, local_user_id=user.user_id)
        db.add(row)
    row.rating, row.flag, row.comment = body.rating, body.flag, body.comment
    row.updated_at = datetime.now(UTC)
    db.commit()
    return AssistantFeedbackOut(rating=row.rating, flag=row.flag, comment=row.comment)
