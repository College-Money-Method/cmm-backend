"""File selection inside a Zoom recording payload.

A recording is a set of files and picking the wrong one is silent: the job
succeeds and publishes a speaker-only rendition, or a 200 KB audio-only stub,
with nothing in the logs to say so. These tests pin the two selection rules.
"""

from __future__ import annotations

import pytest

from src.integrations import zoom
from src.video_pipeline import zoom_recording_fetch
from src.video_pipeline.zoom_recording_fetch import (
    RecordingFetchError,
    RecordingNotReadyError,
    fetch_recording,
    select_transcript_file,
    select_video_file,
)


def _payload(*files: dict) -> dict:
    return {"recording_files": list(files)}


def test_shared_screen_with_speaker_wins_over_a_larger_speaker_only_file():
    """The rule is the view, not the size.

    A webinar of static slides composites smaller than the speaker camera beside
    it, so picking by size published the speaker-only rendition — slides gone.
    """
    speaker = {
        "id": "a",
        "file_type": "MP4",
        "file_size": 980_000_000,
        "status": "completed",
        "recording_type": "speaker_view",
    }
    shared_screen = {
        "id": "b",
        "file_type": "MP4",
        "file_size": 120_000_000,
        "status": "completed",
        "recording_type": "shared_screen_with_speaker_view",
    }
    gallery = {
        "id": "c",
        "file_type": "MP4",
        "file_size": 310_000_000,
        "status": "completed",
        "recording_type": "gallery_view",
    }

    assert select_video_file(_payload(speaker, shared_screen, gallery))["id"] == "b"


def test_shared_screen_with_gallery_beats_camera_only_views():
    """Any rendition carrying the screen outranks any that does not."""
    gallery = {
        "id": "a",
        "file_type": "MP4",
        "file_size": 900_000_000,
        "status": "completed",
        "recording_type": "gallery_view",
    }
    screen_gallery = {
        "id": "b",
        "file_type": "MP4",
        "file_size": 100_000_000,
        "status": "completed",
        "recording_type": "shared_screen_with_gallery_view",
    }

    assert select_video_file(_payload(gallery, screen_gallery))["id"] == "b"


def test_recording_type_matching_ignores_case_and_padding():
    """The field is remote JSON — it has arrived title-cased and space-padded."""
    speaker = {
        "id": "a",
        "file_type": "MP4",
        "file_size": 900_000_000,
        "status": "completed",
        "recording_type": "speaker_view",
    }
    shared_screen = {
        "id": "b",
        "file_type": "MP4",
        "file_size": 10,
        "status": "completed",
        "recording_type": " Shared_Screen_With_Speaker_View ",
    }

    assert select_video_file(_payload(speaker, shared_screen))["id"] == "b"


def test_unlabelled_renditions_fall_back_to_the_largest():
    """No `recording_type` at all: size is the only signal left, so use it."""
    small = {"id": "a", "file_type": "MP4", "file_size": 120_000_000, "status": "completed"}
    large = {"id": "b", "file_type": "MP4", "file_size": 980_000_000, "status": "completed"}

    assert select_video_file(_payload(small, large))["id"] == "b"


def test_largest_wins_within_one_view():
    """Size still breaks a tie between two entries of the same type."""
    small = {
        "id": "a",
        "file_type": "MP4",
        "file_size": 120_000_000,
        "status": "completed",
        "recording_type": "shared_screen_with_speaker_view",
    }
    large = {
        "id": "b",
        "file_type": "MP4",
        "file_size": 980_000_000,
        "status": "completed",
        "recording_type": "shared_screen_with_speaker_view",
    }

    assert select_video_file(_payload(small, large))["id"] == "b"


def test_incomplete_mp4_is_ignored_even_when_better_ranked():
    """A still-processing file has a size but no usable bytes behind it."""
    processing = {
        "id": "a",
        "file_type": "MP4",
        "file_size": 990_000_000,
        "status": "processing",
        "recording_type": "shared_screen_with_speaker_view",
    }
    ready = {
        "id": "b",
        "file_type": "MP4",
        "file_size": 400_000_000,
        "status": "completed",
        "recording_type": "speaker_view",
    }

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


def _refusing(exc: zoom.ZoomApiError, monkeypatch):
    monkeypatch.setattr(
        zoom_recording_fetch.zoom, "get_recording", lambda uuid_: (_ for _ in ()).throw(exc)
    )


def test_a_recording_zoom_is_still_processing_is_not_a_failure(monkeypatch, tmp_path):
    """Zoom answers 404 both for "still transcoding" and for "gone", so only its
    own error code can tell the caller to come back later rather than give up."""
    _refusing(
        zoom.ZoomApiError("HTTP 404 (code 3301): This recording is still being processed", 404, 3301),
        monkeypatch,
    )

    with pytest.raises(RecordingNotReadyError):
        fetch_recording("rec-1", tmp_path)


def test_any_other_refusal_stays_a_plain_failure(monkeypatch, tmp_path):
    """A recording that was deleted, or a scope that was never granted, arrives
    as the same 404 and must still end the run instead of looping on it."""
    _refusing(zoom.ZoomApiError("HTTP 404 (code 3001): meeting not found", 404, 3001), monkeypatch)

    with pytest.raises(RecordingFetchError) as caught:
        fetch_recording("rec-1", tmp_path)
    assert not isinstance(caught.value, RecordingNotReadyError)
