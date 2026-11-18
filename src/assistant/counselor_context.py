"""Everything the counselor prompt needs about one school, assembled once per turn.

The chat endpoint and the offline eval both build the prompt here, so a
calibration run grades exactly the prompt counselors get.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from src.assistant.counselor_card_context import card_prompt
from src.assistant.counselor_program_snapshot import program_snapshot_section
from src.assistant.counselor_prompt import build_counselor_prompt
from src.assistant.counselor_template_values import (
    fill_template_chunks,
    own_name,
    template_values,
    template_values_section,
)
from src.assistant.counselor_workshop_section import school_workshop_section
from src.assistant.prompt_sections import prompt_guidance
from src.assistant.search import RetrievedChunk
from src.emails.counselor_resolver import resolve_counselor_name
from src.emails.school_links import email_origin
from src.schools.models import School

COUNSELOR_AUDIENCE = "counselor"


def counselor_prompt_builder(
    db: Session,
    school: School,
    user_id: uuid.UUID | None,
    *,
    guidance: str | None = None,
    card_type: str | None = None,
) -> Callable[[list[RetrievedChunk]], str]:
    """A ``build_prompt`` for ``run_chat_turn``; ``guidance`` overrides the admin sections (eval only),
    and ``card_type`` names the Hub job card the message came from."""
    workshops = school_workshop_section(db, school, datetime.now(UTC))
    program = program_snapshot_section(db, school)
    if guidance is None:
        guidance = prompt_guidance(db, COUNSELOR_AUDIENCE)
    # The asker's name and school fill the drafts' merge tags, also when a super admin tests a school.
    own = own_name(db, user_id) if user_id else None
    values = template_values(school, own or resolve_counselor_name(db, school.id))
    values_section = template_values_section(values)
    asker_name = own[2] if own else ""
    origin = email_origin()
    card = card_prompt(card_type)
    return lambda chunks: build_counselor_prompt(
        fill_template_chunks(chunks, values), school, workshops, guidance, values_section, asker_name,
        program=program, origin=origin, card=card,
    )
