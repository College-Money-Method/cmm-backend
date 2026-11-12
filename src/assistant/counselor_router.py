"""Counselor assistant API: hub login, streamed chat, per-user history.

Same event contract as the family assistant. Identity comes from the verified
hub bearer; the school comes from the user's role (a super_admin names it), and
retrieval plus workshops are bound to that school. A family's or another
counselor's conversation answers 404.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.responses import StreamingResponse
from sqlalchemy import select

from src.assistant.chat_turn import owned_conversation, run_chat_turn
from src.assistant.counselor_prompt import build_counselor_prompt
from src.assistant.counselor_schemas import CounselorChatIn
from src.assistant.counselor_workshop_section import school_workshop_section
from src.assistant.family_school_scope import scope_for_school
from src.assistant.models import AssistantConversation
from src.assistant.prompt_sections import prompt_guidance
from src.assistant.schemas import AssistantConversationListOut, AssistantConversationOut
from src.auth.deps import get_current_user
from src.auth.hub_school import resolve_hub_school_id
from src.auth.schemas import CurrentUser
from src.db.deps import DbDep
from src.schools.models import School

router = APIRouter(prefix="/api/v1/assistant/counselor", tags=["assistant-counselor"])

COUNSELOR_AUDIENCE = "counselor"
CONVERSATION_LIST_LIMIT = 50
_ALLOWED_ROLES = ("super_admin", "hub_admin", "hub_user")


def require_counselor_assistant(user: Annotated[CurrentUser, Depends(get_current_user)]) -> CurrentUser:
    """Staff who run a school's hub; viewers are read-only and stay out."""
    if user.role not in _ALLOWED_ROLES:
        raise HTTPException(status_code=403, detail="counselor_assistant_forbidden")
    return user


CounselorAssistantUser = Annotated[CurrentUser, Depends(require_counselor_assistant)]


@router.post("/chat")
def chat(body: CounselorChatIn, user: CounselorAssistantUser, db: DbDep) -> StreamingResponse:
    """Answer one counselor question from counselor knowledge, streamed as server-sent events."""
    # Resolved before any embedding so a refused school costs no Bedrock call.
    school = db.get(School, resolve_hub_school_id(user, body.school_id))
    if school is None:
        raise HTTPException(status_code=404, detail="assistant_school_not_found")
    # Admins switch the assistant on per school; a super admin may test any school.
    if user.role != "super_admin" and not school.counselor_assistant_enabled:
        raise HTTPException(status_code=403, detail="counselor_assistant_disabled")
    scope = scope_for_school(school, COUNSELOR_AUDIENCE, "full")
    workshops = school_workshop_section(db, school, datetime.now(UTC))
    guidance = prompt_guidance(db, COUNSELOR_AUDIENCE)
    return run_chat_turn(
        db,
        audience=COUNSELOR_AUDIENCE,
        owner_id=user.user_id,
        conversation_id=body.conversation_id,
        message=body.message,
        scope=scope,
        build_prompt=lambda chunks: build_counselor_prompt(chunks, school, workshops, guidance),
        profile={"school_id": str(school.id)},
        school_slug=school.slug,
    )


@router.get("/conversations", response_model=AssistantConversationListOut)
def list_conversations(user: CounselorAssistantUser, db: DbDep, response: Response) -> AssistantConversationListOut:
    response.headers["Cache-Control"] = "private, no-store"
    rows = db.scalars(
        select(AssistantConversation)
        .where(
            AssistantConversation.local_user_id == user.user_id,
            AssistantConversation.audience == COUNSELOR_AUDIENCE,
        )
        .order_by(AssistantConversation.updated_at.desc())
        .limit(CONVERSATION_LIST_LIMIT)
    ).all()
    return AssistantConversationListOut(
        items=[{"id": c.id, "title": c.title, "updated_at": c.updated_at} for c in rows]
    )


@router.get("/conversations/{conversation_id}", response_model=AssistantConversationOut)
def get_conversation(
    conversation_id: uuid.UUID, user: CounselorAssistantUser, db: DbDep, response: Response
) -> AssistantConversationOut:
    response.headers["Cache-Control"] = "private, no-store"
    conversation = owned_conversation(db, conversation_id, user.user_id, COUNSELOR_AUDIENCE)
    return AssistantConversationOut(
        id=conversation.id,
        title=conversation.title,
        messages=[
            {"id": m.id, "role": m.role, "content": m.content, "sources": m.sources or [], "created_at": m.created_at}
            for m in conversation.messages
        ],
    )


@router.delete("/conversations/{conversation_id}", status_code=204)
def delete_conversation(conversation_id: uuid.UUID, user: CounselorAssistantUser, db: DbDep) -> Response:
    db.delete(owned_conversation(db, conversation_id, user.user_id, COUNSELOR_AUDIENCE))
    db.commit()
    return Response(status_code=204)
