"""Word-level timings for a finished reel's audio, from AWS Transcribe.

Zoom's transcript is cue-level — a few seconds of text per cue — which is fine
for choosing cuts but too coarse for captions that light up word by word. So
the ~60 seconds of *reel* audio is transcribed again, not the whole webinar:
cents instead of dollars, and the timings come out already on the reel's clock.

Transcribe batch jobs read their media from S3, so the audio is uploaded under
the job's own prefix and deleted afterwards, along with the transcription job.
No output bucket is given: Transcribe keeps the result and hands back a
short-lived presigned URL, so nothing else is left behind in the bucket.
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path

import boto3
import httpx
from botocore.exceptions import BotoCoreError, ClientError

from src.config import settings
from src.storage.s3_client import s3_client

logger = logging.getLogger(__name__)

_POLL_SECONDS = 5.0
_TIMEOUT_SECONDS = 600.0


class TranscribeError(RuntimeError):
    """The reel audio could not be transcribed."""


@dataclass(frozen=True)
class Word:
    start: float
    end: float
    text: str

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def transcribe_words(audio: Path, key_prefix: str, *, language: str = "en-US") -> list[Word]:
    """Upload `audio` (FLAC), transcribe it, and return its words in order."""
    if not settings.s3_bucket_name:
        raise TranscribeError("S3_BUCKET_NAME is not configured")
    key = f"{key_prefix.rstrip('/')}/reel-audio-{uuid.uuid4().hex[:8]}.flac"
    job_name = f"trailer-{uuid.uuid4().hex}"
    client = boto3.client(
        "transcribe",
        region_name=settings.aws_region,
        aws_access_key_id=settings.aws_access_key_id or None,
        aws_secret_access_key=settings.aws_secret_access_key or None,
    )
    try:
        s3_client().upload_file(str(audio), settings.s3_bucket_name, key,
                                ExtraArgs={"ContentType": "audio/flac"})
        client.start_transcription_job(
            TranscriptionJobName=job_name,
            LanguageCode=language,
            MediaFormat="flac",
            Media={"MediaFileUri": f"s3://{settings.s3_bucket_name}/{key}"},
        )
        uri = _wait(client, job_name)
        resp = httpx.get(uri, timeout=30.0)
        resp.raise_for_status()
        payload = resp.json()
    except (BotoCoreError, ClientError, httpx.HTTPError) as exc:
        raise TranscribeError(f"Transcribing reel audio failed: {exc}") from exc
    finally:
        _cleanup(client, job_name, key)
    words = parse_items(payload["results"]["items"])
    logger.info("Transcribed %d words from %s", len(words), audio.name)
    return words


def parse_items(items: list[dict]) -> list[Word]:
    """Transcribe items → words, with punctuation glued onto the word before it."""
    words: list[Word] = []
    for item in items:
        content = item["alternatives"][0]["content"]
        if item["type"] == "punctuation":
            if words:
                last = words[-1]
                words[-1] = Word(last.start, last.end, last.text + content)
            continue
        words.append(Word(float(item["start_time"]), float(item["end_time"]), content))
    return words


def _wait(client, job_name: str) -> str:
    deadline = time.monotonic() + _TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        job = client.get_transcription_job(TranscriptionJobName=job_name)["TranscriptionJob"]
        status = job["TranscriptionJobStatus"]
        if status == "COMPLETED":
            return job["Transcript"]["TranscriptFileUri"]
        if status == "FAILED":
            raise TranscribeError(f"Transcribe failed: {job.get('FailureReason')}")
        time.sleep(_POLL_SECONDS)
    raise TranscribeError(f"Transcribe job {job_name} did not finish in {_TIMEOUT_SECONDS:.0f}s")


def _cleanup(client, job_name: str, key: str) -> None:
    """Best effort: a leftover object or job costs nothing worth failing a reel over."""
    try:
        s3_client().delete_object(Bucket=settings.s3_bucket_name, Key=key)
    except (BotoCoreError, ClientError) as exc:
        logger.warning("Could not delete s3://%s/%s: %s", settings.s3_bucket_name, key, exc)
    try:
        client.delete_transcription_job(TranscriptionJobName=job_name)
    except (BotoCoreError, ClientError) as exc:
        # Also raised when the job was never created; nothing to clean up then.
        logger.debug("Could not delete transcription job %s: %s", job_name, exc)
