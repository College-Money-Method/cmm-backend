"""Recovering spoken answers from the transcript.

Two failures in this module are expensive rather than merely wrong, and both are
pinned here. A cue index read as a timestamp points an admin's deep link at a
minute that has nothing to do with the answer; and an answer matched to a
question that was asked *later* asserts a causality that never happened —
someone asked precisely because they had just heard the segment.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select

from src.video_pipeline import artifact_store
from src.video_pipeline.bedrock_client import BedrockCallError
from src.video_pipeline.models import WebinarVideoJob
from src.video_pipeline.states import JobState
from src.workshops import qa_extraction_service
from src.workshops.qa_extraction_service import _cue_index, extract_answers
from src.workshops.qa_models import WebinarQaAnswerExtraction, WebinarQaQuestion

RECORDING_START = datetime(2026, 9, 15, 23, 22, tzinfo=timezone.utc)

# Five cues, ten seconds each. Index 3 starts at 30 s — far enough from its own
# index that a service confusing the two would be caught.
CUES = [
    {"start": i * 10.0, "end": i * 10.0 + 10.0, "text": f"Speaker {i % 2}: line {i}"}
    for i in range(5)
]


@pytest.fixture
def published_job(qa_db, qa_webinar):
    job = WebinarVideoJob(
        id=uuid.uuid4(),
        webinar_id=qa_webinar.id,
        zoom_recording_uuid="rec-uuid-1",
        state=JobState.PUBLISHED.value,
        frames_prefix="webinars/abc/frames/",
        trim_offset_seconds=Decimal("0"),
        recording_start=RECORDING_START,
    )
    qa_db.add(job)
    qa_db.commit()
    return job


@pytest.fixture
def make_question(qa_db, qa_webinar):
    counter = iter(range(1000))

    def _make(text: str = "How late can I file?", **kwargs) -> WebinarQaQuestion:
        fields = {
            "answer_source": "live",
            "classification": "question",
            "asked_at": RECORDING_START + timedelta(seconds=60),
            **kwargs,
        }
        question = WebinarQaQuestion(
            id=uuid.uuid4(),
            webinar_id=qa_webinar.id,
            zoom_question_id=f"q-{next(counter)}",
            question_text=text,
            **fields,
        )
        qa_db.add(question)
        qa_db.commit()
        return question

    return _make


@pytest.fixture
def transcript(monkeypatch):
    """Serve CUES as the job's transcript artefact. Nothing reaches S3."""
    monkeypatch.setattr(
        artifact_store, "load_json_artifact", lambda prefix, filename: list(CUES)
    )


def _model(monkeypatch, results):
    monkeypatch.setattr(
        qa_extraction_service,
        "call_json",
        lambda **_kwargs: ({"results": results}, 100, 50),
    )


def _rows(db) -> list[WebinarQaAnswerExtraction]:
    return list(
        db.scalars(
            select(WebinarQaAnswerExtraction).order_by(WebinarQaAnswerExtraction.created_at)
        ).all()
    )


@pytest.mark.parametrize("raw", ["#50", 50, "50", " 50 ", "#50 "])
def test_a_cue_marker_is_read_as_its_index_however_the_model_writes_it(raw):
    # Asked for seconds instead, the model returned the digits of whatever label
    # it was shown, concatenated — a cue labelled [78:00|4681] came back as
    # 78001. Indices are echoed verbatim and mapped to seconds in code, and this
    # is the coercion that makes that safe.
    assert _cue_index(raw) == 50


@pytest.mark.parametrize("raw", ["", None, "cue fifty", "#"])
def test_an_unreadable_cue_marker_is_none_rather_than_a_guess(raw):
    assert _cue_index(raw) is None


def test_a_matched_answer_is_stored_with_seconds_taken_from_the_cues(
    qa_db, published_job, make_question, transcript, monkeypatch
):
    question = make_question()
    _model(
        monkeypatch,
        [
            {
                "i": 0,
                "found": True,
                # Written the way the model actually writes it.
                "start_cue": "#1",
                "end_cue": "#3",
                "answered_by": "Paul Martin",
                "answer": "File as soon as the form opens.",
                "confidence": 0.93,
            }
        ],
    )

    assert extract_answers(qa_db, question.webinar_id) == 1

    row = _rows(qa_db)[0]
    assert row.status == "extracted"
    # Cue 1 starts at 10 s and cue 3 ends at 40 s — never 1 and 3.
    assert (row.transcript_start_seconds, row.transcript_end_seconds) == (10, 40)
    assert row.answered_by == "Paul Martin"
    assert row.answer_text == "File as soon as the form opens."
    assert row.confidence == Decimal("0.93")
    assert row.transcript_excerpt == "\n".join(c["text"] for c in CUES[1:4])
    assert row.prompt_version == qa_extraction_service.PROMPT_VERSION
    assert (row.input_tokens, row.output_tokens) == (100, 50)


def test_a_span_outside_the_transcript_is_refused(
    qa_db, published_job, make_question, transcript, monkeypatch
):
    question = make_question()
    _model(
        monkeypatch,
        [{"i": 0, "found": True, "start_cue": 9999, "end_cue": 9999, "confidence": 0.9}],
    )

    extract_answers(qa_db, question.webinar_id)

    row = _rows(qa_db)[0]
    assert row.status == "failed"
    assert "unusable span" in row.answer_text
    # A bad offset stored as if it were real would deep-link the admin into the
    # wrong minute of the replay.
    assert row.transcript_start_seconds is None


def test_a_reversed_span_is_refused(
    qa_db, published_job, make_question, transcript, monkeypatch
):
    question = make_question()
    _model(
        monkeypatch, [{"i": 0, "found": True, "start_cue": 3, "end_cue": 1, "confidence": 0.9}]
    )

    extract_answers(qa_db, question.webinar_id)
    assert _rows(qa_db)[0].status == "failed"


def test_a_question_the_model_skipped_is_not_recorded_as_a_verdict(
    qa_db, published_job, make_question, transcript, monkeypatch
):
    question = make_question()
    _model(monkeypatch, [])

    extract_answers(qa_db, question.webinar_id)

    row = _rows(qa_db)[0]
    # `not_found` would assert a verdict nobody gave.
    assert row.status == "failed"
    assert "no verdict" in row.answer_text


def test_no_match_is_recorded_as_not_found(
    qa_db, published_job, make_question, transcript, monkeypatch
):
    question = make_question()
    _model(monkeypatch, [{"i": 0, "found": False, "confidence": 0.2}])

    extract_answers(qa_db, question.webinar_id)

    row = _rows(qa_db)[0]
    assert (row.status, row.confidence) == ("not_found", Decimal("0.20"))
    assert row.answer_text is None


def test_an_answer_spoken_long_before_the_question_is_presentation_coverage(
    qa_db, published_job, make_question, transcript, monkeypatch
):
    # Cue 0 is spoken at recording_start. The question lands 10 minutes later,
    # well past the drift tolerance — so the attendee asked because they heard
    # the segment, not the other way round.
    question = make_question(asked_at=RECORDING_START + timedelta(minutes=10))
    _model(
        monkeypatch,
        [{"i": 0, "found": True, "start_cue": 0, "end_cue": 1, "answer": "x", "confidence": 0.9}],
    )

    extract_answers(qa_db, question.webinar_id)
    assert _rows(qa_db)[0].status == "presentation_coverage"


def test_an_answer_just_inside_the_drift_tolerance_still_counts(
    qa_db, published_job, make_question, transcript, monkeypatch
):
    # Zoom's recording clock and its question clock do not agree to the second;
    # a genuine match sat 14 s the wrong side of its question during testing.
    question = make_question(
        asked_at=RECORDING_START + timedelta(seconds=qa_extraction_service.TOLERANCE_SECONDS - 30)
    )
    _model(
        monkeypatch,
        [{"i": 0, "found": True, "start_cue": 0, "end_cue": 1, "answer": "x", "confidence": 0.9}],
    )

    extract_answers(qa_db, question.webinar_id)
    assert _rows(qa_db)[0].status == "extracted"


def test_without_a_recording_start_the_causality_check_does_not_run(
    qa_db, published_job, make_question, transcript, monkeypatch
):
    # The scheduled start and Zoom's "actual start" are both minutes out, so
    # guessing an origin would flag real answers while missing the ones the
    # check exists for. Rows predating the column have no way to learn it.
    published_job.recording_start = None
    qa_db.commit()

    question = make_question(asked_at=RECORDING_START + timedelta(hours=1))
    _model(
        monkeypatch,
        [{"i": 0, "found": True, "start_cue": 0, "end_cue": 1, "answer": "x", "confidence": 0.9}],
    )

    extract_answers(qa_db, question.webinar_id)
    assert _rows(qa_db)[0].status == "extracted"


def test_the_trim_offset_moves_the_answer_onto_the_wall_clock(
    qa_db, published_job, make_question, transcript, monkeypatch
):
    # The published replay starts after the trim point, so a cue at 0 s on the
    # transcript clock was spoken `trim_offset` seconds into the recording.
    published_job.trim_offset_seconds = Decimal("600")
    qa_db.commit()

    question = make_question(asked_at=RECORDING_START + timedelta(minutes=10))
    _model(
        monkeypatch,
        [{"i": 0, "found": True, "start_cue": 0, "end_cue": 1, "answer": "x", "confidence": 0.9}],
    )

    extract_answers(qa_db, question.webinar_id)
    # Without the offset this same span would read as ten minutes early.
    assert _rows(qa_db)[0].status == "extracted"


def test_a_webinar_with_no_published_job_gets_a_reason_not_an_exception(
    qa_db, qa_webinar, make_question, transcript, monkeypatch
):
    question = make_question()
    _model(monkeypatch, [])

    assert extract_answers(qa_db, qa_webinar.id) == 1

    row = _rows(qa_db)[0]
    assert (row.status, row.video_job_id) == ("failed", None)
    assert row.answer_text == "No published video job for this webinar"


def test_an_unpublished_job_does_not_count_as_one(
    qa_db, qa_webinar, published_job, make_question, transcript, monkeypatch
):
    published_job.state = JobState.PENDING.value
    qa_db.commit()
    make_question()
    _model(monkeypatch, [])

    extract_answers(qa_db, qa_webinar.id)
    assert _rows(qa_db)[0].answer_text == "No published video job for this webinar"


def test_a_missing_transcript_gets_a_reason_not_an_exception(
    qa_db, published_job, make_question, monkeypatch
):
    question = make_question()

    def _missing(prefix, filename):
        raise artifact_store.ArtifactError("transcript.json not in S3")

    monkeypatch.setattr(artifact_store, "load_json_artifact", _missing)

    extract_answers(qa_db, question.webinar_id)

    row = _rows(qa_db)[0]
    assert row.status == "failed"
    assert "Transcript unavailable" in row.answer_text
    assert row.video_job_id == published_job.id


def test_a_bedrock_outage_gets_a_reason_not_an_exception(
    qa_db, published_job, make_question, transcript, monkeypatch
):
    question = make_question()

    def _boom(**_kwargs):
        raise BedrockCallError("bedrock unavailable")

    monkeypatch.setattr(qa_extraction_service, "call_json", _boom)

    extract_answers(qa_db, question.webinar_id)
    assert "Model call failed" in _rows(qa_db)[0].answer_text


def test_only_live_answered_questions_are_extracted_for(
    qa_db, published_job, make_question, transcript, monkeypatch
):
    make_question("Typed answer", answer_source="typed")
    make_question("Unanswered", answer_source="unanswered")
    make_question("Noise", answer_source="live", classification="thanks")
    live = make_question("The only one")
    _model(
        monkeypatch,
        [{"i": 0, "found": True, "start_cue": 1, "end_cue": 2, "answer": "x", "confidence": 0.9}],
    )

    assert extract_answers(qa_db, live.webinar_id) == 1
    assert _rows(qa_db)[0].question_id == live.id


def test_a_webinar_with_nothing_to_extract_writes_nothing(
    qa_db, qa_webinar, published_job, transcript, monkeypatch
):
    _model(monkeypatch, [])
    # Ingest and publish arrive in either order; nothing to do yet is normal.
    assert extract_answers(qa_db, qa_webinar.id) == 0
    assert _rows(qa_db) == []


def test_a_second_run_appends_and_leaves_the_first_verdict_intact(
    qa_db, published_job, make_question, transcript, monkeypatch
):
    question = make_question()
    _model(
        monkeypatch,
        [{"i": 0, "found": True, "start_cue": 1, "end_cue": 2, "answer": "first", "confidence": 0.7}],
    )
    extract_answers(qa_db, question.webinar_id)

    _model(
        monkeypatch,
        [{"i": 0, "found": True, "start_cue": 2, "end_cue": 3, "answer": "second", "confidence": 0.9}],
    )
    extract_answers(qa_db, question.webinar_id)

    rows = _rows(qa_db)
    # Extraction is non-deterministic on borderline questions; the history is
    # the only way to tell a prompt regression from ordinary model variance.
    assert len(rows) == 2
    assert {r.answer_text for r in rows} == {"first", "second"}
