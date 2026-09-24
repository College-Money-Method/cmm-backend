"""Response shapes for the admin video pipeline endpoints."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field


class VideoJobStageEvent(BaseModel):
    """One step of the pipeline, stamped when it started.

    ``at`` is passed through as the string the row holds rather than parsed into
    a datetime: this list exists to diagnose a job, and a row with one malformed
    timestamp must still render instead of failing the whole response.
    """

    stage: str
    at: str = ""


class VideoJobSummary(BaseModel):
    """One row in the monitoring list.

    The three flags are the reason the list exists. A job that fell back to the
    first caption cue for its trim point, hit the chapter cap, or published a
    title the transcript never confirms is `published` like any other — nothing
    alerts, and without these it takes opening every job to find the one that
    went subtly wrong.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    # Null for an audit run, which was started from a pasted source and has no
    # webinar to publish to.
    webinar_id: uuid.UUID | None = None
    webinar_name: str | None = None
    workshop_name: str | None = None
    zoom_webinar_id: str | None = None
    zoom_recording_uuid: str
    state: str
    attempt: int
    error: str | None = None
    vimeo_video_id: str | None = None
    source_duration_seconds: int | None = None
    chapter_count: int = 0
    trim_fallback_used: bool = False
    chapters_truncated: bool = False
    # Chapters whose title was not found in the speech around it. Advisory: a
    # title card the presenter never read aloud is common and correct.
    unconfirmed_chapters: int = 0
    # An audit run: uploaded to the Vimeo audit folder and stopped there. It
    # wrote no embed code and deleted no Zoom recording, so a `published` audit
    # run has changed nothing a school can see.
    audit_only: bool = False
    source_url: str | None = None
    # The transcript an operator supplied with the source, if any. Shown because
    # its absence explains a frames-only chapter list.
    transcript_url: str | None = None
    # The step the job is on, finer than `state` — `processing` covers the Zoom
    # download, the trim, the Vimeo upload and the transcode wait, which is most
    # of the runtime. None for a job that ran before stages were recorded.
    stage: str | None = None
    # Translated captions are made after the replay is live, so this runs on its
    # own clock: `pending` while Vimeo's English transcript is still being
    # waited for, then `running`, `completed`, `failed`, or `skipped` when no
    # transcript ever appeared.
    captions_state: str = "pending"
    captions_error: str | None = None
    created_at: datetime
    updated_at: datetime


class VideoJobDetail(VideoJobSummary):
    """Everything the pipeline learned, for diagnosing one job."""

    ecs_task_arn: str | None = None
    trim_offset_seconds: Decimal | None = None
    vimeo_hash: str | None = None
    chapters: list | None = None
    failed_notified_at: datetime | None = None
    archive_key: str | None = None
    archive_expires_at: datetime | None = None
    # Decided server-side: the screen renders both on the server and in the
    # browser, and a clock read in each would disagree across that boundary.
    archive_expired: bool = False
    frames_prefix: str | None = None
    # Whether the retry button can do anything, and why not when it cannot. An
    # enabled button that is certain to fail is worse than a disabled one with
    # a reason beside it.
    retryable: bool = False
    retry_blocked_reason: str | None = None
    # The same pair for the force retry, which re-runs a *published* job whose
    # output an operator judged wrong. Separate fields rather than a widened
    # `retryable` so the screen can keep the two apart: one is recovery from a
    # failure, the other is discarding work that succeeded.
    force_retryable: bool = False
    force_retry_blocked_reason: str | None = None
    # The whole timeline, in order. Each stage lasted until the next one began,
    # so the screen derives per-step durations from this alone.
    stage_events: list[VideoJobStageEvent] = Field(default_factory=list)
    # The stages *this* job will take, ordered. Sent because it is a property of
    # the job, not of the screen: an audit run never deletes the Zoom recording
    # or writes an embed code, and rendering those as pending would be a lie.
    stage_plan: list[str] = Field(default_factory=list)


class VideoRetryRequest(BaseModel):
    """Body of a retry. Empty means the ordinary retry of a failed job.

    ``force`` is what lets a *published* job be re-run: the state check it skips
    is the one protecting finished work, so it is an explicit flag rather than a
    separate endpoint that could be called by accident.
    """

    force: bool = False


class VideoRunCreate(BaseModel):
    """A run started by hand from the admin screen.

    One paste field on purpose: a Zoom meeting ID, a Zoom recording UUID, a
    Zoom meeting link and an https download URL are all told apart by their
    shape, so asking the operator to also classify what they pasted only adds a
    way to get it wrong.
    """

    source: str = Field(min_length=1, max_length=2048)
    # Names the Vimeo video and nothing else. An audit run never writes to the
    # webinar it names.
    webinar_id: uuid.UUID | None = None
    # Optional https URL for the WebVTT transcript. A separate field rather than
    # a second guess at the paste field, because a source and its captions are
    # two different things and a run needs both to chapter the way the
    # unattended pipeline does. Empty means none was supplied.
    transcript_url: str | None = Field(default=None, max_length=2048)


class VideoRunStarted(BaseModel):
    job: VideoJobDetail
    # False when the job was created but no ECS task could be launched — over
    # the concurrency cap, or ECS not configured. The job stays `pending` and
    # the sweeper picks it up, so this is information, not a failure.
    dispatched: bool


class VideoJobList(BaseModel):
    items: list[VideoJobSummary]
    total: int
    limit: int
    offset: int


class VideoJobFrame(BaseModel):
    """One sampled frame with a short-lived URL the browser can load directly."""

    index: int
    timestamp: float
    filename: str
    url: str


class VideoJobFrames(BaseModel):
    items: list[VideoJobFrame]
    # Seconds the URLs above remain valid, so the screen can refetch rather
    # than showing broken images after a long-open tab. None: CDN URLs, which
    # do not expire.
    expires_in: int | None = None
