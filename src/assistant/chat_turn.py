"""One question-and-answer turn, shared by every assistant endpoint.

Loads the conversation's recent history (or starts a conversation), retrieves
chunks for the caller's scope, stores the question, and streams the answer. The
caller decides the audience, the owner id, the scope and how the prompt is
built; nothing here reads them from the request body.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from datetime import UTC, datetime

from fastapi import HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy import select

from src.assistant.answer import HISTORY_MESSAGES, stream_answer
from src.assistant.embeddings import EmbeddingError, embed_query
from src.assistant.models import AssistantConversation, AssistantMessage
from src.assistant.retrieval_scope import RetrievalScope
from src.assistant.search import RetrievedChunk, hybrid_search, retrieval_query

logger = logging.getLogger(__name__)

_TITLE_CHARS = 80


def owned_conversation(db, conversation_id: uuid.UUID, owner_id: uuid.UUID, audience: str) -> AssistantConversation:
    """The caller's conversation in this assistant; anything else answers 404."""
    conversation = db.get(AssistantConversation, conversation_id)
    if conversation is None or conversation.local_user_id != owner_id or conversation.audience != audience:
        raise HTTPException(status_code=404, detail="assistant_conversation_not_found")
    return conversation


def _title_from(message: str) -> str:
    title = " ".join(message.split())
    return title if len(title) <= _TITLE_CHARS else title[: _TITLE_CHARS - 1].rstrip() + "…"


def run_chat_turn(
    db,
    *,
    audience: str,
    owner_id: uuid.UUID,
    conversation_id: uuid.UUID | None,
    message: str,
    scope: RetrievalScope,
    build_prompt: Callable[[list[RetrievedChunk]], str],
    profile: dict,
) -> StreamingResponse:
    """Retrieve for ``scope``, save the question, and stream the answer as SSE."""
    history: list[dict] = []
    if conversation_id:
        conversation = owned_conversation(db, conversation_id, owner_id, audience)
        recent = db.scalars(
            select(AssistantMessage)
            .where(AssistantMessage.conversation_id == conversation.id)
            .order_by(AssistantMessage.created_at.desc())
            .limit(HISTORY_MESSAGES)
        ).all()
        history = [{"role": m.role, "content": m.content} for m in reversed(recent)]
    else:
        conversation = AssistantConversation(local_user_id=owner_id, audience=audience, title=_title_from(message))
        db.add(conversation)

    # Messages must start with a user turn. A question whose answer failed is
    # an orphan user turn: drop it when it is last (the new question follows);
    # one mid-history is merged with the next user turn by the API.
    while history and history[0]["role"] != "user":
        history.pop(0)
    if history and history[-1]["role"] == "user":
        history.pop()

    retrieval_text = retrieval_query(history, message)
    try:
        query_vector = embed_query(retrieval_text)
    except EmbeddingError:
        logger.exception("assistant: query embedding failed")
        raise HTTPException(status_code=503, detail="assistant_unavailable") from None
    chunks = hybrid_search(db, retrieval_text, query_vector, scope)

    db.add(AssistantMessage(conversation=conversation, role="user", content=message, profile=profile))
    conversation.updated_at = datetime.now(UTC)
    db.commit()

    return StreamingResponse(
        stream_answer(
            conversation_id=conversation.id,
            history=history,
            question=message,
            chunks=chunks,
            profile=profile,
            system_prompt=build_prompt(chunks),
        ),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )
