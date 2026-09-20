"""Recover the answers that were only ever spoken out loud.

Zoom's Q&A report marks a question "live answered" and stores no answer text for
it — the answer exists only in the recording. This module collects those
questions, hands a transcript and the questions to the model in one call, and
stores a verdict per question. The rules for reading a verdict live in
``qa_extraction_match``.

One thing about the call itself is load-bearing: **the whole transcript goes in,
unwindowed.** Panelists answer questions as they watch them arrive and bulk-mark
them resolved much later, so more than half the answers precede their question's
Zoom timestamp. Any forward-looking window drops them.

Where the transcript comes from is deliberately not fixed here.
``extract_answers`` uses the artefact the video pipeline left in S3;
``extract_answers_from_cues`` takes cues from anywhere, which is what lets a
replay that predates the pipeline be filled from its Vimeo captions.

Every run appends. Nothing here updates a previous verdict or touches an admin's
override.
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.config import settings
from src.video_pipeline import artifact_store
from src.video_pipeline.bedrock_client import BedrockCallError, call_json
from src.video_pipeline.models import WebinarVideoJob
from src.video_pipeline.states import JobState
from src.workshops.qa_extraction_match import (
    PROMPT_VERSION,
    SYSTEM,
    TOLERANCE_SECONDS,
    build_extraction,
    cue_index,
)
from src.workshops.qa_models import WebinarQaAnswerExtraction, WebinarQaQuestion

logger = logging.getLogger(__name__)

# Kept importable from here: this module was the only home for these before the
# matching rules moved out, and callers address them by these names.
_cue_index = cue_index
__all__ = [
    "PROMPT_VERSION",
    "SYSTEM",
    "TOLERANCE_SECONDS",
    "extract_answers",
    "extract_answers_from_cues",
    "live_questions",
]


def live_questions(db: Session, webinar_id: uuid.UUID) -> list[WebinarQaQuestion]:
    """The questions worth spending a model call on for this webinar.

    Live-answered and classified as an actual question — a "thanks!" nobody
    replied to in writing is not an answer waiting to be found in the recording.
    """
    return list(
        db.scalars(
            select(WebinarQaQuestion)
            .where(
                WebinarQaQuestion.webinar_id == webinar_id,
                WebinarQaQuestion.answer_source == "live",
                WebinarQaQuestion.classification == "question",
            )
            .order_by(WebinarQaQuestion.asked_at)
        ).all()
    )


def _published_job(db: Session, webinar_id: uuid.UUID) -> WebinarVideoJob | None:
    """The newest published job for this webinar that left a transcript behind.

    Several jobs can share a webinar — a retry, or a re-publish — so "newest
    published" is the tie-break. An audit run never qualifies: it carries no
    ``webinar_id`` at all.
    """
    return db.scalars(
        select(WebinarVideoJob)
        .where(
            WebinarVideoJob.webinar_id == webinar_id,
            WebinarVideoJob.state == JobState.PUBLISHED.value,
            WebinarVideoJob.frames_prefix.is_not(None),
        )
        .order_by(WebinarVideoJob.created_at.desc())
    ).first()


def _record_failure(
    db: Session, questions: list[WebinarQaQuestion], reason: str, job_id: uuid.UUID | None
) -> int:
    """Write one ``failed`` row per question saying why nothing could be extracted.

    A failed row is never read as an answer — the API only resolves text from
    ``extracted`` — so the reason can live in ``answer_text`` where an admin
    looking at the row will actually see it.
    """
    for question in questions:
        db.add(
            WebinarQaAnswerExtraction(
                question_id=question.id,
                video_job_id=job_id,
                status="failed",
                answer_text=reason,
                prompt_version=PROMPT_VERSION,
            )
        )
    db.commit()
    logger.warning("Q&A extraction failed for %d question(s) — %s", len(questions), reason)
    return len(questions)


def extract_answers(db: Session, webinar_id: uuid.UUID) -> int:
    """Find spoken answers using the transcript the video pipeline published.

    Returns the number of extraction rows written. Commits. Never raises: this
    runs off the back of a publish, and a replay that is live must not be held
    back by a model call.
    """
    questions = live_questions(db, webinar_id)
    if not questions:
        # Ingest and publish are independent and arrive in either order. Nothing
        # to do yet is the normal case, not a problem.
        return 0

    job = _published_job(db, webinar_id)
    if job is None:
        return _record_failure(db, questions, "No published video job for this webinar", None)

    try:
        raw_cues = artifact_store.load_json_artifact(
            job.frames_prefix, artifact_store.TRANSCRIPT_FILENAME
        )
    except artifact_store.ArtifactError as exc:
        return _record_failure(db, questions, f"Transcript unavailable: {exc}", job.id)

    cues = [c for c in raw_cues if isinstance(c, dict)] if isinstance(raw_cues, list) else []
    if not cues:
        return _record_failure(db, questions, "Transcript artefact is empty", job.id)

    return extract_answers_from_cues(db, webinar_id, cues, job=job, questions=questions)


def extract_answers_from_cues(
    db: Session,
    webinar_id: uuid.UUID,
    cues: list[dict],
    *,
    job: WebinarVideoJob | None = None,
    questions: list[WebinarQaQuestion] | None = None,
) -> int:
    """Match one webinar's live-answered questions against the cues given.

    ``job`` is optional because a transcript need not come from the pipeline. A
    replay captioned on Vimeo has no job, so the rows it writes carry a null
    ``video_job_id`` and skip the causality check, which has no recording clock
    to work from. Everything else — the prompt, the span validation, the
    append-only storage — is identical either way.
    """
    if questions is None:
        questions = live_questions(db, webinar_id)
    if not questions or not cues:
        return 0

    transcript = "\n".join(f"#{i} {c.get('text') or ''}" for i, c in enumerate(cues))
    qlist = "\n".join(f"{i}. {q.question_text}" for i, q in enumerate(questions))
    try:
        parsed, input_tokens, output_tokens = call_json(
            system=SYSTEM,
            content=f"TRANSCRIPT:\n{transcript}\n\nQUESTIONS:\n{qlist}",
            max_tokens=8192,
        )
    except BedrockCallError as exc:
        return _record_failure(
            db, questions, f"Model call failed: {exc}", job.id if job else None
        )

    by_index: dict[int, dict] = {}
    for result in parsed.get("results") or []:
        if isinstance(result, dict):
            index = cue_index(result.get("i"))
            if index is not None:
                by_index[index] = result

    trim_offset = float((job.trim_offset_seconds if job else 0) or 0)
    counts: dict[str, int] = {}
    for i, question in enumerate(questions):
        row = build_extraction(question, by_index.get(i), cues, job, trim_offset)
        row.model_id = settings.bedrock_haiku_model_id
        row.prompt_version = PROMPT_VERSION
        row.input_tokens = input_tokens
        row.output_tokens = output_tokens
        db.add(row)
        counts[row.status] = counts.get(row.status, 0) + 1

    db.commit()
    logger.info(
        "Q&A answers extracted — webinar=%s job=%s questions=%d %s tokens_in=%d tokens_out=%d",
        webinar_id,
        job.id if job else None,
        len(questions),
        counts,
        input_tokens,
        output_tokens,
    )
    return len(questions)
