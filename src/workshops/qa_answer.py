"""The one place that decides what a question's answer *is*.

A question can carry up to three answers at once: what an admin typed to correct
the record, what a panelist typed into Zoom, and what the model recovered from
the recording. They are stored separately on purpose — the ingested fact must
stay recoverable — so something has to pick, and if the API, the admin page and a
future warehouse export each pick for themselves they will eventually disagree
about what was said on a webinar. That is the bug this module exists to prevent.

Precedence, highest first:

1. ``answer_text_override`` — an admin has looked at it and decided
2. ``typed_answer_text`` — the panelist's own words, straight from Zoom
3. the best ``extracted`` transcript extraction — what was said out loud
4. nothing — the question went unanswered

Extractions are append-only, so "best" needs a rule: the newest prompt version
first, then the most confident, then the most recent.
"""

from __future__ import annotations

from typing import NamedTuple

from sqlalchemy import ColumnElement, func, or_, select

from src.workshops.qa_models import WebinarQaAnswerExtraction, WebinarQaQuestion


class ResolvedAnswer(NamedTuple):
    """The answer to show, and which of the three layers it came from."""

    text: str | None
    # "override" | "typed" | "extracted" | "unanswered"
    source: str
    # Set only for an extracted answer: who said it, where in the replay, and
    # how sure the model was. The confidence travels with the answer because the
    # list screen shows it beside every recovered answer — re-deriving it there
    # would mean re-implementing `best_extraction` in the browser.
    answered_by: str | None = None
    start_seconds: int | None = None
    end_seconds: int | None = None
    confidence: float | None = None


def best_extraction(question: WebinarQaQuestion) -> WebinarQaAnswerExtraction | None:
    """The extraction a reader should be shown, or None if there is no usable one.

    ``prompt_version`` is compared as a string, which orders "v1" < "v2" but
    would order "v10" < "v9". Bump versions with that in mind — zero-pad past
    nine rather than changing the stored history's meaning.
    """
    usable = [e for e in question.extractions if e.status == "extracted" and e.answer_text]
    if not usable:
        return None
    return max(
        usable,
        key=lambda e: (
            e.prompt_version or "",
            float(e.confidence) if e.confidence is not None else 0.0,
            e.created_at,
        ),
    )


def resolve_answer(question: WebinarQaQuestion) -> ResolvedAnswer:
    """Apply the precedence above to one question."""
    override = (question.answer_text_override or "").strip()
    if override:
        return ResolvedAnswer(text=override, source="override")

    typed = (question.typed_answer_text or "").strip()
    if typed:
        return ResolvedAnswer(text=typed, source="typed")

    extraction = best_extraction(question)
    if extraction is not None:
        return ResolvedAnswer(
            text=extraction.answer_text,
            source="extracted",
            answered_by=extraction.answered_by,
            start_seconds=extraction.transcript_start_seconds,
            end_seconds=extraction.transcript_end_seconds,
            confidence=(
                float(extraction.confidence) if extraction.confidence is not None else None
            ),
        )

    return ResolvedAnswer(text=None, source="unanswered")


def resolve_classification(question: WebinarQaQuestion) -> str | None:
    """An admin's label wins over the model's, same as their answer does."""
    return question.classification_override or question.classification


def _non_empty(column) -> ColumnElement[bool]:
    """True when the column holds something other than blanks."""
    return func.coalesce(func.trim(column), "") != ""


def has_answer_expression() -> ColumnElement[bool]:
    """SQL twin of ``resolve_answer``: does this question have an answer at all?

    Filtering happens in the database — a list screen cannot load every row to
    ask ``resolve_answer`` — so the precedence above has to be expressed twice.
    Both live in this file precisely so they are changed together: a layer added
    to ``resolve_answer`` and forgotten here would give an admin a filter that
    hides answers the page then displays.

    Only *whether* there is an answer, not which one wins. That makes it
    indifferent to precedence order, so a reshuffle of the rules above leaves
    this correct as long as the set of layers is the same.
    """
    return or_(
        _non_empty(WebinarQaQuestion.answer_text_override),
        _non_empty(WebinarQaQuestion.typed_answer_text),
        select(1)
        .where(
            WebinarQaAnswerExtraction.question_id == WebinarQaQuestion.id,
            WebinarQaAnswerExtraction.status == "extracted",
            _non_empty(WebinarQaAnswerExtraction.answer_text),
        )
        .exists(),
    )
