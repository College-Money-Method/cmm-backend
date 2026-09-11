"""Write the processing task's outputs to S3 for the chaptering stage to read.

Three artefacts live under one prefix per job:

* ``frame_NNNN.jpg`` — one per distinct visual state, what the vision model sees
* ``candidates.json`` — ``[{index, timestamp, file}]``, the frame-to-time map
* ``transcript.json`` — cues already re-based onto the trimmed video's clock

The task and the API are different processes on different machines, so S3 is the
handoff. Chaptering reads all three and never touches the source video.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from botocore.exceptions import BotoCoreError, ClientError

from src.config import settings
from src.storage.s3_client import s3_client
from src.video_pipeline.ffmpeg_ops import Candidate
from src.video_pipeline.transcript import Cue

logger = logging.getLogger(__name__)

CANDIDATES_FILENAME = "candidates.json"
TRANSCRIPT_FILENAME = "transcript.json"


class ArtifactError(RuntimeError):
    """An artefact could not be written to or read from S3."""


def frames_prefix(job_id: str) -> str:
    """S3 prefix holding one job's frames and manifests."""
    return f"{settings.video_frames_prefix.strip('/')}/{job_id}/"


def _put_bytes(key: str, body: bytes, content_type: str) -> None:
    try:
        s3_client().put_object(
            Bucket=settings.s3_bucket_name, Key=key, Body=body, ContentType=content_type
        )
    except (BotoCoreError, ClientError) as exc:
        raise ArtifactError(f"Writing s3://{settings.s3_bucket_name}/{key} failed: {exc}") from exc


def upload_artifacts(
    job_id: str, candidates: list[Candidate], cues: list[Cue]
) -> str:
    """Upload frames plus both manifests. Returns the prefix they landed under.

    ``candidates.json`` always opens with a ``0.0`` entry so the chapter builder
    has an anchor for the opening chapter — Vimeo requires a chapter at 0 and the
    first *distinct state* is rarely exactly there.
    """
    if not settings.s3_bucket_name:
        raise ArtifactError("S3_BUCKET_NAME is not configured — cannot upload frames")

    prefix = frames_prefix(job_id)
    client = s3_client()
    for candidate in candidates:
        try:
            client.upload_file(
                str(candidate.path),
                settings.s3_bucket_name,
                f"{prefix}{candidate.path.name}",
                ExtraArgs={"ContentType": "image/jpeg"},
            )
        except (BotoCoreError, ClientError) as exc:
            raise ArtifactError(f"Uploading {candidate.path.name} failed: {exc}") from exc

    manifest = build_candidate_manifest(candidates)
    _put_bytes(
        f"{prefix}{CANDIDATES_FILENAME}",
        json.dumps(manifest, indent=2).encode("utf-8"),
        "application/json",
    )
    _put_bytes(
        f"{prefix}{TRANSCRIPT_FILENAME}",
        json.dumps([cue.as_dict() for cue in cues], indent=2).encode("utf-8"),
        "application/json",
    )
    logger.info(
        "Uploaded %d frames + manifests → s3://%s/%s",
        len(candidates),
        settings.s3_bucket_name,
        prefix,
    )
    return prefix


def build_candidate_manifest(candidates: list[Candidate]) -> list[dict[str, object]]:
    """Frame list with a synthetic ``0.0`` opening entry prepended.

    The opening entry carries no file: there is nothing to classify at 0.0, it
    exists so the chapter builder always has a chapter to start the video with.
    """
    manifest: list[dict[str, object]] = [{"index": 0, "timestamp": 0.0, "file": None}]
    manifest.extend(candidate.as_dict() for candidate in candidates)
    return manifest


def load_json_artifact(prefix: str, filename: str) -> object:
    """Read one JSON manifest back out of S3 (the chaptering stage's entry point)."""
    try:
        body = s3_client().get_object(
            Bucket=settings.s3_bucket_name, Key=f"{prefix}{filename}"
        )["Body"].read()
    except (BotoCoreError, ClientError) as exc:
        raise ArtifactError(f"Reading {prefix}{filename} failed: {exc}") from exc
    return json.loads(body.decode("utf-8"))


def download_frame(prefix: str, filename: str, dest: Path) -> Path:
    """Fetch one frame image to a local path (used by the chaptering stage)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        s3_client().download_file(
            settings.s3_bucket_name, f"{prefix}{filename}", str(dest)
        )
    except (BotoCoreError, ClientError) as exc:
        raise ArtifactError(f"Downloading {prefix}{filename} failed: {exc}") from exc
    return dest
