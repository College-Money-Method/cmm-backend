"""AI assistant public API: password -> token, streamed chat, per-device history.

Standalone and password gated like the College Aid Explorer (see
``access_token``). Every chat endpoint is scoped to the anonymous device id the
browser sends; another device's conversation answers 404, never 403, so ids
cannot be probed.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse
from sqlalchemy import select

from src.assistant.access_token import AssistantUserDep, current_password_hash, issue_access_token
from src.assistant.answer import HISTORY_MESSAGES, stream_answer
from src.assistant.embeddings import EmbeddingError, embed_query
from src.assistant.models import AssistantConversation, AssistantMessage
from src.assistant.schemas import (
    AssistantAccessIn,
    AssistantAccessOut,
    AssistantChatIn,
    AssistantConversationListOut,
    AssistantConversationOut,
)
from src.assistant.search import hybrid_search, retrieval_query
from src.colleges.access_password import verify_password
from src.db.deps import DbDep

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/assistant", tags=["assistant"])

# Fixed pause before a wrong-password answer: a cheap brute-force brake.
# Module-level so tests can zero it.
BAD_PASSWORD_DELAY_SECONDS = 1.0
CONVERSATION_LIST_LIMIT = 50
_TITLE_CHARS = 80


def _check_password(db, password: str) -> tuple[str | None, bool]:
    stored = current_password_hash(db)
    return stored, bool(stored) and verify_password(password, stored)


@router.post("/access", response_model=AssistantAccessOut)
async def request_access(body: AssistantAccessIn, db: DbDep) -> AssistantAccessOut:
    """Exchange the shared password for a 7-day access token."""
    # DB read and scrypt run on the threadpool; the wrong-password pause is an
    # async sleep so a burst of bad guesses cannot tie up threadpool workers.
    stored, ok = await run_in_threadpool(_check_password, db, body.password)
    if not stored:
        raise HTTPException(status_code=403, detail="assistant_disabled")
    if not ok:
        await asyncio.sleep(BAD_PASSWORD_DELAY_SECONDS)
        logger.warning("assistant access: wrong password")
        raise HTTPException(status_code=401, detail="assistant_bad_password")
    token, expires_at = issue_access_token(stored)
    return AssistantAccessOut(token=token, expires_at=expires_at)


def _owned_conversation(db, conversation_id: uuid.UUID, user_id: uuid.UUID) -> AssistantConversation:
    conversation = db.get(AssistantConversation, conversation_id)
    if conversation is None or conversation.local_user_id != user_id:
        raise HTTPException(status_code=404, detail="assistant_conversation_not_found")
    return conversation


def _title_from(message: str) -> str:
    title = " ".join(message.split())
    return title if len(title) <= _TITLE_CHARS else title[: _TITLE_CHARS - 1].rstrip() + "…"


@router.post("/chat")
def chat(body: AssistantChatIn, user_id: AssistantUserDep, db: DbDep) -> StreamingResponse:
    """Answer one question from published content, streamed as server-sent events."""
    history: list[dict] = []
    if body.conversation_id:
        conversation = _owned_conversation(db, body.conversation_id, user_id)
        recent = db.scalars(
            select(AssistantMessage)
            .where(AssistantMessage.conversation_id == conversation.id)
            .order_by(AssistantMessage.created_at.desc())
            .limit(HISTORY_MESSAGES)
        ).all()
        history = [{"role": m.role, "content": m.content} for m in reversed(recent)]
    else:
        conversation = AssistantConversation(local_user_id=user_id, title=_title_from(body.message))
        db.add(conversation)

    # Messages must start with a user turn. A question whose answer failed is
    # an orphan user turn: drop it when it is last (the new question follows);
    # one mid-history is merged with the next user turn by the API.
    while history and history[0]["role"] != "user":
        history.pop(0)
    if history and history[-1]["role"] == "user":
        history.pop()

    retrieval_text = retrieval_query(history, body.message)
    profile = body.profile.model_dump()
    try:
        query_vector = embed_query(retrieval_text)
    except EmbeddingError:
        logger.exception("assistant: query embedding failed")
        raise HTTPException(status_code=503, detail="assistant_unavailable") from None
    chunks = hybrid_search(db, retrieval_text, query_vector, role=profile["role"])

    db.add(AssistantMessage(conversation=conversation, role="user", content=body.message, profile=profile))
    conversation.updated_at = datetime.now(UTC)
    db.commit()

    return StreamingResponse(
        stream_answer(
            conversation_id=conversation.id,
            history=history,
            question=body.message,
            chunks=chunks,
            profile=profile,
            calculator_result=body.calculator_result.model_dump() if body.calculator_result else None,
        ),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


@router.get("/conversations", response_model=AssistantConversationListOut)
def list_conversations(user_id: AssistantUserDep, db: DbDep, response: Response) -> AssistantConversationListOut:
    response.headers["Cache-Control"] = "private, no-store"
    rows = db.scalars(
        select(AssistantConversation)
        .where(AssistantConversation.local_user_id == user_id)
        .order_by(AssistantConversation.updated_at.desc())
        .limit(CONVERSATION_LIST_LIMIT)
    ).all()
    return AssistantConversationListOut(
        items=[{"id": c.id, "title": c.title, "updated_at": c.updated_at} for c in rows]
    )


@router.get("/conversations/{conversation_id}", response_model=AssistantConversationOut)
def get_conversation(
    conversation_id: uuid.UUID, user_id: AssistantUserDep, db: DbDep, response: Response
) -> AssistantConversationOut:
    response.headers["Cache-Control"] = "private, no-store"
    conversation = _owned_conversation(db, conversation_id, user_id)
    return AssistantConversationOut(
        id=conversation.id,
        title=conversation.title,
        messages=[
            {"id": m.id, "role": m.role, "content": m.content, "sources": m.sources or [], "created_at": m.created_at}
            for m in conversation.messages
        ],
    )


@router.delete("/conversations/{conversation_id}", status_code=204)
def delete_conversation(conversation_id: uuid.UUID, user_id: AssistantUserDep, db: DbDep) -> Response:
    db.delete(_owned_conversation(db, conversation_id, user_id))
    db.commit()
    return Response(status_code=204)
