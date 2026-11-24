"""Queries behind the admin conversation viewer: filters, per-chat counts, topic totals.

Kept apart from the router so the HTTP layer only parses parameters and shapes
responses. Everything here is portable SQL (the tests run on SQLite).
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from typing import Literal

from sqlalchemy import case, exists, func, select
from sqlalchemy.orm import Session

from src.assistant.models import AssistantConversation, AssistantMessage, AssistantMessageFeedback
from src.assistant.question_topics import OTHER, TOPIC_SLUGS

FeedbackFilter = Literal["helpful", "unhelpful", "flagged", "any"]

conv, msg, fb = AssistantConversation, AssistantMessage, AssistantMessageFeedback


def conversation_filters(
    audience: str,
    school_id: uuid.UUID | None = None,
    topic: str | None = None,
    feedback: FeedbackFilter | None = None,
) -> list:
    """WHERE clauses on ``AssistantConversation`` for the list and the topic totals."""
    clauses = [conv.audience == audience]
    if school_id:
        clauses.append(conv.school_id == school_id)
    if topic:
        tagged = msg.topic_tag == topic if topic != OTHER else (
            (msg.topic_tag == OTHER) | msg.topic_tag.not_in(TOPIC_SLUGS)
        )
        clauses.append(exists().where(msg.conversation_id == conv.id, msg.role == "user", tagged))
    if feedback:
        rated = {
            "helpful": fb.rating == 1,
            "unhelpful": fb.rating == -1,
            "flagged": fb.flag.is_not(None),
            "any": fb.id.is_not(None),
        }[feedback]
        clauses.append(
            exists().where(fb.message_id == msg.id, msg.conversation_id == conv.id, rated)
        )
    return clauses


def question_counts(db: Session, ids: list[uuid.UUID]) -> dict[uuid.UUID, int]:
    rows = db.execute(
        select(msg.conversation_id, func.count(msg.id))
        .where(msg.conversation_id.in_(ids), msg.role == "user")
        .group_by(msg.conversation_id)
    ).all()
    return dict(rows)


def feedback_counts(db: Session, ids: list[uuid.UUID]) -> dict[uuid.UUID, tuple[int, int, int]]:
    """(helpful, unhelpful, flagged) answers per conversation."""
    rows = db.execute(
        select(
            msg.conversation_id,
            func.sum(case((fb.rating == 1, 1), else_=0)),
            func.sum(case((fb.rating == -1, 1), else_=0)),
            func.sum(case((fb.flag.is_not(None), 1), else_=0)),
        )
        .join(msg, msg.id == fb.message_id)
        .where(msg.conversation_id.in_(ids))
        .group_by(msg.conversation_id)
    ).all()
    return {cid: (up or 0, down or 0, flagged or 0) for cid, up, down, flagged in rows}


def topics_by_conversation(db: Session, ids: list[uuid.UUID]) -> dict[uuid.UUID, list[str]]:
    """Distinct topics asked in each conversation, most frequent first."""
    rows = db.execute(
        select(msg.conversation_id, msg.topic_tag, func.count(msg.id))
        .where(msg.conversation_id.in_(ids), msg.role == "user", msg.topic_tag.is_not(None))
        .group_by(msg.conversation_id, msg.topic_tag)
    ).all()
    counted: dict[uuid.UUID, dict[str, int]] = defaultdict(dict)
    for cid, tag, n in rows:
        slug = tag if tag in TOPIC_SLUGS else OTHER
        counted[cid][slug] = counted[cid].get(slug, 0) + n
    return {cid: sorted(tags, key=lambda t: -tags[t]) for cid, tags in counted.items()}


def topic_totals(db: Session, clauses: list) -> dict[str, int]:
    """Questions per topic in the conversations matching ``clauses`` (unknown slugs count as Other)."""
    rows = db.execute(
        select(msg.topic_tag, func.count(msg.id))
        .join(conv, conv.id == msg.conversation_id)
        .where(*clauses, msg.role == "user", msg.topic_tag.is_not(None))
        .group_by(msg.topic_tag)
    ).all()
    totals: dict[str, int] = defaultdict(int)
    for tag, n in rows:
        totals[tag if tag in TOPIC_SLUGS else OTHER] += n
    return totals


def untagged_count(db: Session) -> int:
    return db.scalar(
        select(func.count(msg.id)).where(msg.role == "user", msg.topic_tag.is_(None))
    ) or 0


def feedback_by_message(db: Session, conversation_id: uuid.UUID) -> dict[uuid.UUID, AssistantMessageFeedback]:
    """The rating left on each answer (only the chat's owner can rate, so one per answer)."""
    rows = db.scalars(
        select(fb).join(msg, msg.id == fb.message_id).where(msg.conversation_id == conversation_id)
    ).all()
    return {row.message_id: row for row in rows}
