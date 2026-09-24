"""Borrowing Vimeo's transcript for a recording Zoom delivered without one.

The borrow has three outcomes and the sweep depends on telling them apart: cues
to chapter from, a reason to look again next sweep, or an answer of "none" once
the wait is over so the replay still goes live on its frames.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from src.config import settings
from src.integrations.vimeo import VimeoError
from src.video_pipeline import artifact_store, vimeo_transcript
from src.video_pipeline.vimeo_transcript import TranscriptPending, cues_for

PREFIX = "video-pipeline/frames/job/"
VTT = """WEBVTT

00:00:05.000 --> 00:00:09.000
Welcome everyone.
"""


def _job(waited_minutes: float = 0.0):
    return SimpleNamespace(
        id=uuid.uuid4(),
        frames_prefix=PREFIX,
        updated_at=datetime.now(timezone.utc) - timedelta(minutes=waited_minutes),
    )


@pytest.fixture
def track(monkeypatch):
    served = {"vtt": None}

    def download(ref, language="en"):
        if served["vtt"] is None:
            raise VimeoError("no source track", status=404)
        return served["vtt"], "Auto-generated English"

    monkeypatch.setattr(vimeo_transcript.vimeo, "download_source_track", download)
    monkeypatch.setattr(settings, "video_transcript_wait_minutes", 60)
    return served


@pytest.fixture
def saved(monkeypatch):
    kept: list[tuple[str, list]] = []
    monkeypatch.setattr(
        vimeo_transcript.artifact_store,
        "save_transcript",
        lambda prefix, cues: kept.append((prefix, list(cues))),
    )
    return kept


def test_vimeos_track_is_returned_and_kept_for_the_qna_extraction(track, saved):
    track["vtt"] = VTT

    cues = cues_for(_job(), "987654321")

    assert [(c.start, c.text) for c in cues] == [(5.0, "Welcome everyone.")]
    assert saved == [(PREFIX, cues)]


def test_no_track_yet_defers_the_job_while_the_wait_lasts(track, saved):
    with pytest.raises(TranscriptPending):
        cues_for(_job(waited_minutes=5), "987654321")
    assert saved == []


def test_no_track_after_the_wait_chapters_from_frames_alone(track, saved):
    assert cues_for(_job(waited_minutes=61), "987654321") == []


def test_a_track_that_will_not_parse_counts_as_no_track(track, saved):
    track["vtt"] = "not a caption file"

    with pytest.raises(TranscriptPending):
        cues_for(_job(), "987654321")


def test_failing_to_keep_the_track_does_not_cost_the_chapters(track, monkeypatch):
    track["vtt"] = VTT

    def refuse(prefix, cues):
        raise artifact_store.ArtifactError("access denied")

    monkeypatch.setattr(vimeo_transcript.artifact_store, "save_transcript", refuse)

    assert len(cues_for(_job(), "987654321")) == 1
