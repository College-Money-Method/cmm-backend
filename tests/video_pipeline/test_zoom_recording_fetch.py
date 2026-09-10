"""File selection inside a Zoom recording payload.

A recording is a set of files and picking the wrong one is silent: the job
succeeds and publishes a speaker-only rendition, or a 200 KB audio-only stub,
with nothing in the logs to say so. These tests pin the two selection rules.
"""

from __future__ import annotations

import pytest

from src.video_pipeline.zoom_recording_fetch import (
    RecordingFetchError,
    select_transcript_file,
    select_video_file,
)


def _payload(*files: dict) -> dict:
    return {"recording_files": list(files)}


def test_largest_completed_mp4_wins():
    """Zoom returns several MP4 renditions; the composite is always the biggest."""
    speaker = {"id": "a", "file_type": "MP4", "file_size": 120_000_000, "status": "completed"}
    shared_screen = {"id": "b", "file_type": "MP4", "file_size": 980_000_000, "status": "completed"}
    gallery = {"id": "c", "file_type": "MP4", "file_size": 310_000_000, "status": "completed"}

    assert select_video_file(_payload(speaker, shared_screen, gallery))["id"] == "b"


def test_incomplete_mp4_is_ignored_even_when_larger():
    """A still-processing file has a size but no usable bytes behind it."""
    processing = {"id": "a", "file_type": "MP4", "file_size": 990_000_000, "status": "processing"}
    ready = {"id": "b", "file_type": "MP4", "file_size": 400_000_000, "status": "completed"}

    assert select_video_file(_payload(processing, ready))["id"] == "b"


def test_file_type_matching_is_case_insensitive():
    """Zoom has shipped both `MP4` and `mp4` in this field."""
    assert select_video_file(_payload({"id": "a", "file_type": "mp4", "file_size": 10}))["id"] == "a"


def test_missing_status_is_treated_as_completed():
    """Older payloads omit `status` entirely; refusing them would fail real jobs."""
    assert select_video_file(_payload({"id": "a", "file_type": "MP4", "file_size": 10}))["id"] == "a"


def test_no_mp4_raises():
    with pytest.raises(RecordingFetchError, match="no completed MP4"):
        select_video_file(_payload({"id": "a", "file_type": "M4A", "file_size": 10}))


def test_non_dict_entries_are_skipped():
    """Defensive: the payload is remote JSON, not something we shaped."""
    with pytest.raises(RecordingFetchError):
        select_video_file({"recording_files": ["nonsense", None]})


def test_transcript_selected_by_type():
    chat = {"id": "a", "file_type": "CHAT"}
    vtt = {"id": "b", "file_type": "TRANSCRIPT"}

    assert select_transcript_file(_payload(chat, vtt))["id"] == "b"


def test_missing_transcript_returns_none_rather_than_raising():
    """Audio transcript is an account setting — absent is a state, not a failure."""
    assert select_transcript_file(_payload({"id": "a", "file_type": "MP4"})) is None
