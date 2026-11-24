"""Read a counselor's saved answer feedback, so a reopened chat shows it."""

from __future__ import annotations

import uuid

from sqlalchemy import select

from src.assistant.models import AssistantMessageFeedback
from src.assistant.schemas import AssistantFeedbackOut


def feedback_by_message(db, message_ids: list[uuid.UUID], user_id: uuid.UUID) -> dict[uuid.UUID, AssistantFeedbackOut]:
    if not message_ids:
        return {}
    rows = db.scalars(
        select(AssistantMessageFeedback).where(
            AssistantMessageFeedback.message_id.in_(message_ids),
            AssistantMessageFeedback.local_user_id == user_id,
        )
    ).all()
    return {r.message_id: AssistantFeedbackOut(rating=r.rating, flag=r.flag, comment=r.comment) for r in rows}
