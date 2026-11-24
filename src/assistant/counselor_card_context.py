"""Hidden context for a counselor message sent from one of the Hub's job cards.

The counselor's words are sent and stored as typed ("Is the CSS Profile
required?", "juniors"). The card they started from travels beside the message
as ``card_type``, and only this turn gets:

* ``prompt``: one line in the system prompt saying what the message is, so the
  matching rule applies without wrapping the counselor's words in ours;
* ``search``: words added to the retrieval query, for cards whose answer is a
  bare name or topic ("FAFSA Live", "CSS Profile") that would retrieve poorly alone.

Later turns rely on the conversation history, as any follow-up does.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

# ``finding_resources`` left the Hub's cards but stays valid, so links and history that carry it still work.
CounselorCardType = Literal[
    "family_advising", "program_progress", "workshop_promotion", "grade_checklist", "finding_resources"
]


@dataclass(frozen=True)
class CardContext:
    prompt: str
    search: str = ""


CARD_CONTEXT: dict[str, CardContext] = {
    "family_advising": CardContext(
        prompt=(
            'The counselor started from "Advising a student or family": their message is a question a '
            "student or family asked them. Follow the rule for a family's question: answer it for the "
            "counselor, then draft the reply to the family."
        ),
    ),
    "program_progress": CardContext(
        prompt=(
            'The counselor started from "Understanding program engagement": their message says what '
            "they want to know about this year's program. Follow the rule for how the program is going."
        ),
    ),
    "workshop_promotion": CardContext(
        prompt=(
            'The counselor started from "Promoting our program": their message names the workshop. '
            "Draft an email inviting families to it, following the email and workshop rules."
        ),
        search="workshop announcement email inviting families to register",
    ),
    "grade_checklist": CardContext(
        prompt=(
            'The counselor started from "Making a checklist": their message names the grade or the '
            "families, and may name a timeframe. Make a dated checklist for those families' parents, "
            "following the checklist rule."
        ),
        search="checklist for parents: what families of this grade should do next, deadlines by month",
    ),
    "finding_resources": CardContext(
        prompt=(
            'The counselor started from "Finding Resources": their message names a topic. List the CMM '
            "resources among the sources that cover it, each as its linked title and one line on what it "
            "covers, most useful first, then offer a short note to share them with families. If none "
            "cover the topic, say so plainly."
        ),
        search="CMM resources, guides and videos for families on this topic",
    ),
}


def card_prompt(card_type: str | None) -> str:
    """The system-prompt line for this card, or "" for a typed question."""
    card = CARD_CONTEXT.get(card_type) if card_type else None
    return card.prompt if card else ""


def card_search(card_type: str | None) -> str:
    """Words added to this turn's retrieval query, or ""."""
    card = CARD_CONTEXT.get(card_type) if card_type else None
    return card.search if card else ""
