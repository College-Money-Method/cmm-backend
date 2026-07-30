"""Admin Video CC endpoints: start a caption-translation job, stream its progress.

    POST /api/v1/admin/video-cc/jobs          multipart → { job_id, video_ref }
    GET  /api/v1/admin/video-cc/jobs/{id}/events   text/event-stream

The job runs as a detached asyncio task, so closing the browser does not abort
an in-flight translation — the stream can be re-attached and replays the whole
event backlog from the start.

Auth note: both endpoints require super_admin. The stream is bearer-authed like
every other admin route, so the client must consume it with fetch + streaming
reader rather than EventSource (which cannot set headers).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from src.auth.deps import AdminDep
from src.config import SUPPORTED_LOCALES
from src.content.video_caption_archive import (
    get_record,
    list_records,
    presign_transcript,
    resolve_transcript_key,
)
from src.content.video_cc_jobs import create_job, get_job, watch
from src.content.video_cc_service import publish_edited_track, run_job
from src.content.vtt_parser import VttError
from src.db.deps import DbDep
from src.integrations.vimeo import VimeoError, extract_video_ref

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/admin/video-cc", tags=["admin-video-cc"])

# A 5 MB VTT is roughly a 12-hour transcript — far past anything legitimate,
# and a useful guard against a mis-picked file.
_MAX_FILE_BYTES = 5 * 1024 * 1024

# Each locale is a full pass over the transcript; cap the fan-out per job.
_MAX_LOCALES = 5

# Download links are short-lived — long enough to click, not to circulate.
_DOWNLOAD_TTL = 300

# asyncio keeps only weak references to tasks, so an unreferenced task can be
# garbage-collected mid-flight. Hold strong refs until each job completes.
_running: set[asyncio.Task] = set()


class JobStartResponse(BaseModel):
    job_id: str
    video_ref: str
    locales: list[str]


class VideoRecordOut(BaseModel):
    """One processed video, as shown in the admin list."""

    video_id: str
    privacy_hash: str | None
    title: str | None
    vimeo_created_at: datetime | None
    duration_seconds: int | None
    source_origin: str | None
    source_cue_count: int | None
    source_s3_key: str | None
    # locale -> {language, vimeo_code, track_uri, cue_count, missing_cues, s3_key, ...}
    translations: dict
    last_status: str | None
    last_run_at: datetime | None


class TranscriptDownload(BaseModel):
    url: str
    filename: str
    expires_in_seconds: int


class TrackPublished(BaseModel):
    locale: str
    language: str
    vimeo_code: str
    track_uri: str
    replaced: bool
    cue_count: int


@router.get("/videos", response_model=list[VideoRecordOut])
def list_processed_videos(_admin: AdminDep, db: DbDep = None) -> list[VideoRecordOut]:
    """Videos that have been through Video CC, most recently run first."""
    return [VideoRecordOut.model_validate(r, from_attributes=True) for r in list_records(db)]


@router.get("/videos/{video_id}/transcripts/{label}", response_model=TranscriptDownload)
def download_transcript(
    video_id: str, label: str, _admin: AdminDep, db: DbDep = None
) -> TranscriptDownload:
    """Short-lived download URL for an archived transcript.

    - **label**: ``source`` for the English original, or a locale code (``es``).

    The S3 key is resolved from the registry, never taken from the caller, so
    this cannot be used to read arbitrary bucket objects.
    """
    record = get_record(db, video_id)
    if record is None:
        raise HTTPException(status_code=404, detail="This video has no Video CC history.")

    key = resolve_transcript_key(record, label)
    if not key:
        raise HTTPException(
            status_code=404,
            detail=f"No archived '{label}' transcript for this video. Runs from before "
            "archiving was added have no stored file.",
        )

    slug = _slugify(record.title or video_id)
    url = presign_transcript(key, f"{slug}-{label}.vtt", expires_in=_DOWNLOAD_TTL)
    if url is None:
        raise HTTPException(status_code=502, detail="Could not generate a download link.")

    return TranscriptDownload(
        url=url, filename=f"{slug}-{label}.vtt", expires_in_seconds=_DOWNLOAD_TTL
    )


@router.post("/videos/{video_id}/tracks", response_model=TrackPublished)
async def publish_track(
    video_id: str,
    _admin: AdminDep,
    locale: str = Form(..., description="Target locale of the edited track, e.g. 'es'"),
    privacy_hash: str | None = Form(None, description="Required for unlisted videos"),
    file: UploadFile = File(..., description="Edited .vtt/.srt for this language"),
    db: DbDep = None,
) -> TrackPublished:
    """Publish a hand-edited caption file to Vimeo without translating it.

    For the download → fix a line → re-publish loop. Unlike ``POST /jobs``, the
    file is treated as the finished track for ``locale``, not as English source.
    """
    targets = _parse_locales(locale)
    if len(targets) != 1:
        raise HTTPException(status_code=400, detail="Provide exactly one locale.")

    # Raises 400 on an empty, oversized or non-UTF-8 file.
    content = await _read_upload(file)

    # The record stores the hash, so the caller usually need not supply one.
    record = get_record(db, video_id)
    resolved_hash = privacy_hash or (record.privacy_hash if record else None)
    video_ref = f"{video_id}:{resolved_hash}" if resolved_hash else video_id

    try:
        result = await asyncio.to_thread(
            publish_edited_track, db, video_ref, targets[0], content
        )
    except (VimeoError, VttError) as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    logger.info(
        "video_cc: published edited %s track for video=%s", targets[0], video_id
    )
    return TrackPublished(
        locale=targets[0],
        language=result["language"],
        vimeo_code=result["vimeo_code"],
        track_uri=result["track_uri"],
        replaced=result["replaced"],
        cue_count=result["cue_count"],
    )


def _slugify(text: str) -> str:
    """Filesystem-safe, lowercase slug for a download filename."""
    cleaned = re.sub(r"[^\w\s-]", "", text).strip().lower()
    return re.sub(r"[\s_]+", "-", cleaned)[:80] or "transcript"


@router.post("/jobs", response_model=JobStartResponse)
async def start_job(
    _admin: AdminDep,
    video: str = Form(..., description="Vimeo URL, embed code, or numeric video ID"),
    locales: str = Form(..., description="Comma-separated target locale codes, e.g. 'es,zh'"),
    file: UploadFile | None = File(
        None, description="Optional transcript (.vtt/.srt); omit to use the video's own track"
    ),
) -> JobStartResponse:
    """Validate input, start the translation job, return its id for streaming.

    With no ``file``, the English track already on the video (Vimeo's AI captions
    or a previous upload) is downloaded and used as the translation source.
    """
    try:
        video_ref = extract_video_ref(video)
    except VimeoError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    targets = _parse_locales(locales)
    # An empty multipart part arrives as an UploadFile with no filename.
    content = await _read_upload(file) if file and file.filename else None

    job = create_job(video_ref)
    task = asyncio.create_task(run_job(job, content, targets))
    _running.add(task)
    task.add_done_callback(_running.discard)

    logger.info(
        "video_cc: job %s started — video=%s locales=%s source=%s",
        job.id,
        video_ref,
        targets,
        "upload" if content else "vimeo",
    )
    return JobStartResponse(job_id=job.id, video_ref=video_ref, locales=targets)


@router.get("/jobs/{job_id}/events")
async def stream_job_events(job_id: str, _admin: AdminDep) -> StreamingResponse:
    """Server-sent events for one job: backlog first, then live until it ends."""
    job = get_job(job_id)
    if job is None:
        raise HTTPException(
            status_code=404,
            detail="Job not found. It may have finished more than 30 minutes ago, "
            "or the server restarted.",
        )

    async def event_stream():
        try:
            async for event in watch(job):
                if event is None:
                    yield ": keep-alive\n\n"  # comment frame; ignored by clients
                else:
                    yield f"data: {json.dumps(event)}\n\n"
        except asyncio.CancelledError:
            # Client disconnected — the job itself keeps running.
            logger.debug("video_cc: stream for job %s closed by client", job_id)
            raise

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            # Stop nginx/ALB buffering, which would hold events until the end.
            "X-Accel-Buffering": "no",
        },
    )


def _parse_locales(raw: str) -> list[str]:
    """Split, validate and de-duplicate the requested target locales."""
    targets: list[str] = []
    for code in (c.strip() for c in raw.split(",")):
        if not code or code in targets:
            continue
        if code == "en":
            raise HTTPException(
                status_code=400, detail="'en' is the source language, not a target."
            )
        if code not in SUPPORTED_LOCALES:
            raise HTTPException(
                status_code=400,
                detail=f"Locale '{code}' is not supported. Supported: {sorted(SUPPORTED_LOCALES)}",
            )
        targets.append(code)

    if not targets:
        raise HTTPException(status_code=400, detail="Select at least one target language.")
    if len(targets) > _MAX_LOCALES:
        raise HTTPException(
            status_code=400, detail=f"At most {_MAX_LOCALES} languages per job."
        )
    return targets


async def _read_upload(file: UploadFile) -> str:
    """Read and decode the uploaded caption file, enforcing size and encoding."""
    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="The caption file is empty.")
    if len(raw) > _MAX_FILE_BYTES:
        raise HTTPException(
            status_code=400,
            detail=f"Caption file is too large (max {_MAX_FILE_BYTES // (1024 * 1024)} MB).",
        )
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise HTTPException(
            status_code=400,
            detail="The caption file must be UTF-8 encoded. Re-export it as UTF-8 and retry.",
        ) from exc
