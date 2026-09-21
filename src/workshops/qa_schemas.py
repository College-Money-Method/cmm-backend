"""Response and request bodies for the admin Q&A endpoints.

Every question leaves here with ``resolved_answer`` already decided by
``qa_answer.resolve_answer``. The screen renders what it is given and never
re-derives it — three copies of a precedence rule is three chances for the page,
the API and an export to disagree about what a panel actually said.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict

from src.workshops.qa_answer import resolve_answer, resolve_classification
from src.workshops.qa_models import WebinarQaAnswerExtraction, WebinarQaQuestion


class QaExtraction(BaseModel):
    """One attempt at recovering a spoken answer. History, not just the winner."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    video_job_id: uuid.UUID | None = None
    status: str
    answer_text: str | None = None
    answered_by: str | None = None
    transcript_start_seconds: int | None = None
    transcript_end_seconds: int | None = None
    transcript_excerpt: str | None = None
    confidence: Decimal | None = None
    model_id: str | None = None
    prompt_version: str | None = None
    created_at: datetime


class QaQuestionSummary(BaseModel):
    """One row of the Q&A list."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    webinar_id: uuid.UUID
    webinar_name: str | None = None
    workshop_name: str | None = None
    zoom_question_id: str
    question_text: str
    asked_at: datetime | None = None
    asker_name: str | None = None
    asker_email: str | None = None
    is_anonymous: bool = False
    # How Zoom recorded the answer: typed by a panelist, given out loud, or not
    # at all. Distinct from `resolved_answer_source`, which says where the text
    # below actually came from.
    answer_source: str
    answer_visibility: str | None = None
    responder_name: str | None = None
    classification: str | None = None
    classified_by: str | None = None
    has_override: bool = False
    # The answer to show, and the layer it came from. Decided server-side.
    resolved_answer: str | None = None
    resolved_answer_source: str = "unanswered"
    # Present only for an answer recovered from the recording: the speaker and
    # the seconds on the published replay's clock, so the UI can deep-link.
    resolved_answered_by: str | None = None
    resolved_start_seconds: int | None = None
    resolved_end_seconds: int | None = None
    # How sure the model was, for a recovered answer only. An admin scanning the
    # list uses it to decide which answers are worth reading closely.
    resolved_confidence: float | None = None
    extraction_count: int = 0


class QaQuestionDetail(QaQuestionSummary):
    """One question with everything behind the resolved answer."""

    typed_answer_text: str | None = None
    answer_text_override: str | None = None
    classification_override: str | None = None
    question_status: str | None = None
    marked_answered_at: datetime | None = None
    responder_email: str | None = None
    registration_id: uuid.UUID | None = None
    edited_by_user_id: uuid.UUID | None = None
    edited_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    extractions: list[QaExtraction] = []


class QaQuestionList(BaseModel):
    items: list[QaQuestionSummary]
    total: int
    limit: int
    offset: int


class QaQuestionUpdate(BaseModel):
    """An admin edit. Every field optional — only what is sent is changed.

    Passing an empty string clears an override rather than storing a blank one,
    which is how an admin takes their correction back off a question.
    """

    answer_text_override: str | None = None
    classification_override: str | None = None


class QaSyncResult(BaseModel):
    """Outcome of a manual re-pull or re-extraction."""

    webinar_id: uuid.UUID
    # False from a resync means Zoom has not produced the report yet — worth
    # trying again later, not a failure of the request.
    ok: bool
    detail: str
    question_count: int = 0


def _summary_fields(question: WebinarQaQuestion) -> dict:
    answer = resolve_answer(question)
    webinar = question.webinar
    workshop = getattr(webinar, "workshop", None) if webinar else None
    return {
        "webinar_name": getattr(webinar, "webinar_name", None),
        "workshop_name": getattr(workshop, "name", None),
        "classification": resolve_classification(question),
        "has_override": bool((question.answer_text_override or "").strip()),
        "resolved_answer": answer.text,
        "resolved_answer_source": answer.source,
        "resolved_answered_by": answer.answered_by,
        "resolved_start_seconds": answer.start_seconds,
        "resolved_end_seconds": answer.end_seconds,
        "resolved_confidence": answer.confidence,
        "extraction_count": len(question.extractions),
    }


def to_summary(question: WebinarQaQuestion) -> QaQuestionSummary:
    return QaQuestionSummary.model_validate(question).model_copy(
        update=_summary_fields(question)
    )


def to_detail(question: WebinarQaQuestion) -> QaQuestionDetail:
    fields = _summary_fields(question)
    # Newest verdict first: a re-run under a better prompt is what an admin
    # checking a doubtful answer wants to see at the top.
    fields["extractions"] = [
        QaExtraction.model_validate(e)
        for e in sorted(question.extractions, key=lambda e: e.created_at, reverse=True)
    ]
    return QaQuestionDetail.model_validate(question).model_copy(update=fields)
