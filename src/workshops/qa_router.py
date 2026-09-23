"""Admin endpoints for webinar Q&A — prefix /api/v1/admin/webinar-qa.

The list, one editable question, and the two levers that re-run the machinery
behind it. All super_admin only (``AdminDep``).

Two rules hold this together. Nothing here decides what an answer is — that is
``qa_answer.resolve_answer``, reached through the schema layer, so the screen and
any future export cannot drift apart. And an admin edit writes only to the
override columns, which no sync or extraction ever touches, so a correction
survives every re-pull.
"""

from __future__ import annotations

import logging
import uuid
from datetime import date, datetime, time, timedelta, timezone

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, joinedload, selectinload

from src.auth.deps import AdminDep
from src.db.deps import DbDep
from src.workshops.models import Webinar
from src.workshops.qa_answer import has_answer_expression
from src.workshops.qa_extraction_service import extract_answers
from src.workshops.qa_models import CLASSIFICATIONS, WebinarQaQuestion
from src.workshops.qa_schemas import (
    QaQuestionDetail,
    QaQuestionList,
    QaQuestionUpdate,
    QaSyncResult,
    to_detail,
    to_summary,
)
from src.workshops.qa_sync_service import sync_webinar_qa

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/admin/webinar-qa", tags=["webinar-qa-admin"])


def _loaded(statement):
    """Pull the webinar, workshop and extraction history in with the questions.

    Every one of them is read for every row — the resolved answer comes out of
    the extractions — so lazy loading here is N+1 by construction.
    """
    return statement.options(
        joinedload(WebinarQaQuestion.webinar).joinedload(Webinar.workshop),
        selectinload(WebinarQaQuestion.extractions),
    )


def _load(db: Session, question_id: uuid.UUID) -> WebinarQaQuestion:
    question = db.execute(
        _loaded(select(WebinarQaQuestion).where(WebinarQaQuestion.id == question_id))
    ).unique().scalar_one_or_none()
    if question is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Question not found")
    return question


def _webinar(db: Session, webinar_id: uuid.UUID) -> Webinar:
    webinar = db.get(Webinar, webinar_id)
    if webinar is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Webinar not found")
    return webinar


# Everything the classifier can say that is not a question worth answering.
# Derived from CLASSIFICATIONS rather than retyped, so a label added there is
# never silently left visible here.
_NOISE_LABELS = tuple(c for c in CLASSIFICATIONS if c != "question")

def _day_start(day: date) -> datetime:
    """Midnight UTC on ``day``, for comparing against a timestamptz column.

    The date filter is a UTC day, not the admin's local one. Webinars run in US
    business hours, so a UTC day boundary never splits a session in two — which
    is the only way this choice could surprise anyone reading the list.
    """
    return datetime.combine(day, time.min, tzinfo=timezone.utc)


# An admin's relabel outranks the model's everywhere a label is read.
_LABEL = func.coalesce(
    WebinarQaQuestion.classification_override, WebinarQaQuestion.classification
)


@router.get("/questions", response_model=QaQuestionList)
def list_questions(
    _admin: AdminDep,
    db: DbDep,
    webinar_id: uuid.UUID | None = Query(None, description="Filter to one webinar"),
    classification: str | None = Query(None, description="question, greeting, thanks, comment, spam"),
    answer_source: str | None = Query(None, description="typed, live, unanswered"),
    answered: bool | None = Query(
        None, description="true: has an answer; false: still needs one; omit: both"
    ),
    include_noise: bool = Query(
        False, description="Include greetings, thanks, comments and spam"
    ),
    search: str | None = Query(None, description="Substring of the question text"),
    date_from: date | None = Query(None, description="Asked on or after this day (UTC)"),
    date_to: date | None = Query(None, description="Asked on or before this day (UTC), inclusive"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> QaQuestionList:
    """Questions oldest-first — the order they were asked in, which is how the
    session read.

    The noise the classifier labelled is out by default: a Q&A panel collects
    far more "Thank you!" and "This is excellent" than questions, and listing
    them all buries what the admin opened the page to read. Asking for a label
    by name brings that label back.

    An unclassified question is NOT noise and stays in the list. Labelling
    reaches Bedrock and can fail, so treating a missing label as noise would
    silently drop real questions on exactly the runs that went wrong.

    Both filters honour an admin's override, so a question they relabelled is
    found under the label they gave it, not the model's. So does ``answered``,
    which counts an admin's typed correction as an answer like any other.

    ``date_from``/``date_to`` bound when the question was asked, not when its
    webinar was scheduled — a question typed into the panel after the session
    overran belongs to the day it was actually asked. Both ends are inclusive.
    A question Zoom gave no timestamp for is out of every dated range; there is
    no day it could honestly be placed on.
    """
    filters = []
    if webinar_id:
        filters.append(WebinarQaQuestion.webinar_id == webinar_id)
    if classification:
        if classification not in CLASSIFICATIONS:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Unknown classification '{classification}'",
            )
        filters.append(_LABEL == classification)
    elif not include_noise:
        # Explicit `classification` wins: asking for thanks must return thanks.
        filters.append(or_(_LABEL.is_(None), _LABEL.notin_(_NOISE_LABELS)))
    if answer_source:
        filters.append(WebinarQaQuestion.answer_source == answer_source)
    if answered is not None:
        # Deliberately not the same question as `answer_source`. That records
        # what Zoom said happened in the session; this asks whether any answer
        # text actually exists now — a live-answered question has none until the
        # transcript extraction recovers it, and those are exactly the rows an
        # admin is hunting for.
        expression = has_answer_expression()
        filters.append(expression if answered else ~expression)
    if search:
        term = f"%{search.strip()}%"
        filters.append(
            or_(
                WebinarQaQuestion.question_text.ilike(term),
                WebinarQaQuestion.asker_name.ilike(term),
            )
        )
    if date_from:
        filters.append(WebinarQaQuestion.asked_at >= _day_start(date_from))
    if date_to:
        # Exclusive bound one day on, so the whole of `date_to` is included —
        # comparing against that day's midnight would drop everything asked
        # during it, which is every question on a webinar held that day.
        filters.append(WebinarQaQuestion.asked_at < _day_start(date_to + timedelta(days=1)))

    total = int(
        db.execute(
            select(func.count()).select_from(WebinarQaQuestion).where(*filters)
        ).scalar_one()
    )
    questions = (
        db.execute(
            _loaded(select(WebinarQaQuestion))
            .where(*filters)
            .order_by(WebinarQaQuestion.asked_at, WebinarQaQuestion.created_at)
            .limit(limit)
            .offset(offset)
        )
        .unique()
        .scalars()
        .all()
    )
    return QaQuestionList(
        items=[to_summary(q) for q in questions], total=total, limit=limit, offset=offset
    )


@router.get("/questions/{question_id}", response_model=QaQuestionDetail)
def get_question(question_id: uuid.UUID, _admin: AdminDep, db: DbDep) -> QaQuestionDetail:
    """One question with every extraction ever run against it."""
    return to_detail(_load(db, question_id))


@router.patch("/questions/{question_id}", response_model=QaQuestionDetail)
def update_question(
    question_id: uuid.UUID, payload: QaQuestionUpdate, admin: AdminDep, db: DbDep
) -> QaQuestionDetail:
    """Correct an answer, relabel a question, or hide it.

    Writes only the override columns. Zoom's own answer text and the model's
    verdicts stay exactly as they were recorded, so the correction can always be
    compared against — and undone back to — what actually came in.
    """
    question = _load(db, question_id)
    changed = payload.model_dump(exclude_unset=True)

    if "answer_text_override" in changed:
        # Empty means "take my correction back off", not "the answer is blank".
        question.answer_text_override = (changed["answer_text_override"] or "").strip() or None
    if "classification_override" in changed:
        label = (changed["classification_override"] or "").strip() or None
        if label is not None and label not in CLASSIFICATIONS:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Unknown classification '{label}'",
            )
        question.classification_override = label

    if changed:
        question.edited_by_user_id = admin.user_id
        question.edited_at = datetime.now(tz=timezone.utc)
    db.commit()
    db.refresh(question)
    return to_detail(question)


@router.post("/webinars/{webinar_id}/resync", response_model=QaSyncResult)
def resync_webinar(webinar_id: uuid.UUID, _admin: AdminDep, db: DbDep) -> QaSyncResult:
    """Pull the Zoom Q&A report again for one webinar.

    Returns ``ok=false`` rather than an error when Zoom has not produced the
    report yet: that is a wait, not a failure, and the same call works later.
    """
    webinar = _webinar(db, webinar_id)
    if not webinar.zoom_webinar_id:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="This webinar has no Zoom webinar id to pull a report for",
        )

    ok = sync_webinar_qa(webinar.zoom_webinar_id, db)
    count = int(
        db.execute(
            select(func.count())
            .select_from(WebinarQaQuestion)
            .where(WebinarQaQuestion.webinar_id == webinar_id)
        ).scalar_one()
    )
    return QaSyncResult(
        webinar_id=webinar_id,
        ok=ok,
        detail="Q&A report synced" if ok else "Zoom has not produced the Q&A report yet",
        question_count=count,
    )


@router.post("/webinars/{webinar_id}/re-extract", response_model=QaSyncResult)
def re_extract_webinar(webinar_id: uuid.UUID, _admin: AdminDep, db: DbDep) -> QaSyncResult:
    """Run transcript answer extraction again for one webinar.

    Appends a fresh set of verdicts; it never overwrites the old ones or an
    admin's correction. Synchronous — it is one model call and the admin is
    waiting on the result.
    """
    _webinar(db, webinar_id)
    written = extract_answers(db, webinar_id)
    return QaSyncResult(
        webinar_id=webinar_id,
        ok=written > 0,
        detail=(
            f"Extraction run over {written} question(s)"
            if written
            else "No live-answered questions to extract for this webinar"
        ),
        question_count=written,
    )
