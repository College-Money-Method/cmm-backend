"""Resolve and download the files of one Zoom cloud recording.

A recording is a *set* of files — several MP4 renditions, an M4A, chat, and
(when the account has it enabled) a VTT transcript. The pipeline wants exactly
two of them: the largest MP4, which is the full-resolution shared-screen
rendition, and the TRANSCRIPT.

Download URLs are re-fetched here on every run rather than read from something
saved at webhook time. Zoom's ``download_url`` is only usable with a credential,
so re-deriving it from S2S OAuth is what keeps the pipeline free of Zoom
credentials at rest.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import httpx

from src.integrations import zoom

logger = logging.getLogger(__name__)

_DOWNLOAD_TIMEOUT = httpx.Timeout(60.0, read=300.0)
_STREAM_CHUNK = 8 * 1024 * 1024


class RecordingFetchError(RuntimeError):
    """The recording could not be resolved or downloaded."""


@dataclass(frozen=True)
class FetchedRecording:
    """Local paths for one downloaded recording."""

    video_path: Path
    transcript_path: Path | None
    duration_seconds: int
    topic: str


def _files(payload: dict) -> list[dict]:
    return [f for f in (payload.get("recording_files") or []) if isinstance(f, dict)]


def select_video_file(payload: dict) -> dict:
    """Pick the MP4 to publish: the largest completed one.

    Zoom returns several MP4 entries for the same instance (shared screen with
    speaker view, speaker view alone, gallery view). Size is the reliable
    discriminator — ``recording_type`` names vary by account settings, and the
    biggest file is always the full composite the replay should use.
    """
    videos = [
        f
        for f in _files(payload)
        if (f.get("file_type") or "").upper() == "MP4"
        and (f.get("status") or "completed").lower() == "completed"
    ]
    if not videos:
        raise RecordingFetchError("Zoom recording has no completed MP4 file")
    return max(videos, key=lambda f: int(f.get("file_size") or 0))


def select_transcript_file(payload: dict) -> dict | None:
    """Pick the VTT transcript, or None when the account did not produce one.

    Missing is a real state, not an error: audio transcript is an account
    setting and can be off. The caller falls back to silence detection and logs
    loudly, because a missing transcript degrades trim quality on every future
    recording until someone flips the setting.
    """
    for f in _files(payload):
        if (f.get("file_type") or "").upper() == "TRANSCRIPT":
            return f
    return None


def _download(url: str, token: str, dest: Path) -> Path:
    """Stream one recording file to disk.

    Streamed rather than buffered: these are 1-2 GB files and the task has 4 GB
    of memory it also needs for ffmpeg.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        with httpx.stream(
            "GET",
            url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=_DOWNLOAD_TIMEOUT,
            follow_redirects=True,
        ) as resp:
            resp.raise_for_status()
            with dest.open("wb") as handle:
                for chunk in resp.iter_bytes(_STREAM_CHUNK):
                    handle.write(chunk)
    except httpx.HTTPError as exc:
        raise RecordingFetchError(f"Downloading {dest.name} from Zoom failed: {exc}") from exc

    if dest.stat().st_size == 0:
        raise RecordingFetchError(f"Zoom returned an empty file for {dest.name}")
    logger.info("Downloaded %s — %d bytes", dest.name, dest.stat().st_size)
    return dest


def fetch_recording(recording_uuid: str, work_dir: Path) -> FetchedRecording:
    """Download the MP4 and (when present) the VTT for ``recording_uuid``."""
    payload = zoom.get_recording(recording_uuid)
    if payload is None:
        raise RecordingFetchError(
            f"Zoom returned no recording for {recording_uuid} — it may have been "
            "deleted, or Zoom credentials are missing"
        )

    video = select_video_file(payload)
    download_url = video.get("download_url")
    if not download_url:
        raise RecordingFetchError("Zoom recording MP4 has no download_url")

    token = zoom.recording_access_token()
    video_path = _download(download_url, token, work_dir / "source.mp4")

    transcript_path: Path | None = None
    transcript = select_transcript_file(payload)
    if transcript and transcript.get("download_url"):
        try:
            transcript_path = _download(
                transcript["download_url"], token, work_dir / "source.vtt"
            )
        except RecordingFetchError as exc:
            # A missing transcript is survivable (silence-detection fallback);
            # failing the whole job over it is not worth it.
            logger.warning("Transcript download failed — continuing without it: %s", exc)
    else:
        logger.warning(
            "Zoom recording %s has no TRANSCRIPT file — audio transcript is "
            "probably disabled on the account; falling back to silence detection",
            recording_uuid,
        )

    return FetchedRecording(
        video_path=video_path,
        transcript_path=transcript_path,
        duration_seconds=int(payload.get("duration") or 0) * 60,
        topic=str(payload.get("topic") or ""),
    )
