"""Classify each sampled frame into a segment type, and read title-card headings.

Extracting text off a title card is the easy half; deciding *which* frames are
title cards is the hard half, and it is why this is a vision call rather than OCR.
The classifier also has to name the frames that carry no text at all, because the
introduction, resource-center tour, and Q&A are real sections with no card to read.
"""

from __future__ import annotations

import base64
import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from src.video_pipeline.bedrock_client import BedrockCallError, call_json
from src.video_pipeline.ffmpeg_ops import Candidate

logger = logging.getLogger(__name__)

TITLE_CARD = "title_card"
CONTENT_SLIDE = "content_slide"
SPEAKER = "speaker"
SCREEN_SHARE_OTHER = "screen_share_other"
BLANK = "blank"
ERROR = "error"

VALID_TYPES = frozenset(
    {TITLE_CARD, CONTENT_SLIDE, SPEAKER, SCREEN_SHARE_OTHER, BLANK}
)

DEFAULT_CONCURRENCY = 4

_SYSTEM = """\
You classify single frames from a recorded webinar about college financial aid. \
The frame has already been cropped to remove overlays.

Choose exactly one type:
- "title_card": a slide whose only real content is a short section title, on a \
plain or lightly decorated background. This marks the start of a new section.
- "content_slide": a slide carrying substantive content — bullet lists, tables, \
charts, numbers, screenshots, or several paragraphs. Belongs inside a section \
rather than starting one.
- "speaker": a person on camera with no shared material.
- "screen_share_other": a shared screen that is not a slide — a website, browser, \
document, spreadsheet, or application window.
- "blank": empty, near-black, a loading state, or otherwise unreadable.

Reply with only a JSON object:
{"type": "<one of the five>", "heading": "<title text or empty string>"}

Rules for "heading":
- Fill it only for "title_card". Use an empty string for every other type.
- Copy the title exactly as written, preserving its capitalisation.
- Take only the title. Leave out subtitles, presenter names, logos, slide numbers, \
dates, and footers.
- A slide with a heading AND substantive content below it is "content_slide", not \
"title_card".\
"""


@dataclass(frozen=True)
class Classified:
    index: int
    timestamp: float
    file: str
    type: str
    heading: str
    error: str = ""

    def as_dict(self) -> dict[str, object]:
        data: dict[str, object] = {
            "index": self.index,
            "timestamp": self.timestamp,
            "file": self.file,
            "type": self.type,
            "heading": self.heading,
        }
        if self.error:
            data["error"] = self.error
        return data


def _encode(path: Path) -> str:
    return base64.standard_b64encode(path.read_bytes()).decode("ascii")


def classify_frame(candidate: Candidate, *, attempts: int = 2) -> Classified:
    """Classify one frame. Never raises — a frame that cannot be read is marked.

    A dropped candidate costs at most one chapter, so failing the whole replay
    over a single bad frame would be the wrong trade.
    """
    content = [
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/jpeg",
                "data": _encode(candidate.path),
            },
        },
        {"type": "text", "text": "Classify this frame."},
    ]

    last_error = ""
    for attempt in range(1, attempts + 1):
        try:
            parsed, _, _ = call_json(system=_SYSTEM, content=content, max_tokens=200)
        except BedrockCallError as exc:
            last_error = str(exc)
            logger.warning(
                "frame %d classify attempt %d/%d failed: %s",
                candidate.index, attempt, attempts, exc,
            )
            continue

        frame_type = str(parsed.get("type") or "").strip()
        if frame_type not in VALID_TYPES:
            last_error = f"unknown type {frame_type!r}"
            logger.warning("frame %d returned %s", candidate.index, last_error)
            continue

        heading = str(parsed.get("heading") or "").strip()
        # Only title cards carry a heading; drop one that leaked onto another type
        # so the chapter builder never titles a section from a content slide.
        if frame_type != TITLE_CARD:
            heading = ""
        return Classified(
            index=candidate.index,
            timestamp=candidate.timestamp,
            file=candidate.path.name,
            type=frame_type,
            heading=heading,
        )

    return Classified(
        index=candidate.index,
        timestamp=candidate.timestamp,
        file=candidate.path.name,
        type=ERROR,
        heading="",
        error=last_error or "classification failed",
    )


def classify_frames(
    candidates: list[Candidate], *, concurrency: int = DEFAULT_CONCURRENCY
) -> list[Classified]:
    """Classify every candidate, preserving timeline order in the result."""
    if not candidates:
        return []
    workers = max(1, min(concurrency, len(candidates)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(classify_frame, candidates))
    results.sort(key=lambda item: item.timestamp)

    failed = sum(1 for item in results if item.type == ERROR)
    if failed:
        logger.warning("%d of %d frames could not be classified", failed, len(results))
    return results
