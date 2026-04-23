#!/usr/bin/env python3
"""Batch import Google Docs content into the topics table.

Input file formats:
- CSV (recommended)
- JSON array of objects

Required input field:
- google_doc_url (Google Docs URL or local file path)

Best local format for highest fidelity:
- Google Docs "Web page (.html, zipped)" export, then pass the .zip path

Supported local file types:
- .zip (Google Docs web export), .html/.htm, .md, .txt, .docx

Optional input fields (per row):
- topic_id
- slug
- title
- description
- goal_id
- goal_slug
- status               (draft|published|archived)
- sort_order           (int)
- video_embed_code
- action_items         (pipe-separated string in CSV, list in JSON)

Examples:
  uv run python scripts/import_topics_from_google_docs.py --input /path/to/topics.csv --dry-run
  uv run python scripts/import_topics_from_google_docs.py --input /path/to/topics.csv --provider openai
  uv run python scripts/import_topics_from_google_docs.py --input /path/to/topics.json --provider claude --create-missing
    uv run python scripts/import_topics_from_google_docs.py --input /path/to/topics.csv --provider openai --create-missing
"""

from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import json
import mimetypes
import os
import re
import sys
import urllib.parse
import uuid
import zipfile
from dataclasses import dataclass, field
from html import escape as _html_escape
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

from dotenv import load_dotenv
import requests
from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.db.base import get_engine

VALID_STATUS = {"draft", "published", "archived"}
GOOGLE_DOC_ID_RE = re.compile(r"/document/d/([a-zA-Z0-9_-]+)")

# ── CMM Design colors ─────────────────────────────────────────────────────────
CMM_TEAL = "#4F788D"
CMM_FOREST = "#2E5E4A"
CMM_NAVY = "#1E3A5F"
CMM_SEA_GLASS = "#B0C8C0"

load_dotenv()


@dataclass
class TopicImportRow:
    row_number: int
    source: str
    topic_id: str | None
    slug: str | None
    title: str | None
    description: str | None
    goal_id: str | None
    goal_slug: str | None
    status: str | None
    sort_order: int | None
    video_embed_code: str | None
    action_items: list[str] | None


@dataclass
class TopicPayload:
    title: str
    description: str | None
    summary_html: str | None
    content_html: str
    content_tiptap: dict | None
    action_items: list[str]
    read_time_minutes: int | None = field(default=None)
    watch_time_minutes: int | None = field(default=None)


def _clean_str(value: Any) -> str | None:
    if value is None:
        return None
    text_value = str(value).strip()
    return text_value or None


def _parse_action_items(value: Any) -> list[str] | None:
    if value is None:
        return None
    if isinstance(value, list):
        cleaned = [str(item).strip() for item in value if str(item).strip()]
        return cleaned or None
    raw = str(value).strip()
    if not raw:
        return None
    if raw.startswith("[") and raw.endswith("]"):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                cleaned = [str(item).strip() for item in parsed if str(item).strip()]
                return cleaned or None
        except json.JSONDecodeError:
            pass
    cleaned = [item.strip() for item in raw.split("|") if item.strip()]
    return cleaned or None


def _parse_int(value: Any) -> int | None:
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _load_rows(input_path: Path) -> list[TopicImportRow]:
    if not input_path.exists():
        raise FileNotFoundError(f"Input file not found: {input_path}")

    rows: list[dict[str, Any]]
    if input_path.suffix.lower() == ".csv":
        with input_path.open("r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            rows = [dict(row) for row in reader]
    elif input_path.suffix.lower() == ".json":
        with input_path.open("r", encoding="utf-8") as f:
            payload = json.load(f)
        if not isinstance(payload, list):
            raise ValueError("JSON input must be an array of objects")
        rows = [dict(item) for item in payload if isinstance(item, dict)]
    else:
        raise ValueError("Input must be .csv or .json")

    normalized: list[TopicImportRow] = []
    for idx, raw in enumerate(rows, start=2):
        source = _clean_str(
            raw.get("google_doc_url")
            or raw.get("doc_url")
            or raw.get("url")
            or raw.get("google_docs_url")
            or raw.get("google_doc_path")
            or raw.get("file_path")
            or raw.get("source")
        )
        if not source:
            raise ValueError(f"Row {idx}: missing google_doc_url (or source/file_path alias)")

        status = _clean_str(raw.get("status"))
        if status and status not in VALID_STATUS:
            raise ValueError(f"Row {idx}: invalid status '{status}'")

        normalized.append(
            TopicImportRow(
                row_number=idx,
                source=source,
                topic_id=_clean_str(raw.get("topic_id")),
                slug=_clean_str(raw.get("slug")),
                title=_clean_str(raw.get("title")),
                description=_clean_str(raw.get("description")),
                goal_id=_clean_str(raw.get("goal_id")),
                goal_slug=_clean_str(raw.get("goal_slug")),
                status=status,
                sort_order=_parse_int(raw.get("sort_order")),
                video_embed_code=_clean_str(raw.get("video_embed_code")),
                action_items=_parse_action_items(raw.get("action_items")),
            )
        )

    return normalized


def _extract_doc_id(doc_url: str) -> str:
    match = GOOGLE_DOC_ID_RE.search(doc_url)
    if not match:
        raise ValueError(f"Could not extract Google Doc ID from URL: {doc_url}")
    return match.group(1)


def _looks_like_http_url(value: str) -> bool:
    return value.startswith("http://") or value.startswith("https://")


def _read_text_file(path: Path) -> str:
    for encoding in ("utf-8", "utf-8-sig", "latin-1"):
        try:
            return path.read_text(encoding=encoding).strip()
        except UnicodeDecodeError:
            continue
    raise RuntimeError(f"Failed to decode text file: {path}")


def _markdown_to_html(markdown: str) -> str:
    lines = markdown.splitlines()
    html_lines: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        heading_match = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if heading_match:
            level = len(heading_match.group(1))
            content = heading_match.group(2).strip()
            html_lines.append(f"<h{level}>{content}</h{level}>")
        else:
            html_lines.append(f"<p>{stripped}</p>")
    return "\n".join(html_lines)


def _docx_to_html(path: Path) -> str:
    with zipfile.ZipFile(path, "r") as zf:
        try:
            xml_bytes = zf.read("word/document.xml")
        except KeyError as exc:
            raise RuntimeError(f"Invalid DOCX (missing word/document.xml): {path}") from exc

    root = ET.fromstring(xml_bytes)
    ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    paragraphs: list[str] = []
    for paragraph in root.findall(".//w:p", ns):
        text_parts = [
            node.text or ""
            for node in paragraph.findall(".//w:t", ns)
        ]
        text_value = "".join(text_parts).strip()
        if text_value:
            paragraphs.append(f"<p>{text_value}</p>")
    return "\n".join(paragraphs).strip()


_IMG_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"})


def _load_from_zip(path: Path) -> tuple[str, dict[str, bytes]]:
    """Load HTML and image bytes from a Google Docs web-export ZIP.

    Returns (html_string, {local_src_path: bytes}).
    """
    with zipfile.ZipFile(path, "r") as zf:
        html_candidates = [
            name for name in zf.namelist() if name.lower().endswith((".html", ".htm"))
        ]
        if not html_candidates:
            raise RuntimeError(f"ZIP does not contain an HTML file: {path}")
        html_name = sorted(html_candidates, key=lambda n: (n.count("/"), len(n)))[0]
        html = zf.read(html_name).decode("utf-8", errors="replace").strip()

        image_bytes: dict[str, bytes] = {}
        for name in zf.namelist():
            if Path(name).suffix.lower() in _IMG_EXTENSIONS:
                image_bytes[name] = zf.read(name)

    return html, image_bytes


def _fetch_google_doc_html(doc_url: str) -> str:
    doc_id = _extract_doc_id(doc_url)
    candidates = [
        f"https://docs.google.com/document/d/{doc_id}/export?format=html",
        f"https://docs.google.com/document/d/{doc_id}/mobilebasic",
        f"https://docs.google.com/document/d/{doc_id}/preview",
    ]

    last_error = "unknown error"
    for candidate in candidates:
        try:
            resp = requests.get(candidate, timeout=45)
            if resp.status_code >= 400:
                last_error = f"HTTP {resp.status_code}"
                continue
            content_type = (resp.headers.get("content-type") or "").lower()
            if "html" not in content_type and "text" not in content_type:
                last_error = f"Unexpected content-type: {content_type or 'unknown'}"
                continue
            html = resp.text.strip()
            if html:
                return html
            last_error = "Empty response body"
        except Exception as exc:  # noqa: BLE001
            last_error = str(exc)

    raise RuntimeError(f"Failed to fetch Google Doc HTML ({last_error})")


def _load_source(source: str) -> tuple[str, dict[str, bytes]]:
    """Load HTML and a map of local image src → bytes from any supported source."""
    if _looks_like_http_url(source):
        return _fetch_google_doc_html(source), {}

    source_path = Path(source).expanduser().resolve()
    if not source_path.exists():
        raise FileNotFoundError(f"Local source file not found: {source_path}")

    suffix = source_path.suffix.lower()

    if suffix == ".zip":
        return _load_from_zip(source_path)

    if suffix in {".html", ".htm"}:
        html = _read_text_file(source_path)
        # Collect images from sibling images/ directory (Google Docs HTML export pattern)
        image_bytes: dict[str, bytes] = {}
        images_dir = source_path.parent / "images"
        if images_dir.is_dir():
            for img_path in images_dir.iterdir():
                if img_path.is_file() and img_path.suffix.lower() in _IMG_EXTENSIONS:
                    image_bytes[f"images/{img_path.name}"] = img_path.read_bytes()
        return html, image_bytes

    if suffix in {".md", ".markdown"}:
        return _markdown_to_html(_read_text_file(source_path)), {}

    if suffix == ".txt":
        text_value = _read_text_file(source_path)
        return (
            "\n".join(
                f"<p>{line.strip()}</p>" for line in text_value.splitlines() if line.strip()
            ),
            {},
        )

    if suffix == ".docx":
        return _docx_to_html(source_path), {}

    raise RuntimeError(
        "Unsupported local file type. Use .zip (Google web export), .html, .md, .txt, or .docx"
    )


# ── S3 image upload ───────────────────────────────────────────────────────────

def _s3_client():
    import boto3
    from src.config import settings
    return boto3.client(
        "s3",
        aws_access_key_id=settings.aws_access_key_id,
        aws_secret_access_key=settings.aws_secret_access_key,
        region_name=settings.aws_region,
    ), settings


def _upload_images(
    image_bytes: dict[str, bytes],
    slug: str,
    dry_run: bool,
) -> dict[str, str]:
    """Upload image bytes to S3. Returns map of local_src → public S3 URL."""
    if not image_bytes:
        return {}

    s3, settings = _s3_client()
    url_map: dict[str, str] = {}

    for local_src, data in image_bytes.items():
        filename = Path(local_src).name
        content_type, _ = mimetypes.guess_type(filename)
        content_type = content_type or "image/png"
        s3_key = f"topics/{slug}/images/{filename}"
        s3_url = (
            f"https://{settings.s3_bucket_name}.s3.{settings.aws_region}.amazonaws.com/{s3_key}"
        )

        if dry_run:
            print(
                f"    [dry-run] would upload {len(data)} bytes"
                f" → s3://{settings.s3_bucket_name}/{s3_key}"
            )
        else:
            s3.put_object(
                Bucket=settings.s3_bucket_name,
                Key=s3_key,
                Body=data,
                ContentType=content_type,
            )

        url_map[local_src] = s3_url
        # Also index by bare filename so `src="image1.png"` forms resolve
        url_map[filename] = s3_url

    return url_map


def _replace_image_srcs(html: str, url_map: dict[str, str]) -> str:
    """Replace local image src values with S3 URLs."""
    if not url_map:
        return html

    def _sub(m: re.Match) -> str:
        src = m.group(1)
        if src in url_map:
            return f'src="{url_map[src]}"'
        filename = Path(src).name
        if filename in url_map:
            return f'src="{url_map[filename]}"'
        # Try with images/ prefix
        prefixed = f"images/{filename}"
        if prefixed in url_map:
            return f'src="{url_map[prefixed]}"'
        return m.group(0)

    return re.sub(r'src="([^"]*)"', _sub, html)


# ── Google export cleanup ─────────────────────────────────────────────────────

def _clean_google_export_html(html: str) -> str:
    """Pre-process Google Docs exported HTML to fix known quirks."""

    # 1. Unwrap Google redirect URLs: https://www.google.com/url?q=REAL_URL&...
    def _unwrap_redirect(m: re.Match) -> str:
        try:
            real_url = urllib.parse.unquote(m.group(1))
            return f'href="{real_url}"'
        except Exception:  # noqa: BLE001
            return m.group(0)

    html = re.sub(
        r'href="https://www\.google\.com/url\?q=([^&"]+)[^"]*"',
        _unwrap_redirect,
        html,
        flags=re.IGNORECASE,
    )

    # NOTE: [Visual ...] placeholder paragraphs are intentionally left in so the LLM
    # can generate actual CMM-styled HTML for them (see ## Visual Placeholders in prompt).

    return html


def _sanitize_html(html: str) -> str:
    # NOTE: <script> tags are intentionally NOT stripped here — chart blocks (with
    # Canvas/Chart.js) become rawHtml nodes that are rendered in sandboxed iframes,
    # which are the security boundary. Stripping scripts here would break chart blocks.
    cleaned = re.sub(r"<style[^>]*>.*?</style>", "", html, flags=re.IGNORECASE | re.DOTALL)
    cleaned = re.sub(r"\s+on\w+\s*=\s*(?:\"[^\"]*\"|'[^']*'|[^\s>]+)", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"(href|src)\s*=\s*\"javascript:[^\"]*\"", r"\1=\"#\"", cleaned, flags=re.IGNORECASE)
    return cleaned.strip()


# ── HTML → Tiptap JSON ────────────────────────────────────────────────────────
# Python port of app/components/content/html-to-tiptap.ts

_VOID_TAGS = frozenset({
    "area", "base", "br", "col", "embed", "hr", "img", "input",
    "link", "meta", "param", "source", "track", "wbr",
})
_SIMPLE_BLOCK_TAGS = frozenset({"h1", "h2", "h3", "h4", "h5", "h6", "p", "ul", "ol", "blockquote", "hr"})
_WRAPPER_TAGS = frozenset({"main", "article", "section", "div", "aside", "header", "footer", "nav"})
# NOTE: TABLE is handled natively via _table_to_tiptap_node when _CONVERT_TABLE is True
_COMPLEX_TAGS = frozenset({"canvas", "script", "style", "svg", "object", "embed", "figure", "form"})
# Set to True by --convert-table CLI flag; kept False by default to use rawHtml for tables
_CONVERT_TABLE: bool = False


class _HN:
    """Minimal DOM node for Tiptap conversion."""
    __slots__ = ("tag", "attrs", "children")

    def __init__(self, tag: str, attrs: dict[str, str]) -> None:
        self.tag = tag
        self.attrs = attrs
        self.children: list[_HN | str] = []


class _DOMBuilder(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._root = _HN("__root__", {})
        self._stack: list[_HN] = [self._root]

    @property
    def root(self) -> _HN:
        return self._root

    def handle_starttag(self, tag: str, attrs: list) -> None:
        # Strip XML-style self-closing slash from attrs
        attrs_dict = {k: (v or "") for k, v in attrs if k != "/"}
        node = _HN(tag, attrs_dict)
        self._stack[-1].children.append(node)
        if tag not in _VOID_TAGS:
            self._stack.append(node)

    def handle_endtag(self, tag: str) -> None:
        if tag in _VOID_TAGS:
            return
        for i in range(len(self._stack) - 1, 0, -1):
            if self._stack[i].tag == tag:
                self._stack = self._stack[:i]
                return

    def handle_data(self, data: str) -> None:
        if data:
            self._stack[-1].children.append(data)


def _dom_find(node: _HN, tag: str) -> _HN | None:
    if node.tag == tag:
        return node
    for child in node.children:
        if isinstance(child, _HN):
            result = _dom_find(child, tag)
            if result:
                return result
    return None


def _serialize_node(n: _HN | str) -> str:
    """Re-serialise a DOM node back to an HTML string for rawHtml blocks."""
    if isinstance(n, str):
        return _html_escape(n)
    tag = n.tag
    attrs_str = "".join(
        f' {k}="{_html_escape(v, quote=True)}"' for k, v in n.attrs.items()
    )
    if tag in _VOID_TAGS:
        return f"<{tag}{attrs_str}>"
    # Script and style content is raw text — must NOT be html-escaped or JS breaks
    if tag in ("script", "style"):
        inner = "".join(c if isinstance(c, str) else _serialize_node(c) for c in n.children)
        return f"<{tag}{attrs_str}>{inner}</{tag}>"
    inner = "".join(_serialize_node(c) for c in n.children)
    return f"<{tag}{attrs_str}>{inner}</{tag}>"


def _is_complex_node(el: _HN) -> bool:
    """Complex = needs rawHtml (canvas, SVG, form, or element with inline style).

    <a> tags are never complex — their href is captured as a Tiptap link mark and
    any inline color/style on them is intentionally ignored.
    """
    if el.tag in _COMPLEX_TAGS:
        return True
    # <a> styling is handled by the link mark — never treat it as complex
    if el.tag == "a":
        return False
    # <span> with only color/background-color is handled natively as a textStyle mark
    if el.tag == "span":
        style = el.attrs.get("style", "")
        other_styles = re.sub(r"(?:^|;)?\s*(?:background-)?color\s*:[^;]+", "", style).strip("; ")
        if not other_styles:
            return False
    # Elements with other inline styles → rawHtml to preserve CMM color/font styling
    if el.attrs.get("style"):
        return True
    return any(isinstance(c, _HN) and _is_complex_node(c) for c in el.children)


def _has_block_child(el: _HN) -> bool:
    return any(isinstance(c, _HN) and c.tag in _SIMPLE_BLOCK_TAGS for c in el.children)


def _inline_to_content(node: _HN | str) -> list[dict]:
    """Convert inline HTML to Tiptap inline content nodes (text + marks)."""
    if isinstance(node, str):
        text = re.sub(r"\s+", " ", node)
        return [{"type": "text", "text": text}] if text else []

    tag = node.tag
    marks: list[dict] = []
    if tag in ("strong", "b"):
        marks.append({"type": "bold"})
    elif tag in ("em", "i"):
        marks.append({"type": "italic"})
    elif tag == "u":
        marks.append({"type": "underline"})
    elif tag in ("s", "del", "strike"):
        marks.append({"type": "strike"})
    elif tag == "code":
        marks.append({"type": "code"})
    elif tag == "a":
        href = node.attrs.get("href", "")
        marks.append({"type": "link", "attrs": {"href": href, "target": "_blank"}})
    elif tag == "span":
        style = node.attrs.get("style", "")
        fg_match = re.search(r"(?<!background-)color\s*:\s*([^;]+)", style)
        if fg_match:
            marks.append({"type": "textStyle", "attrs": {"color": fg_match.group(1).strip()}})
    # other inline wrappers: just recurse

    result: list[dict] = []
    for child in node.children:
        for item in _inline_to_content(child):
            if marks and item.get("type") == "text":
                item = {**item, "marks": item.get("marks", []) + marks}
            result.append(item)
    return result


def _trim_content(content: list[dict]) -> list[dict]:
    if not content:
        return content
    result = [dict(n) for n in content]
    if result[0]["type"] == "text" and isinstance(result[0].get("text"), str):
        result[0]["text"] = result[0]["text"].lstrip()
    if result[-1]["type"] == "text" and isinstance(result[-1].get("text"), str):
        result[-1]["text"] = result[-1]["text"].rstrip()
    return [n for n in result if n["type"] != "text" or n.get("text")]


def _table_cell_to_tiptap(cell: _HN, cell_type: str) -> dict:
    """Convert a <td> or <th> element to a Tiptap tableCell / tableHeader node."""
    # Collect paragraph(s) from the cell content
    paragraphs: list[dict] = []
    inline_buffer: list[dict] = []

    def flush_inline() -> None:
        trimmed = _trim_content(inline_buffer[:])
        inline_buffer.clear()
        if trimmed:
            paragraphs.append({"type": "paragraph", "content": trimmed})
        else:
            paragraphs.append({"type": "paragraph"})

    for child in cell.children:
        if isinstance(child, str):
            text = re.sub(r"\s+", " ", child).strip()
            if text:
                inline_buffer.extend([{"type": "text", "text": text}])
        elif isinstance(child, _HN):
            if child.tag == "p":
                inline_buffer.extend(
                    [item for c in child.children for item in _inline_to_content(c)]
                )
                flush_inline()
            elif child.tag in ("br",):
                pass
            else:
                # inline element
                inline_buffer.extend(_inline_to_content(child))

    if inline_buffer or not paragraphs:
        flush_inline()

    node: dict = {"type": cell_type, "content": paragraphs}

    # Preserve colspan / rowspan attrs
    colspan = cell.attrs.get("colspan")
    rowspan = cell.attrs.get("rowspan")
    attrs: dict = {}
    if colspan and colspan != "1":
        attrs["colspan"] = int(colspan)
    if rowspan and rowspan != "1":
        attrs["rowspan"] = int(rowspan)

    # Preserve background-color from inline style for <th> (maps to CustomTableHeader.backgroundColor)
    if cell_type == "tableHeader":
        style = cell.attrs.get("style", "")
        bg_match = re.search(r"background-color:\s*([^;]+)", style)
        fg_match = re.search(r"(?<!background-)color:\s*([^;]+)", style)
        if bg_match:
            attrs["backgroundColor"] = bg_match.group(1).strip()
        if fg_match:
            attrs["textColor"] = fg_match.group(1).strip()

    if attrs:
        node["attrs"] = attrs

    return node


def _table_to_tiptap_node(table: _HN) -> dict | None:
    """Convert a <table> DOM node to a native Tiptap table node.

    Produces: { type: 'table', content: [ tableRow* ] }
    Each tableRow contains tableHeader or tableCell nodes.
    """
    rows: list[dict] = []

    def _collect_rows(node: _HN) -> None:
        for child in node.children:
            if isinstance(child, _HN):
                if child.tag == "tr":
                    cells: list[dict] = []
                    for cell in child.children:
                        if isinstance(cell, _HN):
                            if cell.tag == "th":
                                cells.append(_table_cell_to_tiptap(cell, "tableHeader"))
                            elif cell.tag == "td":
                                cells.append(_table_cell_to_tiptap(cell, "tableCell"))
                    if cells:
                        rows.append({"type": "tableRow", "content": cells})
                elif child.tag in ("thead", "tbody", "tfoot"):
                    _collect_rows(child)

    _collect_rows(table)
    if not rows:
        return None
    return {"type": "table", "content": rows}


def _apply_color_to_content(content: list[dict], color: str) -> list[dict]:
    """Add a textStyle color mark to any text nodes that don't already have one."""
    result = []
    for node in content:
        if node.get("type") == "text":
            existing_marks = node.get("marks", [])
            if not any(m.get("type") == "textStyle" for m in existing_marks):
                node = {**node, "marks": existing_marks + [{"type": "textStyle", "attrs": {"color": color}}]}
        result.append(node)
    return result


def _block_to_tiptap_node(el: _HN) -> dict | None:
    tag = el.tag
    if re.match(r"^h[1-6]$", tag):
        level = int(tag[1])
        inline = _trim_content(
            [item for c in el.children for item in _inline_to_content(c)]
        )
        style = el.attrs.get("style", "")
        fg_match = re.search(r"(?<!background-)color\s*:\s*([^;]+)", style)
        if fg_match:
            inline = _apply_color_to_content(inline, fg_match.group(1).strip())
        return {"type": "heading", "attrs": {"level": level}, "content": inline}

    if tag == "p":
        inline = _trim_content(
            [item for c in el.children for item in _inline_to_content(c)]
        )
        if not inline:
            return {"type": "paragraph"}
        return {"type": "paragraph", "content": inline}

    if tag in ("ul", "ol"):
        list_type = "bulletList" if tag == "ul" else "orderedList"
        items: list[dict] = []
        for child in el.children:
            if isinstance(child, _HN) and child.tag == "li":
                inline = _trim_content(
                    [item for c in child.children for item in _inline_to_content(c)]
                )
                items.append({
                    "type": "listItem",
                    "content": [{"type": "paragraph", "content": inline}],
                })
        return {"type": list_type, "content": items} if items else None

    if tag == "blockquote":
        inline = _trim_content(
            [item for c in el.children for item in _inline_to_content(c)]
        )
        return {
            "type": "blockquote",
            "content": [{"type": "paragraph", "content": inline}],
        }

    if tag == "hr":
        return {"type": "horizontalRule"}

    return None


def _walk_to_tiptap(el: _HN, nodes: list[dict]) -> None:
    for child in el.children:
        if isinstance(child, str):
            continue  # skip bare whitespace at block level
        tag = child.tag

        if tag in ("script", "style"):
            continue  # always skip

        if tag == "table":
            if _CONVERT_TABLE:
                node = _table_to_tiptap_node(child)
                if node:
                    nodes.append(node)
            else:
                nodes.append({"type": "rawHtml", "attrs": {"html": _serialize_node(child)}})
            continue

        if tag in _COMPLEX_TAGS:
            nodes.append({"type": "rawHtml", "attrs": {"html": _serialize_node(child)}})
            continue

        if tag in _SIMPLE_BLOCK_TAGS:
            if re.match(r"^h[1-6]$", tag):
                # Headings always become native Tiptap heading nodes (level determines style)
                node = _block_to_tiptap_node(child)
                if node:
                    nodes.append(node)
            elif _is_complex_node(child):
                # Other block elements with inline styles → rawHtml to preserve styling
                nodes.append({"type": "rawHtml", "attrs": {"html": _serialize_node(child)}})
            else:
                node = _block_to_tiptap_node(child)
                if node:
                    nodes.append(node)
            continue

        # Structural wrappers: rawHtml if complex (inline styles / canvas / script inside),
        # otherwise recurse to extract native Tiptap nodes
        if tag in _WRAPPER_TAGS:
            if _is_complex_node(child):
                nodes.append({"type": "rawHtml", "attrs": {"html": _serialize_node(child)}})
            else:
                _walk_to_tiptap(child, nodes)
            continue

        # Non-wrapper elements that happen to contain block children → unwrap
        if _has_block_child(child):
            _walk_to_tiptap(child, nodes)
            continue

        # Everything else → rawHtml
        nodes.append({"type": "rawHtml", "attrs": {"html": _serialize_node(child)}})


def _html_to_tiptap(html: str) -> dict:
    """Convert clean HTML to a Tiptap JSON document.

    - Headings → native Tiptap heading nodes
    - Tables → native Tiptap table / tableRow / tableHeader / tableCell nodes
    - p / ul / ol / blockquote / hr → native Tiptap nodes
    """
    builder = _DOMBuilder()
    builder.feed(html)
    container = _dom_find(builder.root, "body") or builder.root
    nodes: list[dict] = []
    _walk_to_tiptap(container, nodes)
    if not nodes:
        nodes = [{"type": "paragraph"}]
    return {"type": "doc", "content": nodes}


def _extract_title_from_html(html: str) -> str | None:
    title_match = re.search(r"<title[^>]*>(.*?)</title>", html, flags=re.IGNORECASE | re.DOTALL)
    if not title_match:
        return None
    title = re.sub(r"\s+", " ", title_match.group(1)).strip()
    title = re.sub(r"\s*-\s*Google Docs\s*$", "", title, flags=re.IGNORECASE).strip()
    return title or None


def _extract_body_html(html: str) -> str:
    body_match = re.search(r"<body[^>]*>(.*?)</body>", html, flags=re.IGNORECASE | re.DOTALL)
    if body_match:
        return body_match.group(1).strip()
    return html


def _strip_html(value: str | None) -> str:
    if not value:
        return ""
    text_value = re.sub(r"<[^>]+>", " ", value)
    text_value = re.sub(r"\s+", " ", text_value)
    return text_value.strip()


def _slugify(value: str) -> str:
    value = value.lower().strip()
    value = re.sub(r"[^a-z0-9]+", "-", value)
    return value.strip("-") or "untitled-topic"


def _heuristic_payload(raw_html: str, fallback_title: str | None) -> TopicPayload:
    body_html = _sanitize_html(_extract_body_html(raw_html))
    title = fallback_title or "Untitled Topic"

    paragraphs = re.findall(r"<p[^>]*>.*?</p>", body_html, flags=re.IGNORECASE | re.DOTALL)
    summary_html = "\n".join(paragraphs[:3]).strip() or None

    description = None
    if paragraphs:
        description = _strip_html(paragraphs[0])
        if len(description) > 220:
            description = description[:217].rstrip() + "..."

    content_tiptap = _html_to_tiptap(body_html)

    return TopicPayload(
        title=title,
        description=description,
        summary_html=summary_html,
        content_html=body_html,
        content_tiptap=content_tiptap,
        action_items=[],
    )


def _extract_json_object(text_value: str) -> dict[str, Any]:
    text_value = text_value.strip()
    try:
        parsed = json.loads(text_value)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    match = re.search(r"\{[\s\S]*\}", text_value)
    if not match:
        raise ValueError("LLM response did not include valid JSON")
    parsed = json.loads(match.group(0))
    if not isinstance(parsed, dict):
        raise ValueError("LLM response JSON must be an object")
    return parsed


_LLM_SYSTEM = (
    "You are a content processing assistant for College Money Method (CMM), "
    "an educational platform helping families navigate college financial aid. "
    "Convert exported Google Doc HTML into structured CMS topic fields. "
    "Return valid JSON only — no markdown fences, no extra text."
)

_LLM_PROMPT_TEMPLATE = """\
Convert this Google Docs HTML export into structured CMS topic fields.

## Output JSON Schema
{{
  "title": "string — clean title, strip bracketed prefixes like [T2]",
  "description": "string | null — 1–2 sentence overview from the document subtitle",
  "summary_html": "string | null — HTML for the Key Takeaways / Summary / Your Key Takeaways section (bullet list)",
  "content_html": "string — main article body HTML (see exclusions below)",
  "action_items": ["string"] — imperative bullets from Actions to Consider / Action Items section,
  "read_time_minutes": number | null — integer extracted from patterns like '5 min read',
  "watch_time_minutes": number | null — integer extracted from patterns like '3 min' near video reference
}}

## Content Exclusions (remove these from content_html)
- Document title / subtitle / breadcrumb header
- Video link + watch/read time metadata lines
- Key Takeaways / Summary / Your Key Takeaways section (→ summary_html instead)
- "What We Cover" table-of-contents section
- Actions to Consider / Action Items section (→ action_items instead)
- Helpful Resources section
- Common Questions / FAQ section
- [Note: ...] editor notes (remove them)
- Footnotes or annotation divs at the bottom

## Visual Placeholders
The document may contain paragraphs like: [Visual 1 — Net Price Formula: ...description...]
Read the description carefully and choose the correct visual type below. Replace EACH placeholder with the matching HTML pattern.

---

### Visual Type A — Chart (bar, doughnut, line, horizontal bar)
Use when the description mentions: chart, graph, bar, pie, donut, spectrum, range, trend, breakdown, comparison of numbers.

```html
<div style="background:#F7F9F8; border-left:4px solid #6B9D81; border-radius:6px; padding:24px; margin:32px 0;">
  <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
  <div style="font-family:'Inter',sans-serif; font-size:0.8rem; font-weight:600; color:#2E5E4A; text-transform:uppercase; letter-spacing:0.5px; margin-bottom:4px;">Visual N · Short Label</div>
  <div style="font-family:'Aleo',serif; font-size:1.2rem; font-weight:500; color:#4F788D; margin-bottom:16px;">One punchy insight headline — NOT a description of the chart type</div>
  <div style="position:relative; height:320px;">
    <canvas id="chartN"></canvas>
  </div>
  <script>
  (function() {{
    var colors = {{ teal:'#4F788D', green:'#6B9D81', seaGlass:'#B0C8C0', flax:'#F7DA7A', coral:'#D4604A', forest:'#2E5E4A', navy:'#1E3A5F' }};
    new Chart(document.getElementById('chartN'), {{
      type: 'bar',
      data: {{
        labels: ['Label A', 'Label B', 'Label C'],
        datasets: [
          {{
            label: 'Series 1',
            data: [12000, 18000, 9000],
            backgroundColor: colors.teal
          }},
          {{
            label: 'Series 2',
            data: [8000, 6000, 11000],
            backgroundColor: colors.green
          }}
        ]
      }},
      options: {{
        responsive: true,
        maintainAspectRatio: false,
        plugins: {{
          legend: {{ position: 'bottom', labels: {{ boxWidth: 14, padding: 14 }} }},
          tooltip: {{ callbacks: {{ label: function(c) {{ return c.dataset.label + ': $' + c.parsed.y.toLocaleString(); }} }} }}
        }},
        scales: {{
          x: {{ grid: {{ display: false }} }},
          y: {{ grid: {{ color: 'rgba(176,200,192,0.3)' }} }}
        }}
      }}
    }});
  }})();
  </script>
</div>
```

Chart type rules:
- CRITICAL: Always fill in real labels and data values derived from the placeholder description — NEVER leave `data: {{ ... }}` or any placeholder/ellipsis in the output. The example above shows the required structure: populate `labels`, `datasets[].label`, and `datasets[].data` with actual numbers.
- Bar (stacked or grouped) for comparisons across categories
- Doughnut for part-of-whole / breakdown
- Horizontal bar (indexAxis:'y') for spectrum/range or ranking
- Line for trends over time
- Height: 320px default, 360px for tall charts, 260px for compact
- Both <script> tags must stay inside the outer <div> so the block is self-contained
- Wrap init in an IIFE, use unique canvas ids (chart1, chart2, …), no top-level variables
- Subtitle: short punchy insight (≤10 words). Bad: "Stacked bar showing COA breakdown." Good: "Living costs stay constant — tuition drives the difference."

---

### Visual Type B — Formula / Equation
Use when the description mentions: equation, formula, calculation, "A – B = C", definition of a concept in one line.

```html
<div style="background:rgba(176,200,192,0.18); border:1px solid #B0C8C0; border-radius:8px; padding:32px 24px; text-align:center; margin:32px 0;">
  <div style="font-family:'Aleo',serif; font-weight:500; font-size:1.25rem; color:#2E5E4A; line-height:1.8;">
    <span style="display:inline-block; padding:6px 14px; background:#FFFFFF; border-radius:4px; margin:4px; border:1px solid #B0C8C0;">Term A</span>
    <span style="color:#D4604A; font-size:1.5rem; margin:0 8px; vertical-align:middle;">−</span>
    <span style="display:inline-block; padding:6px 14px; background:#FFFFFF; border-radius:4px; margin:4px; border:1px solid #B0C8C0;">Term B</span>
    <span style="color:#D4604A; font-size:1.5rem; margin:0 8px; vertical-align:middle;">=</span>
    <span style="display:inline-block; padding:6px 14px; background:#6B9D81; color:#FFFFFF; border-radius:4px; margin:4px; border:1px solid #6B9D81;">Result</span>
  </div>
</div>
```

Formula rules:
- Use the operator colors (coral #D4604A for −, =, +) and white pill terms
- The result/answer pill uses green (#6B9D81) background with white text
- Keep it one line if possible; wrap only if the equation has more than 4 terms
- No label or subtitle needed — the formula is self-explanatory

---

### Visual Type C — Side-by-Side Comparison Cards
Use when the description mentions: two-column, side-by-side, vs., Private vs. Public, comparison of two or more named options.

```html
<div style="display:grid; grid-template-columns:1fr 1fr; gap:20px; margin:32px 0;">
  <div style="background:#FFFFFF; border-top:4px solid #4F788D; border-radius:6px; padding:24px; box-shadow:0 1px 6px rgba(0,0,0,0.06);">
    <div style="font-family:'Aleo',serif; font-weight:500; font-size:1.1rem; color:#2C3E4A; margin-bottom:16px;">Option A Title</div>
    <div style="display:flex; justify-content:space-between; padding:10px 0; border-bottom:1px dashed rgba(176,200,192,0.6); font-size:0.95rem;">
      <span style="color:#586068;">Row label</span>
      <span style="font-weight:600; color:#2C3E4A;">Value</span>
    </div>
    <!-- repeat rows as needed -->
    <div style="margin-top:16px; background:#6B9D81; color:#FFFFFF; padding:14px; border-radius:4px; text-align:center; font-family:'Aleo',serif; font-weight:500; font-size:1.1rem;">
      Net result label: $X
    </div>
  </div>
  <div style="background:#FFFFFF; border-top:4px solid #E6B830; border-radius:6px; padding:24px; box-shadow:0 1px 6px rgba(0,0,0,0.06);">
    <!-- same structure for Option B, use border-top color #E6B830 (saffron) for second card -->
    <!-- net result div: use #2E5E4A (forest) background for contrast -->
  </div>
</div>
```

Comparison card rules:
- First card border-top: teal (#4F788D); second card: saffron (#E6B830)
- Net/result row background: green (#6B9D81) for first card, forest (#2E5E4A) for second
- Row dividers use dashed rgba(176,200,192,0.6) bottom border; last row has no divider
- Stack to single column on mobile is handled by the grid automatically
- Use actual numbers/values from the placeholder description

---

### Visual Type D — Multi-Column Tier / Category Diagram
Use when the description mentions: tiers, categories, columns, spectrum with named groups (no numeric data to chart).

```html
<div style="margin:32px 0;">
  <div style="font-family:'Inter',sans-serif; font-size:0.8rem; font-weight:600; color:#2E5E4A; text-transform:uppercase; letter-spacing:0.5px; margin-bottom:12px;">Visual N · Short Label</div>
  <div style="display:flex; gap:16px;">
    <div style="flex:1; background:#F7F9F8; border-top:4px solid #4F788D; border-radius:6px; padding:20px;">
      <div style="font-family:'Aleo',serif; font-weight:500; font-size:1rem; color:#4F788D; margin-bottom:12px;">Tier / Category Name</div>
      <ul style="list-style:none; padding:0; margin:0; font-size:0.9rem; color:#586068; line-height:1.7;">
        <li>• Item one</li>
        <li>• Item two</li>
      </ul>
    </div>
    <!-- repeat columns for each tier -->
  </div>
</div>
```

Tier diagram rules:
- Each column gets a different accent color on its top border: teal, green, saffron, coral in order
- Column header uses the same accent color as the top border
- Use bullet points (•) or short lines, not nested lists

---

### General Rules for All Visual Types
- Subtitle/headline must be punchy (≤10 words), not a description of the visual type
- Always use inline styles only — no class names, no external CSS
- CMM palette: teal=#4F788D, forest=#2E5E4A, navy=#1E3A5F, sea-glass=#B0C8C0, green=#6B9D81, flax=#F7DA7A, coral=#D4604A, saffron=#E6B830, text=#586068
- If the placeholder description is too vague to generate a meaningful visual, omit it entirely


## CMM Design Styling (apply inline styles to HTML elements)
- <table>: style="border-collapse: collapse; width: 100%;"
- <th>: style="background-color: {teal}; color: white; padding: 8px 12px; text-align: left; font-weight: 700; border-right: 1px solid rgba(255,255,255,0.2); border-bottom: 2px solid rgba(0,0,0,0.15); white-space: nowrap;"
- Last <th> in a row: add border-right: none; to remove the trailing right border
- <td>: style="padding: 8px 12px; border: 1px solid {sea_glass}; color: {navy}; vertical-align: top;"
- <a> links: style="color: {navy};"
- Do NOT add inline styles to <h1>, <h2>, <h3>, <h4>, <h5>, <h6>, <p>, <li>, <ul>, <ol>, <strong>, <em>

## HTML Rules
- Preserve tables as proper HTML (<table>, <thead>, <tbody>, <tr>, <th>, <td>).
- Remove Google CSS classes (class="c0", class="c12", etc.).
- Unwrap any Google redirect URLs — replace href="https://www.google.com/url?q=REAL&..." with href="REAL".
- Keep <strong>, <em>, <a href="...">, <br> inline formatting.
- Images: leave <img> tags as-is (src attributes have already been replaced with S3 URLs).
- No outer <html>/<head>/<body> wrappers — return only the inner body HTML.

FALLBACK TITLE: {fallback_title}

SOURCE HTML:
{raw_html}
""".format(
    teal=CMM_TEAL,
    forest=CMM_FOREST,
    navy=CMM_NAVY,
    sea_glass=CMM_SEA_GLASS,
    fallback_title="{fallback_title}",
    raw_html="{raw_html}",
)


def _build_prompt(raw_html: str, fallback_title: str | None) -> str:
    return _LLM_PROMPT_TEMPLATE.replace(
        "{fallback_title}", fallback_title or "None"
    ).replace("{raw_html}", raw_html)


def _call_openai(model: str, api_key: str, prompt: str) -> dict[str, Any]:
    schema = {
        "name": "topic_payload",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "description": {"type": ["string", "null"]},
                "summary_html": {"type": ["string", "null"]},
                "content_html": {"type": "string"},
                "action_items": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "read_time_minutes": {"type": ["integer", "null"]},
                "watch_time_minutes": {"type": ["integer", "null"]},
            },
            "required": [
                "title",
                "description",
                "summary_html",
                "content_html",
                "action_items",
                "read_time_minutes",
                "watch_time_minutes",
            ],
            "additionalProperties": False,
        },
    }

    resp = requests.post(
        "https://api.openai.com/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": model,
            "temperature": 0,
            "messages": [
                {"role": "system", "content": _LLM_SYSTEM},
                {"role": "user", "content": prompt},
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": schema,
            },
        },
        timeout=120,
    )
    resp.raise_for_status()
    payload = resp.json()
    content = payload["choices"][0]["message"]["content"]
    return _extract_json_object(content)


def _call_claude(model: str, api_key: str, prompt: str) -> dict[str, Any]:
    resp = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        },
        json={
            "model": model,
            "max_tokens": 8192,
            "temperature": 0,
            "system": _LLM_SYSTEM,
            "messages": [
                {"role": "user", "content": prompt},
            ],
        },
        timeout=180,
    )
    resp.raise_for_status()
    payload = resp.json()

    text_blocks = []
    for block in payload.get("content", []):
        if block.get("type") == "text":
            text_blocks.append(block.get("text", ""))
    return _extract_json_object("\n".join(text_blocks))


def _normalize_with_llm(
    provider: str,
    model: str,
    raw_html: str,
    fallback_title: str | None,
) -> TopicPayload:
    if provider == "none":
        return _heuristic_payload(raw_html, fallback_title)

    # Strip to body content only — removes Google Docs <head> CSS/JS to reduce token usage
    body_for_llm = _sanitize_html(_extract_body_html(raw_html))
    prompt = _build_prompt(raw_html=body_for_llm, fallback_title=fallback_title)

    if provider == "openai":
        api_key = os.getenv("OPENAI_API_KEY", "").strip()
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY is required when provider=openai")
        parsed = _call_openai(model=model, api_key=api_key, prompt=prompt)
    elif provider == "claude":
        api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
        if not api_key:
            raise RuntimeError("ANTHROPIC_API_KEY is required when provider=claude")
        parsed = _call_claude(model=model, api_key=api_key, prompt=prompt)
    else:
        raise ValueError(f"Unsupported provider: {provider}")

    title = _clean_str(parsed.get("title")) or fallback_title or "Untitled Topic"
    description = _clean_str(parsed.get("description"))
    summary_html = _clean_str(parsed.get("summary_html"))
    content_html = _clean_str(parsed.get("content_html")) or _extract_body_html(raw_html)
    content_html = _sanitize_html(content_html)

    action_items_raw = parsed.get("action_items")
    action_items: list[str] = []
    if isinstance(action_items_raw, list):
        action_items = [str(item).strip() for item in action_items_raw if str(item).strip()]

    # Parse optional timing fields
    read_time = parsed.get("read_time_minutes")
    watch_time = parsed.get("watch_time_minutes")
    read_time_minutes: int | None = int(read_time) if isinstance(read_time, (int, float)) else None
    watch_time_minutes: int | None = int(watch_time) if isinstance(watch_time, (int, float)) else None

    # Convert LLM HTML to Tiptap JSON
    content_tiptap = _html_to_tiptap(content_html)

    return TopicPayload(
        title=title,
        description=description,
        summary_html=summary_html,
        content_html=content_html,
        content_tiptap=content_tiptap,
        action_items=action_items,
        read_time_minutes=read_time_minutes,
        watch_time_minutes=watch_time_minutes,
    )


def _resolve_goal_id(conn, row: TopicImportRow) -> str | None:
    if row.goal_id:
        return row.goal_id
    if not row.goal_slug:
        return None

    result = conn.execute(
        text("SELECT id FROM goals WHERE slug = :slug LIMIT 1"),
        {"slug": row.goal_slug},
    ).fetchone()
    if not result:
        print(f"  [WARN] row {row.row_number}: goal_slug '{row.goal_slug}' not found — goal will be unset")
        return None
    return str(result[0])


def _compute_search_text(
    title: str,
    description: str | None,
    summary_html: str | None,
    content_html: str | None,
    action_items: list[str],
) -> str:
    parts = [
        title,
        description or "",
        _strip_html(summary_html),
        _strip_html(content_html),
        " ".join(action_items),
    ]
    return re.sub(r"\s+", " ", " ".join(parts)).strip()


def _upsert_topic(
    conn,
    row: TopicImportRow,
    payload: TopicPayload,
    create_missing: bool,
    dry_run: bool,
    overwrite: bool = False,
) -> tuple[str, str]:
    existing = None

    if row.topic_id:
        existing = conn.execute(
            text("SELECT id, slug, sort_order FROM topics WHERE id = :id LIMIT 1"),
            {"id": row.topic_id},
        ).fetchone()
    elif row.slug:
        existing = conn.execute(
            text("SELECT id, slug, sort_order FROM topics WHERE slug = :slug LIMIT 1"),
            {"slug": row.slug},
        ).fetchone()

    final_title = row.title or payload.title
    final_description = row.description if row.description is not None else payload.description
    final_summary = payload.summary_html
    final_content = payload.content_html
    final_action_items = row.action_items if row.action_items is not None else payload.action_items
    final_status = row.status or "draft"
    final_goal_id = _resolve_goal_id(conn, row)

    if final_status not in VALID_STATUS:
        final_status = "draft"

    search_text = _compute_search_text(
        title=final_title,
        description=final_description,
        summary_html=final_summary,
        content_html=final_content,
        action_items=final_action_items,
    )

    if existing:
        if not overwrite:
            final_slug = row.slug or str(existing[1])
            return "skipped", final_slug

        topic_id = str(existing[0])
        final_slug = row.slug or str(existing[1])
        final_sort_order = row.sort_order if row.sort_order is not None else int(existing[2] or 0)

        if dry_run:
            return "updated", final_slug

        # Store Tiptap JSON if available, otherwise fall back to HTML
        final_content_stored = (
            json.dumps(payload.content_tiptap)
            if payload.content_tiptap
            else final_content
        )

        conn.execute(
            text(
                """
                UPDATE topics
                SET
                    title = :title,
                    slug = :slug,
                    description = :description,
                    summary = :summary,
                    content = :content,
                    action_items = CAST(:action_items AS jsonb),
                    video_embed_code = :video_embed_code,
                    status = :status,
                    goal_id = :goal_id,
                    sort_order = :sort_order,
                    search_text = :search_text
                WHERE id = :id
                """
            ),
            {
                "id": topic_id,
                "title": final_title,
                "slug": final_slug,
                "description": final_description,
                "summary": final_summary,
                "content": final_content_stored,
                "action_items": json.dumps(final_action_items),
                "video_embed_code": row.video_embed_code,
                "status": final_status,
                "goal_id": final_goal_id,
                "sort_order": final_sort_order,
                "search_text": search_text,
            },
        )
        return "updated", final_slug

    if not create_missing:
        lookup = row.topic_id or row.slug or "(none)"
        raise RuntimeError(
            f"Topic not found for row {row.row_number} (topic_id/slug={lookup}). Use --create-missing to insert."
        )

    final_slug = row.slug or _slugify(final_title)
    final_sort_order = row.sort_order or 0

    # Ensure slug is unique — append UTC timestamp if a collision exists
    slug_exists = conn.execute(
        text("SELECT 1 FROM topics WHERE slug = :slug LIMIT 1"),
        {"slug": final_slug},
    ).fetchone()
    if slug_exists:
        ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        final_slug = f"{final_slug}-{ts}"

    if dry_run:
        return "inserted", final_slug

    # Store Tiptap JSON if available, otherwise fall back to HTML
    final_content_stored = (
        json.dumps(payload.content_tiptap)
        if payload.content_tiptap
        else final_content
    )

    new_id = str(uuid.uuid4())
    conn.execute(
        text(
            """
            INSERT INTO topics (
                id, title, slug, description, summary, content,
                action_items, video_embed_code, status, goal_id,
                sort_order, search_text
            ) VALUES (
                :id, :title, :slug, :description, :summary, :content,
                CAST(:action_items AS jsonb), :video_embed_code, :status, :goal_id,
                :sort_order, :search_text
            )
            """
        ),
        {
            "id": new_id,
            "title": final_title,
            "slug": final_slug,
            "description": final_description,
            "summary": final_summary,
            "content": final_content_stored,
            "action_items": json.dumps(final_action_items),
            "video_embed_code": row.video_embed_code,
            "status": final_status,
            "goal_id": final_goal_id,
            "sort_order": final_sort_order,
            "search_text": search_text,
        },
    )
    return "inserted", final_slug


def _choose_provider(provider: str) -> str:
    if provider != "auto":
        return provider
    if os.getenv("OPENAI_API_KEY", "").strip():
        return "openai"
    if os.getenv("ANTHROPIC_API_KEY", "").strip():
        return "claude"
    return "none"


def _default_model(provider: str) -> str:
    if provider == "openai":
        return "gpt-4.1"
    if provider == "claude":
        return "claude-3-7-sonnet-latest"
    return "none"


def main() -> int:
    parser = argparse.ArgumentParser(description="Batch import Google Docs content into topics")
    parser.add_argument("--input", required=True, help="Path to CSV or JSON input file")
    parser.add_argument(
        "--provider",
        choices=["auto", "openai", "claude", "none"],
        default="auto",
        help="LLM provider used to normalize content",
    )
    parser.add_argument("--model", default=None, help="Model name overwrite")
    parser.add_argument(
        "--create-missing",
        action="store_true",
        help="Insert rows when topic_id/slug does not exist",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print changes without writing to database",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite existing topics when the slug already exists (default: skip existing)",
    )
    parser.add_argument(
        "--convert-table",
        action="store_true",
        default=False,
        help="Convert HTML <table> elements to native Tiptap table nodes instead of rawHtml",
    )
    args = parser.parse_args()

    # Wire the module-level flag so _html_to_tiptap picks it up
    global _CONVERT_TABLE
    _CONVERT_TABLE = args.convert_table

    input_path = Path(args.input).resolve()
    rows = _load_rows(input_path)

    provider = _choose_provider(args.provider)
    model = args.model or _default_model(provider)

    _sep = "─" * 60
    print(_sep)
    print(f"  CMM Topic Import")
    print(f"  Input : {input_path}")
    print(f"  Rows  : {len(rows)}")
    print(f"  LLM   : {provider} / {model}")
    if args.dry_run:
        print("  Mode  : DRY RUN — no database writes")
    if args.create_missing:
        print("  Mode  : --create-missing enabled")
    if args.overwrite:
        print("  Mode  : --overwrite enabled (existing topics will be overwritten)")
    else:
        print("  Mode  : existing topics will be skipped (use --overwrite to update)")
    print(_sep)

    inserted = 0
    updated = 0
    skipped = 0
    failed = 0

    with get_engine().begin() as conn:
        for row in rows:
            try:
                print(f"\n{'━' * 60}")
                print(f"  Row {row.row_number}/{len(rows)}  {row.source}")
                print(f"{'━' * 60}")

                # 1. Load HTML + image bytes
                print("  [1/5] Loading source HTML …", end="", flush=True)
                raw_html, image_bytes = _load_source(row.source)
                print(f" done  ({len(raw_html):,} chars, {len(image_bytes)} image(s))")

                # 2. Determine slug early (needed for S3 key prefix)
                preliminary_slug = (
                    row.slug
                    or _slugify(row.title or _extract_title_from_html(raw_html) or "topic")
                )

                # 3. Upload images to S3 and replace local src attrs
                if image_bytes:
                    print(f"  [2/5] Uploading {len(image_bytes)} image(s) to S3 …", end="", flush=True)
                else:
                    print("  [2/5] No images to upload", end="", flush=True)
                image_url_map = _upload_images(
                    image_bytes=image_bytes,
                    slug=preliminary_slug,
                    dry_run=args.dry_run,
                )
                if image_url_map:
                    raw_html = _replace_image_srcs(raw_html, image_url_map)
                    print(f" done  ({len(image_url_map)} URL(s) replaced)")
                else:
                    print(" done")

                # 4. Pre-clean Google export quirks
                print("  [3/5] Cleaning Google export HTML …", end="", flush=True)
                raw_html = _clean_google_export_html(raw_html)
                print(" done")

                # 5. LLM normalisation
                fallback_title = row.title or _extract_title_from_html(raw_html)
                print(f"  [4/5] Sending to {provider} ({model}) for normalization …", end="", flush=True)
                payload = _normalize_with_llm(
                    provider=provider,
                    model=model,
                    raw_html=raw_html,
                    fallback_title=fallback_title,
                )
                tiptap_blocks = (
                    len(payload.content_tiptap.get("content", []))
                    if payload.content_tiptap
                    else 0
                )
                raw_block_count = sum(
                    1
                    for b in (payload.content_tiptap or {}).get("content", [])
                    if b.get("type") == "rawHtml"
                )
                timing_parts = []
                if payload.read_time_minutes:
                    timing_parts.append(f"read {payload.read_time_minutes} min")
                if payload.watch_time_minutes:
                    timing_parts.append(f"watch {payload.watch_time_minutes} min")
                timing_str = f"  ({', '.join(timing_parts)})" if timing_parts else ""
                print(
                    f" done\n"
                    f"       title        : {payload.title!r}\n"
                    f"       tiptap blocks: {tiptap_blocks} total, {raw_block_count} rawHtml"
                    f"{timing_str}"
                )

                # 6. Write to database
                print(f"  [5/5] Writing to database …", end="", flush=True)
                operation, slug = _upsert_topic(
                    conn=conn,
                    row=row,
                    payload=payload,
                    create_missing=args.create_missing,
                    dry_run=args.dry_run,
                    overwrite=args.overwrite,
                )
                if operation == "inserted":
                    inserted += 1
                elif operation == "updated":
                    updated += 1
                else:
                    skipped += 1
                print(f" done  [{operation}] slug={slug}")

            except Exception as exc:  # noqa: BLE001
                failed += 1
                print(f"\n  [ERROR] row {row.row_number}: {exc}")

    print(f"\n{'─' * 60}")
    print("  Summary")
    print(f"{'─' * 60}")
    print(f"  inserted : {inserted}")
    print(f"  updated  : {updated}")
    print(f"  skipped  : {skipped}")
    print(f"  failed   : {failed}")
    if args.dry_run:
        print("  dry-run  : no database writes were committed")
    print(f"{'─' * 60}")

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
