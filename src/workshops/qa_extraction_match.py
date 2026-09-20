"""Turn one model verdict about a spoken answer into a stored row.

Split from ``qa_extraction_service`` so the matching rules stay put while the
transcript can come from anywhere — the video pipeline's own artefact, or the
auto-generated captions Vimeo holds for a replay that predates the pipeline.

Three things here were learned the expensive way and are load-bearing:

* **Cues are addressed by index, never by timestamp.** Asked for seconds, the
  model returns the digits of whatever label it was shown, concatenated. The
  matches were right and every timestamp was garbage. Indices are echoed back
  verbatim and mapped to seconds here, in code.
* **A span outside the transcript is never stored as if it were real** — a bad
  offset would point the admin's deep link at the wrong minute.
* **Time is a validator, not a search key.** Semantic matching cannot tell
  "answered because asked" from "asked because heard": an attendee who has just
  listened to a segment often asks about it. An answer landing well before its
  question is that second case, and is recorded as ``presentation_coverage``
  rather than passed off as an answer.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from decimal import Decimal

from src.video_pipeline.models import WebinarVideoJob
from src.workshops.qa_models import WebinarQaAnswerExtraction, WebinarQaQuestion
from src.workshops.qa_speaker_names import canonical_speaker

PROMPT_VERSION = "v2"

# Measured wording — 23/23 questions matched on the webinar this was built
# against. Changing a word means re-measuring, so bump PROMPT_VERSION with it or
# the stored history stops being comparable.
SYSTEM = (
    "You match webinar Q&A questions to where they were answered aloud in a transcript.\n"
    "The transcript is numbered lines '#<index> Speaker Name: text'.\n"
    "A moderator often reads a question aloud (paraphrased) before the expert answers it.\n"
    "For each question decide if it was genuinely answered aloud. Do not force a match.\n"
    "SPEAKERS lists the people on this webinar, spelled the way they spell it.\n"
    "When the speaker is one of them, copy answered_by from SPEAKERS character for\n"
    "character. Never spell a listed name from how the transcript sounds it out.\n"
    "If the speaker is plainly somebody not listed, give the name the transcript gives.\n"
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


def cue_index(raw: object) -> int | None:
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


def build_extraction(
    question: WebinarQaQuestion,
    result: dict | None,
    cues: list[dict],
    job: WebinarVideoJob | None,
    trim_offset: float,
    roster: Sequence[str] = (),
) -> WebinarQaAnswerExtraction:
    """Turn one model verdict into a row, validating the span it claims.

    ``job`` is absent when the transcript did not come from the video pipeline —
    a replay captioned on Vimeo has no job to hang the row off, and none of the
    checks below need one.
    """
    row = WebinarQaAnswerExtraction(
        question_id=question.id,
        video_job_id=job.id if job else None,
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

    start = cue_index(result.get("start_cue"))
    end = cue_index(result.get("end_cue"))
    if start is None or end is None or not 0 <= start <= end < len(cues):
        row.answer_text = (
            f"Model returned an unusable span: "
            f"{result.get('start_cue')!r}-{result.get('end_cue')!r}"
        )
        return row

    start_seconds = float(cues[start].get("start") or 0)
    end_seconds = float(cues[end].get("end") or 0)
    row.transcript_start_seconds = int(start_seconds)
    row.transcript_end_seconds = int(end_seconds)
    row.answered_by = canonical_speaker(result.get("answered_by"), roster)
    row.answer_text = str(result.get("answer") or "").strip() or None
    row.transcript_excerpt = (
        "\n".join(str(c.get("text") or "") for c in cues[start : end + 1]).strip() or None
    )

    row.status = _causality_status(question, _recording_start(job), trim_offset, start_seconds)
    return row


def _recording_start(job: WebinarVideoJob | None) -> datetime | None:
    return job.recording_start if job else None


def _causality_status(
    question: WebinarQaQuestion,
    recording_start: datetime | None,
    trim_offset: float,
    cue_start: float,
) -> str:
    """``extracted``, unless the answer was spoken before the question was asked.

    Skipped when either clock is missing. Without ``recording_start`` the
    transcript has no wall-clock origin, and a guess at one (the scheduled start,
    or Zoom's "actual start") is minutes out — which would flag real answers
    while missing the ones this check exists for. A Vimeo-captioned replay is
    always in that case: nothing records when its recording began.
    """
    if recording_start is None or question.asked_at is None:
        return "extracted"
    answered_at = recording_start + timedelta(seconds=trim_offset + cue_start)
    if answered_at < question.asked_at - timedelta(seconds=TOLERANCE_SECONDS):
        return "presentation_coverage"
    return "extracted"
