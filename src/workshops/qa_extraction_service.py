"""Recover the answers that were only ever spoken out loud.

Zoom's Q&A report marks a question "live answered" and stores no answer text for
it — the answer exists only in the recording. This module reads the transcript
the video pipeline already left in S3 and asks the model where each of those
questions was answered.

Three things about this were learned the expensive way and are load-bearing:

* **The whole transcript goes in, unwindowed.** Panelists answer questions as
  they watch them arrive and bulk-mark them resolved much later, so more than
  half the answers precede their question's Zoom timestamp. Any forward-looking
  window drops them.
* **Cues are addressed by index, never by timestamp.** Asked for seconds, the
  model returns the digits of whatever label it was shown, concatenated. The
  matches were right and every timestamp was garbage. Indices are echoed back
  verbatim and mapped to seconds here, in code.
* **Time is a validator, not a search key.** Semantic matching cannot tell "answered
  because asked" from "asked because heard" — an attendee who has just listened
  to a segment often asks about it. An answer landing well before its question
  is that second case, and is recorded as ``presentation_coverage`` rather than
  passed off as an answer.

Every run appends. Nothing here updates a previous verdict or touches an admin's
override.
"""

from __future__ import annotations

import logging
import uuid
from datetime import timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.config import settings
from src.video_pipeline import artifact_store
from src.video_pipeline.bedrock_client import BedrockCallError, call_json
from src.video_pipeline.models import WebinarVideoJob
from src.video_pipeline.states import JobState
from src.workshops.qa_models import WebinarQaAnswerExtraction, WebinarQaQuestion

logger = logging.getLogger(__name__)

PROMPT_VERSION = "v1"

# Measured wording — 23/23 questions matched on the webinar this was built
# against. Changing a word means re-measuring, so bump PROMPT_VERSION with it or
# the stored history stops being comparable.
SYSTEM = (
    "You match webinar Q&A questions to where they were answered aloud in a transcript.\n"
    "The transcript is numbered lines '#<index> Speaker Name: text'.\n"
    "A moderator often reads a question aloud (paraphrased) before the expert answers it.\n"
    "For each question decide if it was genuinely answered aloud. Do not force a match.\n"
    'Reply ONLY with JSON: {"results":[{"i":<index>,"found":true|false,'
    '"start_cue":<index of the first line of the answer>,'
    '"end_cue":<index of the last line of the answer>,"answered_by":"<speaker name>",'
    '"answer":"<faithful 1-3 sentence summary of what was actually said>",'
    '"confidence":<0.0-1.0>}]}\n'
    "start_cue and end_cue must be line numbers copied exactly from the '#' markers.\n"
    "If found is false omit the other fields except i and confidence."
)

# How far before its question an answer may land and still count as an answer.
# The recording start Zoom reports and the clock Zoom stamps questions with do
# not agree to the second — roughly two minutes of drift showed up in testing,
# and a genuine match sat 14 s the wrong side of its question. Below this band
# the check produces false alarms; far above it, it stops catching the
# presentation-coverage case it exists for.
TOLERANCE_SECONDS = 120


def _cue_index(raw: object) -> int | None:
    """The model echoes the marker as written (``"#50"``). Take the number out.

    A strict int check here rejected every valid span in testing, which is why
    this is a coercion and not a validation.
    """
    try:
        return int(str(raw).strip().lstrip("#"))
    except (TypeError, ValueError):
        return None


def _confidence(raw: object) -> Decimal | None:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return Decimal(f"{min(max(value, 0.0), 1.0):.2f}")


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
    """Find spoken answers for one webinar's live-answered questions.

    Returns the number of extraction rows written. Commits. Never raises: this
    runs off the back of a publish, and a replay that is live must not be held
    back by a model call.
    """
    questions = list(
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

    transcript = "\n".join(f"#{i} {c.get('text') or ''}" for i, c in enumerate(cues))
    qlist = "\n".join(f"{i}. {q.question_text}" for i, q in enumerate(questions))
    try:
        parsed, input_tokens, output_tokens = call_json(
            system=SYSTEM,
            content=f"TRANSCRIPT:\n{transcript}\n\nQUESTIONS:\n{qlist}",
            max_tokens=8192,
        )
    except BedrockCallError as exc:
        return _record_failure(db, questions, f"Model call failed: {exc}", job.id)

    by_index: dict[int, dict] = {}
    for result in parsed.get("results") or []:
        if isinstance(result, dict):
            index = _cue_index(result.get("i"))
            if index is not None:
                by_index[index] = result

    trim_offset = float(job.trim_offset_seconds or 0)
    counts: dict[str, int] = {}
    for i, question in enumerate(questions):
        row = _build_extraction(question, by_index.get(i), cues, job, trim_offset)
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
        job.id,
        len(questions),
        counts,
        input_tokens,
        output_tokens,
    )
    return len(questions)


def _build_extraction(
    question: WebinarQaQuestion,
    result: dict | None,
    cues: list[dict],
    job: WebinarVideoJob,
    trim_offset: float,
) -> WebinarQaAnswerExtraction:
    """Turn one model verdict into a row, validating the span it claims."""
    row = WebinarQaAnswerExtraction(
        question_id=question.id,
        video_job_id=job.id,
        status="failed",
    )
    if result is None:
        # The model skipped this question entirely. Recorded as a broken run
        # rather than as `not_found`, which would assert a verdict nobody gave.
        row.answer_text = "Model returned no verdict for this question"
        return row

    row.confidence = _confidence(result.get("confidence"))
    if not result.get("found"):
        row.status = "not_found"
        return row

    start = _cue_index(result.get("start_cue"))
    end = _cue_index(result.get("end_cue"))
    if start is None or end is None or not 0 <= start <= end < len(cues):
        # A span outside the transcript is never stored as if it were real: a
        # bad offset would point the admin's deep link at the wrong minute.
        row.answer_text = f"Model returned an unusable span: {result.get('start_cue')!r}-{result.get('end_cue')!r}"
        return row

    start_seconds = float(cues[start].get("start") or 0)
    end_seconds = float(cues[end].get("end") or 0)
    row.transcript_start_seconds = int(start_seconds)
    row.transcript_end_seconds = int(end_seconds)
    row.answered_by = (str(result.get("answered_by") or "").strip() or None)
    row.answer_text = (str(result.get("answer") or "").strip() or None)
    row.transcript_excerpt = "\n".join(
        str(c.get("text") or "") for c in cues[start : end + 1]
    ).strip() or None

    row.status = _causality_status(question, job, trim_offset, start_seconds)
    return row


def _causality_status(
    question: WebinarQaQuestion,
    job: WebinarVideoJob,
    trim_offset: float,
    cue_start: float,
) -> str:
    """``extracted``, unless the answer was spoken before the question was asked.

    Skipped when either clock is missing. Without ``recording_start`` the
    transcript has no wall-clock origin, and a guess at one (the scheduled start,
    or Zoom's "actual start") is minutes out — which would flag real answers
    while missing the ones this check exists to catch.
    """
    if job.recording_start is None or question.asked_at is None:
        return "extracted"
    answered_at = job.recording_start + timedelta(seconds=trim_offset + cue_start)
    if answered_at < question.asked_at - timedelta(seconds=TOLERANCE_SECONDS):
        return "presentation_coverage"
    return "extracted"
