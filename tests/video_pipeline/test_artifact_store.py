"""Archive keys, expiry, and the frame manifest handed to the chaptering stage.

Nothing here touches S3 — the boto3 calls are covered by their own client. What
matters is the shape of what gets written, because the chaptering stage reads it
in a different process and a wrong offset there moves every chapter.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.config import settings
from src.video_pipeline.archive_original import archive_prefix, expires_at
from src.video_pipeline.artifact_store import build_candidate_manifest, frames_prefix
from src.video_pipeline.ffmpeg_ops import Candidate

JOB_ID = "0f2c9a3e-1111-4222-8333-444455556666"


def test_archive_prefix_is_scoped_to_one_job():
    prefix = archive_prefix(JOB_ID)

    assert prefix == f"{settings.video_archive_prefix}/{JOB_ID}/"
    assert prefix.endswith("/"), "callers concatenate filenames straight onto this"


def test_frames_prefix_is_scoped_to_one_job_and_distinct_from_the_archive():
    """The lifecycle rule expires originals only — frames must not share that prefix."""
    assert frames_prefix(JOB_ID) == f"{settings.video_frames_prefix}/{JOB_ID}/"
    assert not frames_prefix(JOB_ID).startswith(settings.video_archive_prefix)


def test_prefix_is_stable_against_a_configured_trailing_slash(monkeypatch):
    monkeypatch.setattr(settings, "video_archive_prefix", "/video-pipeline/originals/")

    assert archive_prefix(JOB_ID) == f"video-pipeline/originals/{JOB_ID}/"


def test_expiry_matches_the_bucket_lifecycle_window():
    """Stored on the job so the admin screen can grey out a retry that cannot work."""
    archived = datetime(2026, 1, 1, tzinfo=timezone.utc)

    assert expires_at(archived) == archived + timedelta(days=settings.video_archive_retention_days)


def test_expiry_defaults_to_now_and_is_timezone_aware():
    computed = expires_at()

    assert computed.tzinfo is not None
    assert computed > datetime.now(timezone.utc)


def test_manifest_opens_with_a_synthetic_zero_entry():
    """Vimeo requires a chapter at 0:00 and the first distinct state is never there."""
    candidates = [
        Candidate(index=1, timestamp=12.5, path=Path("/tmp/frame_0001.jpg")),
        Candidate(index=2, timestamp=48.0, path=Path("/tmp/frame_0002.jpg")),
    ]

    manifest = build_candidate_manifest(candidates)

    assert manifest[0] == {"index": 0, "timestamp": 0.0, "file": None}
    assert [entry["timestamp"] for entry in manifest] == [0.0, 12.5, 48.0]
    assert [entry["file"] for entry in manifest[1:]] == ["frame_0001.jpg", "frame_0002.jpg"]


def test_manifest_of_no_candidates_still_has_the_opening_anchor():
    assert build_candidate_manifest([]) == [{"index": 0, "timestamp": 0.0, "file": None}]
