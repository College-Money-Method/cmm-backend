"""Turn rich-text content into heading-scoped sections, then into search chunks.

Bodies are Tiptap JSON. Splitting at ``heading`` nodes keeps each chunk about
one idea, so a cited chunk lands the reader near the right part of the page.
Block nodes (paragraphs, list items) become separate lines so lists stay
readable for the model; inline runs join without inserted spaces.
"""

from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass

# ~400 tokens. Long enough to hold an explanation, short enough that a handful
# of chunks fit in one answer prompt.
MAX_CHUNK_CHARS = 1800
# Sections shorter than this ride along with their neighbour instead of
# becoming a chunk on their own (a lone heading + one line embeds poorly).
MIN_CHUNK_CHARS = 300

_BLOCK_END_RE = re.compile(r"</(?:p|li|h[1-6]|div)>|<br\s*/?>", re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")
_SPACE_RE = re.compile(r"[ \t\r\f\v]+")
_BLANK_LINES_RE = re.compile(r"\s*\n\s*")


@dataclass(frozen=True)
class Section:
    heading: str | None
    text: str


@dataclass(frozen=True)
class Chunk:
    heading: str | None
    text: str


_INLINE_TYPES = {"text", "hardBreak", "mention", "emoji"}


def _node_text(node) -> str:
    if not isinstance(node, dict):
        return ""
    if node.get("type") == "text":
        return str(node.get("text") or "")
    if node.get("type") == "hardBreak":
        return "\n"
    children = node.get("content") or []
    inline = all(isinstance(c, dict) and c.get("type") in _INLINE_TYPES for c in children)
    parts = [_node_text(c) for c in children]
    return "".join(parts) if inline else "\n".join(p for p in parts if p.strip())


def _parse_doc(value) -> dict | None:
    """The Tiptap node in `value`, or None when it is plain text / HTML."""
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.lstrip().startswith("{"):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


def plain_text(value) -> str:
    """Plain text from Tiptap JSON, a Tiptap dict, HTML or plain text."""
    if not value:
        return ""
    doc = _parse_doc(value)
    text = _node_text(doc) if doc is not None else str(value)
    if doc is None and "<" in text and ">" in text:
        # Legacy HTML fields (topic summaries): keep paragraph/list breaks.
        text = html.unescape(_TAG_RE.sub(" ", _BLOCK_END_RE.sub("\n", text)))
    return _BLANK_LINES_RE.sub("\n", _SPACE_RE.sub(" ", text)).strip()


def rich_text_sections(value, default_heading: str | None = None) -> list[Section]:
    """Split a Tiptap document at its headings. Non-JSON input is one section."""
    if not value:
        return []
    doc = _parse_doc(value)
    if doc is None:
        text = plain_text(value)
        return [Section(default_heading, text)] if text else []

    sections: list[Section] = []
    heading, parts = default_heading, []

    def flush() -> None:
        body = "\n".join(p for p in parts if p)
        if body:
            sections.append(Section(heading, body))

    for node in doc.get("content") or []:
        if node.get("type") == "heading":
            flush()
            heading, parts = plain_text(node) or heading, []
        else:
            parts.append(plain_text(node))
    flush()
    return sections


def _split_long(text: str, limit: int) -> list[str]:
    """Split on paragraph, then sentence boundaries so no piece exceeds `limit`."""
    if len(text) <= limit:
        return [text]
    pieces, current = [], ""
    for unit in re.split(r"(?<=\n)|(?<=[.!?])\s+", text):
        unit = unit.strip()
        if not unit:
            continue
        while len(unit) > limit:  # a single run-on sentence
            pieces.append(unit[:limit])
            unit = unit[limit:]
        if current and len(current) + 1 + len(unit) > limit:
            pieces.append(current)
            current = unit
        else:
            current = f"{current} {unit}".strip() if current else unit
    if current:
        pieces.append(current)
    return pieces


def chunk_sections(
    sections: list[Section], max_chars: int = MAX_CHUNK_CHARS, min_chars: int = MIN_CHUNK_CHARS
) -> list[Chunk]:
    """Pack sections into chunks: long ones split, short neighbours merged."""
    chunks: list[Chunk] = []
    pending: Chunk | None = None
    for section in sections:
        for piece in _split_long(section.text, max_chars):
            labelled = f"{section.heading}: {piece}" if pending and section.heading else piece
            if pending and len(pending.text) < min_chars and len(pending.text) + len(labelled) + 1 <= max_chars:
                pending = Chunk(pending.heading, f"{pending.text}\n{labelled}")
                continue
            if pending:
                chunks.append(pending)
            pending = Chunk(section.heading, piece)
    if pending:
        chunks.append(pending)
    return chunks
