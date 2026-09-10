"""The step-level timeline: what gets recorded, and what the screen may trust.

The timeline is diagnostic, so two properties matter more than the happy path:
recording a stage can never take down the run it was describing, and a row
whose JSONB has been hand-edited or written by an older version must still
render rather than break the one screen that could explain it.
"""

from __future__ import annotations

import uuid

import pytest

from src.video_pipeline import job_service, stage_progress
from src.video_pipeline.models import WebinarVideoJob
from src.video_pipeline.states import JobState


@pytest.fixture
def job(db, webinar):
    row, _ = job_service.create_from_recording(
        db, webinar_id=webinar.id, zoom_recording_uuid=f"rec-{uuid.uuid4()}"
    )
    return row


# ── the plan ─────────────────────────────────────────────────────────────────


def test_a_production_job_expects_every_stage(db, job, webinar):
    webinar.workshop.recording_thumbnail_url = "https://cdn.example.com/poster.png"
    db.commit()

    assert stage_progress.expected_stages(job) == list(stage_progress.PLAN)


def test_a_workshop_with_no_poster_image_expects_no_thumbnail_step(job):
    """Not skipped work — work that was never part of the job. A workshop with
    no image uploads none, and Vimeo keeps the frame it chose itself."""
    stages = stage_progress.expected_stages(job)

    assert stage_progress.SETTING_THUMBNAIL not in stages
    assert stage_progress.UPLOADING_TO_VIMEO in stages


def test_an_audit_run_expects_neither_the_zoom_delete_nor_the_embed_code(db):
    """Both are work an audit run never does, so showing them pending forever
    would misreport a finished run as stalled."""
    row, _ = job_service.create_from_recording(
        db, zoom_recording_uuid=f"rec-{uuid.uuid4()}", audit_only=True
    )

    stages = stage_progress.expected_stages(row)

    assert stage_progress.DELETING_ZOOM_COPY not in stages
    assert stage_progress.WRITING_EMBED_CODE not in stages
    assert stage_progress.UPLOADING_TO_VIMEO in stages


def test_a_url_source_expects_no_zoom_delete_but_still_writes_the_embed_code(db, webinar):
    row, _ = job_service.create_from_recording(
        db,
        webinar_id=webinar.id,
        zoom_recording_uuid=f"url-{uuid.uuid4()}",
        source_url="https://example.com/webinar.mp4",
    )

    stages = stage_progress.expected_stages(row)

    assert stage_progress.DELETING_ZOOM_COPY not in stages
    assert stage_progress.WRITING_EMBED_CODE in stages


def test_the_plan_keeps_the_pipeline_order(job):
    """The frontend reads step order off this list, so the order is the contract."""
    stages = stage_progress.expected_stages(job)

    assert stages.index(stage_progress.UPLOADING_TO_VIMEO) < stages.index(
        stage_progress.AWAITING_TRANSCODE
    )
    assert stages.index(stage_progress.BUILDING_CHAPTERS) < stages.index(
        stage_progress.SETTING_CHAPTERS
    )
    assert stages[0] == stage_progress.QUEUED
    assert stages[-1] == stage_progress.DONE


def test_failure_is_not_a_planned_step(job):
    """`failed` closes the timeline; it is not a step anyone is waiting for."""
    assert stage_progress.FAILED not in stage_progress.expected_stages(job)


# ── recording ────────────────────────────────────────────────────────────────


def test_a_new_job_starts_queued(job):
    assert stage_progress.current(job) == stage_progress.QUEUED


def test_recording_appends_and_moves_the_current_stage(db, job):
    stage_progress.record(db, job, stage_progress.FETCHING_SOURCE)

    assert [e["stage"] for e in stage_progress.events_of(job)] == [
        stage_progress.QUEUED,
        stage_progress.FETCHING_SOURCE,
    ]
    assert stage_progress.current(job) == stage_progress.FETCHING_SOURCE


def test_every_event_carries_a_timestamp(db, job):
    stage_progress.record(db, job, stage_progress.TRIMMING)

    assert all(e["at"] for e in stage_progress.events_of(job))


def test_recording_the_same_stage_twice_does_not_restart_it(db, job):
    """A retried step inside one stage must not reset the clock the screen is
    using to show how long that stage has been running."""
    stage_progress.record(db, job, stage_progress.AWAITING_TRANSCODE)
    first_at = stage_progress.events_of(job)[-1]["at"]

    stage_progress.record(db, job, stage_progress.AWAITING_TRANSCODE)

    assert [e["stage"] for e in stage_progress.events_of(job)].count(
        stage_progress.AWAITING_TRANSCODE
    ) == 1
    assert stage_progress.events_of(job)[-1]["at"] == first_at


def test_a_stage_may_repeat_once_something_else_happened(db, job):
    """A second pass through the pipeline is worth seeing as a second pass."""
    stage_progress.record(db, job, stage_progress.FETCHING_SOURCE)
    stage_progress.record(db, job, stage_progress.TRIMMING)
    stage_progress.record(db, job, stage_progress.FETCHING_SOURCE)

    assert [e["stage"] for e in stage_progress.events_of(job)] == [
        stage_progress.QUEUED,
        stage_progress.FETCHING_SOURCE,
        stage_progress.TRIMMING,
        stage_progress.FETCHING_SOURCE,
    ]


def test_the_event_list_is_capped_at_the_newest_entries(db, job):
    """A pathological loop must not grow the row without bound."""
    for i in range(stage_progress.MAX_EVENTS + 20):
        # Alternating keeps the dedupe from collapsing them.
        stage_progress.record(db, job, f"stage-{i}")

    events = stage_progress.events_of(job)
    assert len(events) == stage_progress.MAX_EVENTS
    assert events[-1]["stage"] == f"stage-{stage_progress.MAX_EVENTS + 19}"
    assert stage_progress.QUEUED not in {e["stage"] for e in events}


def test_the_write_survives_the_commit(db, job):
    """JSONB is compared by identity, so an in-place append would never flush."""
    stage_progress.record(db, job, stage_progress.SAMPLING_FRAMES)
    db.expire_all()

    reloaded = db.get(WebinarVideoJob, job.id)
    assert [e["stage"] for e in reloaded.stage_events] == [
        stage_progress.QUEUED,
        stage_progress.SAMPLING_FRAMES,
    ]


def test_a_broken_commit_does_not_raise(db, job, monkeypatch):
    """The timeline only describes the run. It must never be what ends it."""
    monkeypatch.setattr(
        db, "commit", lambda: (_ for _ in ()).throw(RuntimeError("connection reset"))
    )
    rolled_back: list[bool] = []
    monkeypatch.setattr(db, "rollback", lambda: rolled_back.append(True))

    stage_progress.record(db, job, stage_progress.TRIMMING)

    assert rolled_back == [True]


# ── reading a row that is not the shape we expect ────────────────────────────


def test_a_job_with_no_timeline_has_no_current_stage(db, job):
    job.stage_events = []

    assert stage_progress.current(job) is None
    assert stage_progress.events_of(job) == []


def test_junk_entries_are_dropped_rather_than_rendered(db, job):
    job.stage_events = [
        {"stage": stage_progress.QUEUED, "at": "2026-09-08T00:00:00+00:00"},
        "not an event",
        {"at": "2026-09-08T00:01:00+00:00"},
        {"stage": stage_progress.TRIMMING, "at": "2026-09-08T00:02:00+00:00"},
    ]

    assert [e["stage"] for e in stage_progress.events_of(job)] == [
        stage_progress.QUEUED,
        stage_progress.TRIMMING,
    ]
    assert stage_progress.current(job) == stage_progress.TRIMMING


def test_a_non_list_value_reads_as_no_timeline(db, job):
    job.stage_events = {"stage": "trimming"}

    assert stage_progress.events_of(job) == []


def test_recording_onto_a_junk_timeline_still_works(db, job):
    """A row written before this column existed, or by hand, must not wedge."""
    job.stage_events = None

    stage_progress.record(db, job, stage_progress.TRIMMING)

    assert [e["stage"] for e in stage_progress.events_of(job)] == [stage_progress.TRIMMING]


# ── the lifecycle hooks ──────────────────────────────────────────────────────


def test_failing_a_job_closes_the_timeline(db, job, monkeypatch):
    monkeypatch.setattr("src.video_pipeline.notify.notify_failure", lambda db, job: None)
    job_service.advance(db, job, JobState.PROCESSING)
    stage_progress.record(db, job, stage_progress.FETCHING_SOURCE)

    job_service.fail(db, job, "download timed out")

    events = stage_progress.events_of(job)
    assert stage_progress.current(job) == stage_progress.FAILED
    # The stage before the failure is where it broke.
    assert events[-2]["stage"] == stage_progress.FETCHING_SOURCE


def test_retrying_a_job_starts_a_fresh_timeline(db, job, monkeypatch):
    """Interleaving the new run's steps with the failed run's would describe a
    sequence that never happened."""
    monkeypatch.setattr("src.video_pipeline.notify.notify_failure", lambda db, job: None)
    job_service.advance(db, job, JobState.PROCESSING)
    stage_progress.record(db, job, stage_progress.TRIMMING)
    job_service.fail(db, job, "ffmpeg exited 1")

    job_service.retry(db, job)

    assert [e["stage"] for e in stage_progress.events_of(job)] == [stage_progress.QUEUED]
