"""Filling spoken answers from a replay's Vimeo captions.

Most webinars predate the video pipeline and have no transcript artefact, so the
only record of what was said aloud is the auto-generated caption track on the
replay. These pin the two things that differ from the pipeline path: a run with
no video job must still store answers, and it must not assert a causality it has
no clock to check.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from src.content import vtt_parser
from src.integrations import vimeo
from src.workshops import qa_extraction_service, qa_vimeo_transcript
from src.workshops.qa_extraction_service import extract_answers_from_cues
from src.workshops.qa_vimeo_transcript import TranscriptUnavailable, load_cues

from tests.workshops.test_qa_extraction_service import (  # noqa: F401
    CUES,
    RECORDING_START,
    _model,
    _rows,
    make_question,
)

VTT = """WEBVTT

00:00:00.000 --> 00:00:10.000
You file by the fifteenth.

00:00:10.000 --> 00:00:20.000
After that it is a late submission.
"""


def test_an_answer_is_stored_even_though_there_is_no_video_job(
    qa_db, qa_webinar, make_question, monkeypatch  # noqa: F811
):
    # The whole point of this path: these webinars never went through the
    # pipeline, so a row that required a job would be a row that never exists.
    question = make_question()
    _model(monkeypatch, [{"i": 0, "found": True, "start_cue": 1, "end_cue": 2,
                          "answer": "By the fifteenth.", "confidence": 0.9}])

    assert extract_answers_from_cues(qa_db, qa_webinar.id, list(CUES)) == 1

    row = _rows(qa_db)[0]
    assert row.question_id == question.id
    assert row.video_job_id is None
    assert row.status == "extracted"
    assert row.answer_text == "By the fifteenth."


def test_an_answer_spoken_early_is_not_demoted_without_a_recording_clock(
    qa_db, qa_webinar, make_question, monkeypatch  # noqa: F811
):
    # With a job, an answer landing well before its question is
    # `presentation_coverage`. Vimeo captions carry no wall-clock origin, so that
    # comparison is impossible here — and guessing one would demote real answers.
    make_question(asked_at=RECORDING_START + timedelta(hours=2))
    _model(monkeypatch, [{"i": 0, "found": True, "start_cue": 0, "end_cue": 0,
                          "answer": "Early.", "confidence": 0.8}])

    extract_answers_from_cues(qa_db, qa_webinar.id, list(CUES))

    assert _rows(qa_db)[0].status == "extracted"


def test_a_webinar_with_no_live_questions_costs_no_model_call(
    qa_db, qa_webinar, make_question, monkeypatch  # noqa: F811
):
    make_question(answer_source="typed")

    def _explode(**_kwargs):
        raise AssertionError("the model must not be called with nothing to match")

    monkeypatch.setattr(qa_extraction_service, "call_json", _explode)
    assert extract_answers_from_cues(qa_db, qa_webinar.id, list(CUES)) == 0


def test_captions_become_cues_with_their_own_timings(qa_webinar, monkeypatch):
    qa_webinar.video_embed_code = '<iframe src="https://player.vimeo.com/video/123"></iframe>'
    monkeypatch.setattr(vimeo, "download_source_track", lambda ref, lang="en": (VTT, "English"))

    cues = load_cues(qa_webinar)

    assert [c["text"] for c in cues] == [
        "You file by the fifteenth.",
        "After that it is a late submission.",
    ]
    assert cues[1]["start"] == 10.0


def test_a_webinar_with_no_replay_is_reported_rather_than_guessed_at(qa_webinar):
    qa_webinar.video_embed_code = None
    with pytest.raises(TranscriptUnavailable, match="no replay"):
        load_cues(qa_webinar)


def test_a_replay_without_english_captions_is_reported_as_unavailable(
    qa_webinar, monkeypatch
):
    # Roughly one replay in ten has no auto-generated track. That is a webinar
    # this path cannot fill, not a crash that should end a backfill run.
    qa_webinar.video_embed_code = "https://vimeo.com/123"

    def _none(ref, lang="en"):
        raise vimeo.VimeoError("This video has no en caption track to translate from.")

    monkeypatch.setattr(vimeo, "download_source_track", _none)
    with pytest.raises(TranscriptUnavailable, match="no en caption"):
        load_cues(qa_webinar)


def test_an_unparseable_caption_track_is_reported_rather_than_raised_raw(
    qa_webinar, monkeypatch
):
    qa_webinar.video_embed_code = "https://vimeo.com/123"
    monkeypatch.setattr(vimeo, "download_source_track", lambda ref, lang="en": ("", "English"))

    with pytest.raises(TranscriptUnavailable):
        load_cues(qa_webinar)


def test_vtt_parse_failure_is_wrapped(qa_webinar, monkeypatch):
    qa_webinar.video_embed_code = "https://vimeo.com/123"
    monkeypatch.setattr(vimeo, "download_source_track", lambda ref, lang="en": ("x", "English"))

    def _boom(_raw):
        raise vtt_parser.VttError("no timed cues")

    monkeypatch.setattr(qa_vimeo_transcript.vtt_parser, "parse", _boom)
    with pytest.raises(TranscriptUnavailable, match="not usable WebVTT"):
        load_cues(qa_webinar)
