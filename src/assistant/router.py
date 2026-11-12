"""Family assistant API: password -> token, streamed chat, per-device history.

Standalone and password gated like the College Aid Explorer (see
``access_token``). Every chat endpoint is scoped to the anonymous device id the
browser sends; another device's conversation answers 404, never 403, so ids
cannot be probed.
"""

from __future__ import annotations

import asyncio
import logging
import uuid

from fastapi import APIRouter, HTTPException, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse
from sqlalchemy import select

from src.assistant.access_token import (
    AssistantUserDep,
    current_password_hash,
    issue_access_token,
)
from src.assistant.chat_turn import owned_conversation, run_chat_turn
from src.assistant.family_prompt import build_family_prompt
from src.assistant.family_school_scope import family_school_scope
from src.assistant.models import AssistantConversation
from src.assistant.prompt_sections import prompt_guidance
from src.assistant.schemas import (
    AssistantAccessIn,
    AssistantAccessOut,
    AssistantChatIn,
    AssistantConversationListOut,
    AssistantConversationOut,
)
from src.colleges.access_password import verify_password
from src.db.deps import DbDep
from src.schools.models import School
from src.schools.src_session import OptionalUserDep, SrcSessionHeader

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/assistant", tags=["assistant"])

# Fixed pause before a wrong-password answer: a cheap brute-force brake.
# Module-level so tests can zero it.
BAD_PASSWORD_DELAY_SECONDS = 1.0
CONVERSATION_LIST_LIMIT = 50
FAMILY_AUDIENCE = "family"


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


@router.post("/chat")
def chat(
    body: AssistantChatIn,
    user_id: AssistantUserDep,
    db: DbDep,
    user: OptionalUserDep,
    src_session: SrcSessionHeader = None,
) -> StreamingResponse:
    """Answer one question from published family content, streamed as server-sent events."""
    # Resolved first so a refused school costs no embedding call. The audience
    # is fixed by this endpoint; the profile only words the prompt.
    scope = family_school_scope(db, body.school_id, src_session, user)
    # Already loaded and checked above; citations then open in that school's resource center.
    school = db.get(School, body.school_id) if body.school_id else None
    profile = body.profile.model_dump()
    profile["school_id"] = str(body.school_id) if body.school_id else None
    guidance = prompt_guidance(db, FAMILY_AUDIENCE)
    return run_chat_turn(
        db,
        audience=FAMILY_AUDIENCE,
        owner_id=user_id,
        conversation_id=body.conversation_id,
        message=body.message,
        scope=scope,
        build_prompt=lambda chunks: build_family_prompt(
            chunks,
            profile,
            body.calculator_result.model_dump() if body.calculator_result else None,
            body.college_aid_result.model_dump() if body.college_aid_result else None,
            guidance=guidance,
        ),
        profile=profile,
        school_slug=school.slug if school else None,
    )


@router.get("/conversations", response_model=AssistantConversationListOut)
def list_conversations(user_id: AssistantUserDep, db: DbDep, response: Response) -> AssistantConversationListOut:
    response.headers["Cache-Control"] = "private, no-store"
    rows = db.scalars(
        select(AssistantConversation)
        .where(
            AssistantConversation.local_user_id == user_id, AssistantConversation.audience == FAMILY_AUDIENCE
        )
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
    conversation = owned_conversation(db, conversation_id, user_id, FAMILY_AUDIENCE)
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
    db.delete(owned_conversation(db, conversation_id, user_id, FAMILY_AUDIENCE))
    db.commit()
    return Response(status_code=204)
