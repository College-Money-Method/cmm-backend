"""Translated captions, made after the replay is already live.

The shape of this follow-up is forced by Vimeo: it writes the English transcript
itself, minutes after the transcode, and announces it to nobody. So the pipeline
publishes first and keeps looking afterwards — which means the two behaviours
worth pinning are that looking and finding nothing is not a failure, and that
nothing here can disturb the `published` state a school's page depends on.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from src.config import settings
from src.video_pipeline import caption_task, job_service
from src.video_pipeline.states import JobState


@pytest.fixture
def locales(monkeypatch):
    monkeypatch.setattr(settings, "video_caption_locales", "es,zh,zh-Hant")


def _published(db, webinar, **fields):
    job, _ = job_service.create_from_recording(
        db, webinar_id=webinar.id, zoom_recording_uuid=f"rec-{uuid.uuid4()}"
    )
    job.state = JobState.PUBLISHED.value
    job.vimeo_video_id = "1225339276"
    for name, value in fields.items():
        setattr(job, name, value)
    db.commit()
    return job


# ── which languages ──────────────────────────────────────────────────────────


def test_the_configured_locales_are_what_gets_translated(locales):
    assert caption_task.locales() == ["es", "zh", "zh-Hant"]


def test_an_unsupported_locale_is_dropped_rather_than_failing_the_run(monkeypatch):
    """Two languages out of three is worth publishing; nothing is not."""
    monkeypatch.setattr(settings, "video_caption_locales", "es, klingon ,zh")

    assert caption_task.locales() == ["es", "zh"]


# ── which jobs ───────────────────────────────────────────────────────────────


def test_only_published_jobs_are_due(db, webinar):
    job, _ = job_service.create_from_recording(
        db, webinar_id=webinar.id, zoom_recording_uuid=f"rec-{uuid.uuid4()}"
    )

    assert caption_task.due(db, limit=10) == []


def test_a_published_job_is_due_until_its_captions_settle(db, webinar):
    job = _published(db, webinar)

    assert [j.id for j in caption_task.due(db, limit=10)] == [job.id]

    job.captions_state = caption_task.COMPLETED
    db.commit()

    assert caption_task.due(db, limit=10) == []


def test_a_run_abandoned_mid_translation_is_picked_up_again(db, webinar):
    """A process that dies leaves `running` behind. Re-running is safe — each
    language's track is replaced wholesale."""
    job = _published(db, webinar, captions_state=caption_task.RUNNING)
    job.updated_at = datetime.now(timezone.utc) - timedelta(hours=3)
    db.commit()

    assert [j.id for j in caption_task.due(db, limit=10)] == [job.id]


def test_a_run_that_started_moments_ago_is_left_alone(db, webinar):
    _published(db, webinar, captions_state=caption_task.RUNNING)

    assert caption_task.due(db, limit=10) == []


# ── waiting for Vimeo ────────────────────────────────────────────────────────


def test_no_english_track_yet_costs_an_attempt_and_nothing_else(db, webinar, locales, monkeypatch):
    monkeypatch.setattr(caption_task, "has_source_track", lambda ref: False)
    job = _published(db, webinar)

    assert caption_task.run(db, job) == caption_task.PENDING
    assert job.captions_attempts == 1
    assert job.job_state is JobState.PUBLISHED


def test_a_transcript_that_never_arrives_is_skipped_not_failed(db, webinar, locales, monkeypatch):
    """Vimeo not transcribing a recording is an outcome, not a broken pipeline —
    and an ops alert for it would be noise nobody can act on."""
    monkeypatch.setattr(caption_task, "has_source_track", lambda ref: False)
    monkeypatch.setattr(settings, "video_caption_max_attempts", 2)
    job = _published(db, webinar)

    assert caption_task.run(db, job) == caption_task.PENDING
    assert caption_task.run(db, job) == caption_task.SKIPPED
    assert job.captions_completed_at is not None


def test_vimeo_being_unreachable_leaves_the_job_for_the_next_sweep(db, webinar, locales, monkeypatch):
    def boom(ref):
        raise RuntimeError("connection reset")

    monkeypatch.setattr(caption_task, "has_source_track", boom)
    job = _published(db, webinar)

    assert caption_task.run(db, job) == caption_task.PENDING
    assert job.captions_attempts == 0


def test_configuring_no_locales_at_all_skips_rather_than_polls_forever(db, webinar, monkeypatch):
    monkeypatch.setattr(settings, "video_caption_locales", "")
    job = _published(db, webinar)

    assert caption_task.run(db, job) == caption_task.SKIPPED


# ── running it ───────────────────────────────────────────────────────────────


def test_the_private_hash_travels_with_the_video_reference(db, webinar):
    job = _published(db, webinar, vimeo_hash="abc123")

    assert caption_task.video_ref(job) == "1225339276:abc123"


def test_a_finished_run_is_recorded_as_completed(db, webinar, locales, monkeypatch):
    seen: dict = {}

    async def run_job(cc_job, vtt, targets):
        seen["ref"], seen["vtt"], seen["locales"] = cc_job.video_ref, vtt, targets
        cc_job.finish("completed")

    monkeypatch.setattr(caption_task, "has_source_track", lambda ref: True)
    monkeypatch.setattr(caption_task.video_cc_service, "run_job", run_job)
    job = _published(db, webinar)

    assert caption_task.run(db, job) == caption_task.COMPLETED
    # None means "translate the track already on the video" — Vimeo's own
    # English transcript is the source, and there is nothing to upload.
    assert seen == {"ref": "1225339276", "vtt": None, "locales": ["es", "zh", "zh-Hant"]}


def test_a_failed_run_keeps_the_reason_and_leaves_the_replay_published(db, webinar, locales, monkeypatch):
    async def run_job(cc_job, vtt, targets):
        cc_job.emit("error", error="Bedrock throttled")
        cc_job.finish("failed")

    monkeypatch.setattr(caption_task, "has_source_track", lambda ref: True)
    monkeypatch.setattr(caption_task.video_cc_service, "run_job", run_job)
    job = _published(db, webinar)

    assert caption_task.run(db, job) == caption_task.FAILED
    assert job.captions_error == "Bedrock throttled"
    assert job.job_state is JobState.PUBLISHED
