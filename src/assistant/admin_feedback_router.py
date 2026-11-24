"""Admin review of the counselor assistant: answer feedback and per-school usage.

Read-only. Usage counts include chats a counselor deleted (soft delete), so the
numbers reflect what was asked, not what is still visible in the Hub.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Query, Response
from pydantic import BaseModel
from sqlalchemy import case, func, select
from sqlalchemy.orm import aliased

from src.assistant.counselor_context import COUNSELOR_AUDIENCE
from src.assistant.models import AssistantConversation, AssistantMessage, AssistantMessageFeedback
from src.auth.deps import AdminDep
from src.auth.models import Profile
from src.db.deps import DbDep
from src.schools.models import School

router = APIRouter(prefix="/api/v1/admin/assistant", tags=["assistant-admin"])

FEEDBACK_LIMIT = 200


class FeedbackRowOut(BaseModel):
    id: uuid.UUID
    conversation_id: uuid.UUID
    created_at: datetime
    updated_at: datetime
    school_name: str | None
    counselor_email: str | None
    card_type: str | None
    question: str | None
    answer: str
    rating: int | None
    flag: str | None
    comment: str | None
    conversation_deleted: bool


class FeedbackListOut(BaseModel):
    items: list[FeedbackRowOut]


class UsageRowOut(BaseModel):
    school_id: uuid.UUID | None
    school_name: str | None
    counselors: int
    conversations: int
    deleted_conversations: int
    questions: int
    last_used_at: datetime | None


class UsageListOut(BaseModel):
    items: list[UsageRowOut]


def _question_for(answer: type[AssistantMessage]):
    """The counselor's message just before this answer, as a correlated subquery."""
    asked = aliased(AssistantMessage)
    return (
        select(asked.content)
        .where(
            asked.conversation_id == answer.conversation_id,
            asked.role == "user",
            asked.created_at <= answer.created_at,
            asked.id != answer.id,
        )
        .order_by(asked.created_at.desc())
        .limit(1)
        .scalar_subquery()
    )


@router.get("/feedback", response_model=FeedbackListOut)
def list_feedback(
    _admin: AdminDep,
    db: DbDep,
    response: Response,
    flag: Literal["wrong_or_outdated"] | None = None,
    rating: int | None = Query(default=None, ge=-1, le=1),
    since: datetime | None = None,
    limit: int = Query(default=FEEDBACK_LIMIT, ge=1, le=FEEDBACK_LIMIT),
) -> FeedbackListOut:
    response.headers["Cache-Control"] = "private, no-store"
    fb, msg, conv = AssistantMessageFeedback, AssistantMessage, AssistantConversation
    query = (
        select(fb, msg.conversation_id, msg.content, conv.card_type, conv.deleted_at, School.name, Profile.email, _question_for(msg))
        .join(msg, msg.id == fb.message_id)
        .join(conv, conv.id == msg.conversation_id)
        .outerjoin(School, School.id == conv.school_id)
        .outerjoin(Profile, Profile.user_id == fb.local_user_id)
        .order_by(fb.updated_at.desc())
        .limit(limit)
    )
    if flag:
        query = query.where(fb.flag == flag)
    if rating:
        query = query.where(fb.rating == rating)
    if since:
        query = query.where(fb.updated_at >= since)
    items = [
        FeedbackRowOut(
            id=row.id, conversation_id=conversation_id, created_at=row.created_at, updated_at=row.updated_at,
            school_name=school, counselor_email=email, card_type=card_type,
            question=question, answer=answer, rating=row.rating, flag=row.flag,
            comment=row.comment, conversation_deleted=deleted_at is not None,
        )
        for row, conversation_id, answer, card_type, deleted_at, school, email, question in db.execute(query).all()
    ]
    return FeedbackListOut(items=items)


@router.get("/usage", response_model=UsageListOut)
def counselor_usage(_admin: AdminDep, db: DbDep, response: Response) -> UsageListOut:
    response.headers["Cache-Control"] = "private, no-store"
    conv, msg = AssistantConversation, AssistantMessage
    questions = (
        select(msg.conversation_id, func.count(msg.id).label("n"))
        .where(msg.role == "user")
        .group_by(msg.conversation_id)
        .subquery()
    )
    rows = db.execute(
        select(
            conv.school_id,
            School.name,
            func.count(func.distinct(conv.local_user_id)),
            func.count(conv.id),
            func.sum(case((conv.deleted_at.is_not(None), 1), else_=0)),
            func.coalesce(func.sum(questions.c.n), 0),
            func.max(conv.updated_at),
        )
        .outerjoin(School, School.id == conv.school_id)
        .outerjoin(questions, questions.c.conversation_id == conv.id)
        .where(conv.audience == COUNSELOR_AUDIENCE)
        .group_by(conv.school_id, School.name)
        .order_by(func.max(conv.updated_at).desc())
    ).all()
    return UsageListOut(
        items=[
            UsageRowOut(
                school_id=school_id, school_name=name, counselors=counselors,
                conversations=conversations, deleted_conversations=deleted or 0,
                questions=asked, last_used_at=last,
            )
            for school_id, name, counselors, conversations, deleted, asked, last in rows
        ]
    )
