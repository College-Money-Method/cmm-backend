"""Admin-written instructions appended to the assistant's system prompt.

Admins manage them as titled sections (see ``admin_settings_router``); each one
targets the family assistant, the counselor assistant or both. Enabled sections
for the asking assistant go in after the built-in rules, oldest first.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.assistant.models import AssistantPromptSection

PROMPT_SECTION_AUDIENCES = ("family", "counselor", "both")

_HEADER = (
    "Team guidance (written by the College Money Method team; follow it together with the rules "
    "above, and where it covers the same situation as a rule above, follow the guidance):"
)


def prompt_guidance(db: Session, audience: str) -> str:
    """The enabled sections for ``audience`` as one prompt block, or "" when there are none.

    A non-empty block ends with a blank line so the template can place it
    directly before the next section.
    """
    rows = db.scalars(
        select(AssistantPromptSection)
        .where(
            AssistantPromptSection.enabled.is_(True),
            AssistantPromptSection.audience.in_((audience, "both")),
        )
        .order_by(AssistantPromptSection.created_at, AssistantPromptSection.id)
    ).all()
    parts = [f"### {row.title.strip()}\n{row.body.strip()}" for row in rows if row.body.strip()]
    if not parts:
        return ""
    return _HEADER + "\n\n" + "\n\n".join(parts) + "\n\n"
