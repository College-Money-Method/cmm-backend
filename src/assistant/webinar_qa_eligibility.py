"""Which answered webinar questions the assistant may learn from, and why not.

Answered Zoom Q&A goes into the knowledge base automatically, so the filter is
the quality gate. One function decides it for both readers: the sync job that
indexes questions and the admin Q&A screen that shows "used by the assistant"
next to each one. If they decided separately, an admin could be told a question
is used when the index left it out.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

from src.workshops.qa_answer import resolve_answer, resolve_classification
from src.workshops.qa_models import WebinarQaQuestion

# Aid rules change every cycle; an answer older than this is more likely stale.
MAX_AGE = timedelta(days=365)
# Below this an answer is usually "yes", "great question" or "we'll cover that".
MIN_ANSWER_CHARS = 40
# Recovered (spoken) answers are a model's reading of the recording.
MIN_RECOVERED_CONFIDENCE = 0.7

SKIP_REASONS = (
    "excluded",
    "not_a_question",
    "unanswered",
    "private_answer",
    "unknown_visibility",
    "too_short",
    "low_confidence",
    "too_old",
)

_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
# Toll-free numbers (800, 888, ...) belong to organisations, like the federal
# student aid line, and are kept.
_PHONE_RE = re.compile(
    r"(?<!\d)(?:\+?1[\s.-]?)?(?!\(?8(?:00|33|44|55|66|77|88)\b)\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}(?!\d)"
)


def asked_on(question: WebinarQaQuestion) -> datetime | None:
    """When the question was asked, or the webinar's start when Zoom gave no time."""
    asked = question.asked_at or getattr(question.webinar, "start_datetime", None)
    if asked is not None and asked.tzinfo is None:
        asked = asked.replace(tzinfo=UTC)
    return asked


def skip_reason(question: WebinarQaQuestion, now: datetime | None = None) -> str | None:
    """None when the assistant may use this question, else the first reason it may not."""
    if question.exclude_from_assistant:
        return "excluded"
    # Unlabelled counts as a question, as on the admin list: labelling can fail,
    # and a stray "thanks" is caught by the answer-length rule anyway.
    if resolve_classification(question) not in (None, "question"):
        return "not_a_question"
    answer = resolve_answer(question)
    if not answer.text:
        return "unanswered"
    # A private typed answer was written for one attendee. An admin's own
    # answer replaces it, so it is no longer private. Zoom only reports
    # visibility since April 2026; older typed answers are kept out because
    # some of them were private replies.
    if answer.source == "typed" and question.answer_visibility != "public":
        return "private_answer" if question.answer_visibility == "private" else "unknown_visibility"
    if len(answer.text.strip()) < MIN_ANSWER_CHARS:
        return "too_short"
    if answer.source == "extracted" and (answer.confidence or 0) < MIN_RECOVERED_CONFIDENCE:
        return "low_confidence"
    asked = asked_on(question)
    if asked is None or asked < (now or datetime.now(UTC)) - MAX_AGE:
        return "too_old"
    return None


def scrub_contact_details(text: str, asker_name: str | None = None) -> str:
    """Drop email addresses, phone numbers and the asker's name from the text.

    Presenters often read the name out ("Great question, Maria"), so it reaches
    recovered answers as well as typed ones.
    """
    text = _PHONE_RE.sub("[phone]", _EMAIL_RE.sub("[email]", text))
    for part in sorted(set((asker_name or "").split()), key=len, reverse=True):
        if len(part) > 2:
            text = re.sub(rf"\b{re.escape(part)}\b", "[name]", text, flags=re.IGNORECASE)
    return text
