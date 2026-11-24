"""Admin conversation viewer: browse every assistant chat, read one, summarize it,
and see which topics users ask about.

Super admin only. Includes chats a counselor deleted (they are kept for review).
Summaries and topic tags are written by Haiku; see ``conversation_summary`` and
``question_topics``.
"""

from __future__ import annotations

import uuid
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Response
from sqlalchemy import func, select

from src.assistant import admin_conversation_queries as q
from src.assistant.admin_conversation_schemas import (
    BackfillOut,
    ConversationDetailOut,
    ConversationListOut,
    ConversationRowOut,
    FeedbackOut,
    MessageOut,
    SummaryOut,
    TopicListOut,
    TopicOut,
)
from src.assistant.conversation_summary import SummaryError, summarize_conversation
from src.assistant.counselor_context import COUNSELOR_AUDIENCE
from src.assistant.models import AssistantConversation
from src.assistant.question_topics import BACKFILL_BATCH, OTHER, TOPIC_SLUGS, TOPICS, tag_untagged
from src.auth.deps import AdminDep
from src.auth.models import Profile
from src.db.deps import DbDep
from src.schools.models import School

router = APIRouter(prefix="/api/v1/admin/assistant", tags=["assistant-admin"])

Audience = Literal["counselor", "family"]
PAGE_LIMIT = 100


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "private, no-store"


def _rows(db, ids: list[uuid.UUID]) -> list[ConversationRowOut]:
    """List rows for ``ids``, in the given order."""
    if not ids:
        return []
    meta = {
        c.id: (c, school, email)
        for c, school, email in db.execute(
            select(q.conv, School.name, Profile.email)
            .outerjoin(School, School.id == q.conv.school_id)
            .outerjoin(Profile, Profile.user_id == q.conv.local_user_id)
            .where(q.conv.id.in_(ids))
        ).all()
    }
    asked, rated, topics = q.question_counts(db, ids), q.feedback_counts(db, ids), q.topics_by_conversation(db, ids)
    out = []
    for cid in ids:
        c, school, email = meta[cid]
        up, down, flagged = rated.get(cid, (0, 0, 0))
        out.append(ConversationRowOut(
            id=c.id, title=c.title, audience=c.audience, school_id=c.school_id, school_name=school,
            # Family chats are keyed by an anonymous device id; there is no account to show.
            user_email=email if c.audience == COUNSELOR_AUDIENCE else None,
            card_type=c.card_type, created_at=c.created_at, updated_at=c.updated_at,
            deleted=c.deleted_at is not None, questions=asked.get(cid, 0),
            helpful=up, unhelpful=down, flagged=flagged, topics=topics.get(cid, []),
        ))
    return out


@router.get("/conversations", response_model=ConversationListOut)
def list_conversations(
    _admin: AdminDep,
    db: DbDep,
    response: Response,
    audience: Audience = COUNSELOR_AUDIENCE,
    school_id: uuid.UUID | None = None,
    topic: str | None = None,
    feedback: q.FeedbackFilter | None = None,
    limit: int = Query(default=50, ge=1, le=PAGE_LIMIT),
    offset: int = Query(default=0, ge=0),
) -> ConversationListOut:
    _no_store(response)
    clauses = q.conversation_filters(audience, school_id, topic, feedback)
    total = db.scalar(select(func.count(q.conv.id)).where(*clauses)) or 0
    ids = list(db.scalars(
        select(q.conv.id).where(*clauses).order_by(q.conv.updated_at.desc()).limit(limit).offset(offset)
    ))
    return ConversationListOut(items=_rows(db, ids), total=total)


def _get_conversation(db, conversation_id: uuid.UUID) -> AssistantConversation:
    conversation = db.get(AssistantConversation, conversation_id)
    if conversation is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return conversation


def _detail(db, conversation: AssistantConversation) -> ConversationDetailOut:
    feedback = q.feedback_by_message(db, conversation.id)
    messages = [
        MessageOut(
            id=m.id, role=m.role, content=m.content, sources=m.sources or [], created_at=m.created_at,
            topic=(m.topic_tag if m.topic_tag in TOPIC_SLUGS else OTHER) if m.topic_tag else None,
            feedback=FeedbackOut(rating=f.rating, flag=f.flag, comment=f.comment) if (f := feedback.get(m.id)) else None,
        )
        for m in conversation.messages
    ]
    summary = None
    if conversation.admin_summary:
        summary = SummaryOut(
            text=conversation.admin_summary, at=conversation.admin_summary_at,
            messages=conversation.admin_summary_messages,
            stale=conversation.admin_summary_messages != len(messages),
        )
    return ConversationDetailOut(conversation=_rows(db, [conversation.id])[0], messages=messages, summary=summary)


@router.get("/conversations/{conversation_id}", response_model=ConversationDetailOut)
def get_conversation(conversation_id: uuid.UUID, _admin: AdminDep, db: DbDep, response: Response) -> ConversationDetailOut:
    _no_store(response)
    return _detail(db, _get_conversation(db, conversation_id))


@router.post("/conversations/{conversation_id}/summary", response_model=ConversationDetailOut)
def summarize(conversation_id: uuid.UUID, _admin: AdminDep, db: DbDep, response: Response) -> ConversationDetailOut:
    _no_store(response)
    conversation = _get_conversation(db, conversation_id)
    try:
        summarize_conversation(db, conversation, q.feedback_by_message(db, conversation.id))
    except SummaryError as exc:
        code = "empty_conversation" if str(exc) == "empty_conversation" else "summary_failed"
        raise HTTPException(status_code=422 if code == "empty_conversation" else 502, detail=code) from exc
    return _detail(db, conversation)


@router.get("/question-topics", response_model=TopicListOut)
def question_topics(
    _admin: AdminDep, db: DbDep, response: Response,
    audience: Audience = COUNSELOR_AUDIENCE, school_id: uuid.UUID | None = None,
) -> TopicListOut:
    _no_store(response)
    totals = q.topic_totals(db, q.conversation_filters(audience, school_id))
    return TopicListOut(
        items=[TopicOut(slug=t.slug, label=t.label, group=t.group, description=t.description,
                        count=totals.get(t.slug, 0)) for t in TOPICS],
        untagged=q.untagged_count(db),
    )


@router.post("/question-topics/backfill", response_model=BackfillOut)
def backfill_topics(_admin: AdminDep, db: DbDep, response: Response) -> BackfillOut:
    """Tag up to one batch of untagged questions (older chats, or a failed live tag)."""
    _no_store(response)
    tagged, failed = tag_untagged(db, BACKFILL_BATCH)
    return BackfillOut(tagged=tagged, failed=failed, remaining=q.untagged_count(db))
