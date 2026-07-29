"""WebVTT parsing, chunking and re-serialisation for caption translation.

The translator only ever sees cue *text*. Timestamps, cue identifiers, settings
(``align:start position:10%``) and NOTE/STYLE/REGION blocks are held aside and
re-emitted verbatim, so a model that mangles or drops a line can never corrupt
timing.

SRT input is accepted too — the only structural difference that matters here is
``,`` vs ``.`` as the millisecond separator — because admins commonly have SRT
exports to hand.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# "00:00:01.000 --> 00:00:04.000" with optional trailing cue settings.
# Hours are optional in WebVTT ("01:02.500"). SRT uses a comma separator.
_TIMING_RE = re.compile(
    r"^(?P<start>(?:\d{1,3}:)?\d{2}:\d{2}[.,]\d{1,3})\s*-->\s*"
    r"(?P<end>(?:\d{1,3}:)?\d{2}:\d{2}[.,]\d{1,3})(?P<settings>.*)$"
)

# Blocks that carry no translatable dialogue and must survive untouched.
_METADATA_PREFIXES = ("NOTE", "STYLE", "REGION")


class VttError(Exception):
    """Raised when the uploaded file is not usable WebVTT/SRT."""


@dataclass
class Cue:
    """One caption cue. Only ``text`` is ever sent to the translator."""

    index: int
    timing: str
    text: str
    identifier: str | None = None


@dataclass
class VttDocument:
    """Parsed caption file: translatable cues plus everything else, in order.

    ``blocks`` is the rendering order. Each entry is either a raw string (header,
    NOTE/STYLE block) or an int — the index of a cue in ``cues``.
    """

    cues: list[Cue] = field(default_factory=list)
    blocks: list[str | int] = field(default_factory=list)


def parse(raw: str) -> VttDocument:
    """Parse WebVTT (or SRT) into cues + passthrough blocks.

    Raises:
        VttError: when the file contains no timed cues at all.
    """
    if not raw or not raw.strip():
        raise VttError("The caption file is empty.")

    # Normalise line endings and strip a UTF-8 BOM, which breaks the WEBVTT header.
    text = raw.lstrip("﻿").replace("\r\n", "\n").replace("\r", "\n")

    doc = VttDocument()
    # Blank-line-separated blocks is the structure both VTT and SRT share.
    for chunk in re.split(r"\n\s*\n", text):
        block = chunk.strip("\n")
        if not block.strip():
            continue
        _consume_block(block, doc)

    if not doc.cues:
        raise VttError(
            "No caption cues found. The file must be WebVTT or SRT with "
            "'00:00:00.000 --> 00:00:02.000' timing lines."
        )

    return doc


def _consume_block(block: str, doc: VttDocument) -> None:
    """Classify one block as a cue or as passthrough content."""
    lines = block.split("\n")

    if lines[0].strip().startswith(_METADATA_PREFIXES) or lines[0].strip().startswith("WEBVTT"):
        doc.blocks.append(block)
        return

    # A cue is [optional identifier line] + timing line + text lines.
    timing_at = 0 if _TIMING_RE.match(lines[0].strip()) else 1
    if timing_at >= len(lines) or not _TIMING_RE.match(lines[timing_at].strip()):
        # Not a cue and not recognised metadata — keep it rather than lose it.
        doc.blocks.append(block)
        return

    identifier = lines[0].strip() if timing_at == 1 else None
    body = "\n".join(lines[timing_at + 1 :]).strip()
    if not body:
        # Timed but empty — nothing to translate, emit as-is.
        doc.blocks.append(block)
        return

    cue = Cue(
        index=len(doc.cues),
        timing=_normalise_timing(lines[timing_at].strip()),
        text=body,
        identifier=identifier,
    )
    doc.cues.append(cue)
    doc.blocks.append(cue.index)


def _normalise_timing(timing: str) -> str:
    """Convert an SRT timing line to WebVTT form (comma → period milliseconds)."""
    match = _TIMING_RE.match(timing)
    if not match:
        return timing
    start = match.group("start").replace(",", ".")
    end = match.group("end").replace(",", ".")
    return f"{start} --> {end}{match.group('settings')}"


def serialize(doc: VttDocument, translations: dict[int, str]) -> str:
    """Rebuild a WebVTT document, substituting translated text per cue index.

    Cues missing from ``translations`` keep their source text, so a partially
    failed translation still yields a valid, playable file.
    """
    parts: list[str] = []
    has_header = any(
        isinstance(b, str) and b.strip().startswith("WEBVTT") for b in doc.blocks
    )
    if not has_header:
        parts.append("WEBVTT")

    for block in doc.blocks:
        if isinstance(block, str):
            parts.append(block)
            continue
        cue = doc.cues[block]
        text = translations.get(cue.index, cue.text)
        lines = [cue.identifier, cue.timing, text] if cue.identifier else [cue.timing, text]
        parts.append("\n".join(lines))

    # WebVTT requires a blank line between blocks and a trailing newline.
    return "\n\n".join(parts) + "\n"


def chunk_cues(cues: list[Cue], char_budget: int, max_count: int) -> list[list[Cue]]:
    """Group cues into batches bounded by total characters and cue count.

    Keeps each Bedrock call comfortably inside Haiku's 8k output-token cap while
    staying large enough that the model sees neighbouring cues for context.
    A single oversized cue gets a batch to itself.
    """
    batches: list[list[Cue]] = []
    current: list[Cue] = []
    current_chars = 0

    for cue in cues:
        length = len(cue.text)
        if current and (current_chars + length > char_budget or len(current) >= max_count):
            batches.append(current)
            current, current_chars = [], 0
        current.append(cue)
        current_chars += length

    if current:
        batches.append(current)
    return batches
