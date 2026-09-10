"""The ceiling on the candidate set, and that it stops the job before spending.

Sampling is the only step whose output size is unbounded. Everything after it
costs per frame — an S3 PUT, an S3 GET and a Bedrock vision call each — so a
filter that stops discriminating on an unfamiliar layout does not produce worse
chapters, it produces a bill. A real 82-minute recording sampled 2,655 states
and spent 53 minutes moving them through S3 before anything looked at the
number.

The order matters as much as the limit: the check has to sit between sampling
and the first upload, or the money is already gone by the time it fires.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from src.config import settings
from src.video_pipeline import ffmpeg_ops, job_service, process_recording
from src.video_pipeline.process_recording import SamplingError
from src.video_pipeline.states import JobState


def _job(db, *, claimed=False):
    """A job as the ECS task sees it — `processing`, claimed by the dispatcher."""
    job, _ = job_service.create_from_recording(
        db,
        zoom_recording_uuid=f"rec-{uuid.uuid4()}",
        source_url="https://example.com/a.mp4",
        audit_only=True,
    )
    if claimed:
        job_service.advance(db, job, JobState.PROCESSING)
    return job


class TestTheCeiling:
    def test_a_count_at_the_ceiling_is_allowed_through(self, db, monkeypatch):
        """The limit is a ceiling, not a target — landing on it is not a fault."""
        monkeypatch.setattr(settings, "video_max_sample_frames", 1000)

        process_recording._guard_candidate_count(_job(db), 1000)

    def test_a_count_above_the_ceiling_stops_the_job(self, db, monkeypatch):
        monkeypatch.setattr(settings, "video_max_sample_frames", 1000)

        with pytest.raises(SamplingError) as exc:
            process_recording._guard_candidate_count(_job(db), 1001)

        # The number and the limit both belong in the message: it is the only
        # thing an admin sees on the failed row, and "too many frames" without
        # the count says nothing about how far off the filter was.
        assert "1001" in str(exc.value)
        assert "1000" in str(exc.value)


class TestNothingIsSpentPastTheCeiling:
    """The point of the gate is what does *not* happen after it."""

    def _stub_up_to_sampling(self, monkeypatch, tmp_path: Path, count: int):
        source = tmp_path / "source.mp4"
        source.write_bytes(b"mp4")
        monkeypatch.setattr(ffmpeg_ops, "require_ffmpeg", lambda: None)
        monkeypatch.setattr(
            process_recording, "_acquire_source", lambda db, job, work_dir: (source, None)
        )
        monkeypatch.setattr(process_recording, "_resolve_trim", lambda video, vtt: (0.0, [], True))
        monkeypatch.setattr(
            process_recording.ffmpeg_ops, "trim_stream_copy", lambda src, dest, offset: source
        )
        monkeypatch.setattr(
            process_recording.ffmpeg_ops,
            "sample_distinct_frames",
            lambda *a, **k: [
                ffmpeg_ops.Candidate(index=i, timestamp=float(i), path=source)
                for i in range(count)
            ],
        )

    def test_the_frames_are_never_uploaded(self, db, monkeypatch, tmp_path):
        monkeypatch.setattr(settings, "video_max_sample_frames", 10)
        self._stub_up_to_sampling(monkeypatch, tmp_path, count=11)
        monkeypatch.setattr(
            process_recording.artifact_store,
            "upload_artifacts",
            lambda *a, **k: pytest.fail("uploaded a candidate set the gate rejected"),
        )

        with pytest.raises(SamplingError):
            process_recording.process(db, _job(db), tmp_path / "work")

    def test_the_video_is_never_sent_to_vimeo(self, db, monkeypatch, tmp_path):
        """Vimeo quota is the one cost of a rejected run that cannot be undone."""
        monkeypatch.setattr(settings, "video_max_sample_frames", 10)
        self._stub_up_to_sampling(monkeypatch, tmp_path, count=11)
        monkeypatch.setattr(
            process_recording.artifact_store, "upload_artifacts", lambda *a, **k: "frames/x"
        )
        monkeypatch.setattr(
            process_recording,
            "_publish_to_vimeo",
            lambda *a, **k: pytest.fail("uploaded to Vimeo past the gate"),
        )

        with pytest.raises(SamplingError):
            process_recording.process(db, _job(db), tmp_path / "work")

    def test_a_set_under_the_ceiling_still_goes_on_to_upload(self, db, monkeypatch, tmp_path):
        """The gate must not be the reason a normal run stops."""
        monkeypatch.setattr(settings, "video_max_sample_frames", 10)
        self._stub_up_to_sampling(monkeypatch, tmp_path, count=9)
        uploaded: list[int] = []
        monkeypatch.setattr(
            process_recording.artifact_store,
            "upload_artifacts",
            lambda job_id, candidates, cues: uploaded.append(len(candidates)) or "frames/x",
        )
        monkeypatch.setattr(
            process_recording, "_publish_to_vimeo", lambda *a, **k: "123:abc"
        )
        monkeypatch.setattr(process_recording, "wait_for_transcode", lambda ref: None)
        monkeypatch.setattr(process_recording, "_delete_zoom_copy", lambda db, job: None)
        monkeypatch.setattr(
            process_recording.ffmpeg_ops, "probe_duration", lambda path: 1200.0
        )

        process_recording.process(db, _job(db, claimed=True), tmp_path / "work")

        assert uploaded == [9]
