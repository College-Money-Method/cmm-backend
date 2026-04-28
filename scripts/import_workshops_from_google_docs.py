#!/usr/bin/env python3
"""Batch import Google Docs workshop detail pages into the workshops table.

Uses an LLM (OpenAI or Claude) to reliably extract structured metadata from
Google Docs HTML exports — workshop name, description, grade level, key action
items, and the "WHAT WE COVER" objectives. Objectives are matched against the
database and linked via the objective_workshops junction table.

Input file: CSV or JSON array.

Required CSV column:
  source          Path to the Google Docs HTML export file

Optional CSV columns:
  workshop_id     UUID of an existing workshop (used for --overwrite lookup)
  name            overwrite the workshop name extracted from the HTML
  description     overwrite the description extracted from the HTML
  sequence_number Unique sort position (integer)
  suggested_grades e.g. "9th grade"
  resource_center_slug  Unique slug for the resource centre
  workshop_art_url      Hero image URL

Examples:
  uv run python scripts/import_workshops_from_google_docs.py \\
      --input scripts/input/workshops.csv --provider openai --dry-run

  uv run python scripts/import_workshops_from_google_docs.py \\
      --input scripts/input/workshops.csv --provider openai --create-missing

  uv run python scripts/import_workshops_from_google_docs.py \\
      --input scripts/input/workshops.csv --provider openai --overwrite
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from sqlalchemy import text
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Reuse parsing infrastructure from the topics import script.
# It has an if __name__ == "__main__" guard so importing is safe.
from scripts.import_topics_from_google_docs import (
    _DOMBuilder,
    _HN,
    _clean_google_export_html,
    _dom_find,
    _extract_json_object,
    _html_to_tiptap,
    _load_source,
    _sanitize_html,
    _serialize_node,
    _strip_html,
)
from src.db.base import get_engine

load_dotenv()

# ── CMM palette ───────────────────────────────────────────────────────────────
_TEAL = "#4F788D"
_FOREST = "#2E5E4A"
_GREEN = "#6B9D81"
_DARK = "#2C3E4A"
_TEXT = "#586068"
_CARD_BG = "#E8F0EC"


# ── Data structures ───────────────────────────────────────────────────────────


@dataclass
class WorkshopImportRow:
    row_number: int
    source: str
    workshop_id: str | None
    name: str | None
    description: str | None
    sequence_number: int | None
    suggested_grades: str | None
    resource_center_slug: str | None
    workshop_art_url: str | None


@dataclass
class WorkshopPayload:
    name: str
    description: str | None
    suggested_grades: str | None
    key_actions: str | None  # Tiptap JSON string
    body: str | None  # Tiptap JSON string (kept for legacy compat; set to None when objectives are linked)
    action_items: list[str]
    objective_names: list[str]  # raw names extracted from "WHAT WE COVER" section


# ── Objective name extraction ────────────────────────────────────────────────


_MAX_OBJECTIVE_LEN = 120  # longer lines are section descriptions, not objective names


def _get_text_lines(node: _HN) -> list[str]:
    """
    Collect non-empty text lines from a node, treating <br> children as line breaks.
    """
    parts: list[str] = []
    current: list[str] = []

    def _walk(n: _HN | str) -> None:
        if isinstance(n, str):
            current.append(n)
        elif n.tag == "br":
            line = "".join(current).strip()
            if line:
                parts.append(line)
            current.clear()
        else:
            for child in n.children:
                _walk(child)

    _walk(node)
    tail = "".join(current).strip()
    if tail:
        parts.append(tail)
    return parts


def _extract_objective_names_from_html(html: str) -> list[str]:
    """
    Extract the objective names listed under the 'WHAT WE COVER' section.

    Handles three HTML patterns seen across workshop docs:
    - Separate <p> per objective (Workshop 5 / new format)
    - Single <p> with <br>-separated items (Workshop 1 & 2 / old format)
    - Separate <p> per objective before an empty <p> separator (Workshop 3 & 4)

    Stops at: h1/h2/h3, empty <p>, or a paragraph longer than _MAX_OBJECTIVE_LEN chars.
    """
    builder = _DOMBuilder()
    builder.feed(html)
    root = _dom_find(builder.root, "body") or builder.root
    children = [c for c in _top_children(root) if isinstance(c, _HN)]

    names: list[str] = []
    collecting = False

    for node in children:
        text = _get_text(node).strip()

        # Detect the 'WHAT WE COVER' marker
        if re.search(r"WHAT\s+WE\s+COVER", text, re.IGNORECASE):
            collecting = True
            # Strip the marker; remaining text may contain inline items
            remainder = re.sub(r".*WHAT\s+WE\s+COVER\s*", "", text, flags=re.IGNORECASE).strip()
            if remainder:
                for line in remainder.splitlines():
                    line = line.strip()
                    if line and not line.startswith("[") and len(line) <= _MAX_OBJECTIVE_LEN:
                        names.append(line)
            continue

        if not collecting:
            continue

        # Stop at any heading
        if node.tag in ("h1", "h2", "h3"):
            break

        if node.tag == "p":
            # Empty paragraph = end of the objective list (Workshop 3 & 4 pattern)
            if not text:
                if names:  # only stop once we've collected at least one
                    break
                continue

            # Use <br>-aware line splitting (Workshop 1 & 2 have all items in one <p>)
            lines = _get_text_lines(node)
            if not lines:
                lines = [text]  # fallback: treat the whole paragraph as one line

            for line in lines:
                line = line.strip()
                if (
                    line
                    and not line.startswith("[")
                    and not re.search(r"WHAT\s+WE\s+COVER", line, re.IGNORECASE)
                    and len(line) <= _MAX_OBJECTIVE_LEN
                ):
                    names.append(line)

    return names


# ── DOM helpers ───────────────────────────────────────────────────────────────


def _get_text(node: _HN | str) -> str:
    """Recursively collect plain text from a DOM node."""
    if isinstance(node, str):
        return node
    return "".join(_get_text(c) for c in node.children)


def _find_first(node: _HN, tag: str) -> _HN | None:
    """BFS for first child element with the given tag."""
    for child in node.children:
        if isinstance(child, _HN):
            if child.tag == tag:
                return child
            found = _find_first(child, tag)
            if found:
                return found
    return None


def _top_children(node: _HN) -> list[_HN | str]:
    """Direct children of an _HN node."""
    return node.children  # type: ignore[return-value]


# ── Resource-card detection & conversion ─────────────────────────────────────


def _is_resource_card_table(table: _HN) -> bool:
    """True when a <table> is a CMM resource card (▶ arrow + 'Resource:' present)."""
    text = _get_text(table)
    return "\u27a4" in text and "Resource:" in text


def _parse_resource_card(table: _HN) -> dict | None:
    """
    Extract structured data from a resource card table.
    Returns dict with keys: title, url, link_text, resource_type, description.
    Returns None if the table cannot be parsed.
    """
    td = _find_first(table, "td")
    if not td:
        return None

    # Collect direct <p> children of the <td>
    paras = [c for c in _top_children(td) if isinstance(c, _HN) and c.tag == "p"]
    if not paras:
        return None

    # Paragraph 0: "▶ [Bold title]"
    raw_title = _get_text(paras[0]).strip()
    title = re.sub(r"^[\u27a4\s►▶\xa0]+", "", raw_title).strip()

    # Paragraph 1: "Resource: <a>Link</a> | Type"
    url = ""
    link_text = ""
    resource_type = ""
    if len(paras) >= 2:
        a_tag = _find_first(paras[1], "a")
        if a_tag:
            url = a_tag.attrs.get("href", "").strip()
            link_text = _get_text(a_tag).strip()
        full = _get_text(paras[1])
        m = re.search(r"[|│]\s*(.+)$", full.strip())
        if m:
            resource_type = m.group(1).strip()

    # Paragraph 2+: description
    description = " ".join(_get_text(p).strip() for p in paras[2:]).strip()

    if not title and not url:
        return None

    return {
        "title": title,
        "url": url,
        "link_text": link_text or title,
        "resource_type": resource_type,
        "description": description,
    }


def _resource_card_to_html(card: dict) -> str:
    """Render a parsed resource card as a self-contained styled HTML div."""
    title = card["title"]
    url = card["url"]
    link_text = card["link_text"]
    resource_type = card.get("resource_type", "")
    description = card.get("description", "")

    type_badge = (
        f'<span style="color:{_TEXT}; font-style:italic;">'
        f" &nbsp;&#9474;&nbsp; {resource_type}</span>"
        if resource_type
        else ""
    )
    desc_block = (
        f'<div style="font-size:0.9rem; color:{_TEXT}; line-height:1.55; margin-top:4px;">'
        f"{description}</div>"
        if description
        else ""
    )

    return (
        f'<div style="background-color:{_CARD_BG}; border:1px solid {_GREEN}; '
        f"border-radius:6px; padding:16px 20px; margin:12px 0;\">"
        f'<div style="display:flex; gap:10px; align-items:flex-start;">'
        f'<span style="color:{_TEAL}; font-size:1.1rem; flex-shrink:0; padding-top:1px;">&#10148;</span>'
        f'<div style="flex:1;">'
        f'<div style="font-weight:600; color:{_DARK}; margin-bottom:6px;">{title}</div>'
        f'<div style="font-size:0.875rem; margin-bottom:2px;">'
        f'<span style="color:{_TEXT}; font-style:italic;">Resource: </span>'
        f'<a href="{url}" style="color:{_TEAL}; text-decoration:underline;">{link_text}</a>'
        f"{type_badge}</div>"
        f"{desc_block}"
        f"</div>"
        f"</div>"
        f"</div>"
    )


# ── Body Tiptap builder ───────────────────────────────────────────────────────


def _build_body_tiptap(body_html: str) -> dict:
    """
    Convert body HTML to a Tiptap JSON document.

    Resource card tables → rawHtml nodes (styled cards).
    Everything else → native Tiptap nodes via _html_to_tiptap.
    """
    builder = _DOMBuilder()
    builder.feed(body_html)
    root = _dom_find(builder.root, "body") or builder.root

    all_nodes: list[dict] = []
    html_buffer: list[str] = []

    def _flush() -> None:
        if not html_buffer:
            return
        chunk = "".join(html_buffer)
        html_buffer.clear()
        doc = _html_to_tiptap(chunk)
        all_nodes.extend(doc.get("content", []))

    for child in _top_children(root):
        if isinstance(child, _HN):
            if child.tag == "table" and _is_resource_card_table(child):
                _flush()
                card = _parse_resource_card(child)
                if card:
                    all_nodes.append(
                        {"type": "rawHtml", "attrs": {"html": _resource_card_to_html(card)}}
                    )
                else:
                    # Can't parse — fall back to serialised table HTML
                    html_buffer.append(_serialize_node(child))
            else:
                html_buffer.append(_serialize_node(child))
        elif isinstance(child, str):
            html_buffer.append(child)

    _flush()

    return {"type": "doc", "content": all_nodes or [{"type": "paragraph"}]}


# ── LLM metadata extraction ───────────────────────────────────────────────────

_WORKSHOP_LLM_SYSTEM = (
    "You are a content processing assistant for College Money Method (CMM), "
    "an educational platform helping families navigate college financial aid. "
    "Extract structured metadata from a Google Docs workshop detail page. "
    "Return valid JSON only — no markdown fences, no extra text."
)

_WORKSHOP_LLM_PROMPT = """\
Extract structured metadata from this Google Docs workshop detail page HTML.

## Output JSON Schema
{
  "name": "string — the workshop title (clean, strip bracketed editor notes like [Workshop 3 details page])",
  "description": "string | null — 1–3 sentence overview paragraph immediately below the title",
  "suggested_grades": "string | null — grade level, e.g. '9th grade', '10th grade', '11th/12th grade'",
  "action_items": ["string"] — bullet items from the 'Key Actions and Insights' or similar section,
  "objective_names": ["string"] — short section titles listed under 'WHAT WE COVER' (3–6 words each, not descriptions)
}

## Rules
- `name`: the main workshop title. Strip any bracketed editor notes (e.g. "[Workshop 5 details page]").
- `description`: the descriptive paragraph immediately below the title. Do not include the grade/length line.
- `suggested_grades`: extract from patterns like "[11th grade]", "[11th/12th grade]", "11th grade", etc. Return null if absent.
- `action_items`: bullets from "Key Actions and Insights" / "Key Takeaways" / "YOUR KEY TAKEAWAYS". These are short imperative or descriptive bullets (1 sentence each).
- `objective_names`: ONLY the short section headings listed under "WHAT WE COVER" — typically 3–6 words each. Do NOT include the longer description paragraphs or sub-section content.

## HTML
{HTML}
"""


def _call_llm_workshop(provider: str, model: str, html: str) -> dict[str, Any]:
    """Call the configured LLM provider to extract workshop metadata."""
    # Strip to plain text of body for token efficiency
    clean = _sanitize_html(html)
    prompt = _WORKSHOP_LLM_PROMPT.replace("{HTML}", clean)

    if provider == "openai":
        api_key = os.getenv("OPENAI_API_KEY", "").strip()
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY is required when provider=openai")

        schema = {
            "name": "workshop_metadata",
            "strict": True,
            "schema": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "description": {"type": ["string", "null"]},
                    "suggested_grades": {"type": ["string", "null"]},
                    "action_items": {"type": "array", "items": {"type": "string"}},
                    "objective_names": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["name", "description", "suggested_grades", "action_items", "objective_names"],
                "additionalProperties": False,
            },
        }
        resp = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": model,
                "temperature": 0,
                "messages": [
                    {"role": "system", "content": _WORKSHOP_LLM_SYSTEM},
                    {"role": "user", "content": prompt},
                ],
                "response_format": {"type": "json_schema", "json_schema": schema},
            },
            timeout=60,
        )
        resp.raise_for_status()
        return _extract_json_object(resp.json()["choices"][0]["message"]["content"])

    elif provider == "claude":
        api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
        if not api_key:
            raise RuntimeError("ANTHROPIC_API_KEY is required when provider=claude")

        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "max_tokens": 2048,
                "temperature": 0,
                "system": _WORKSHOP_LLM_SYSTEM,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=60,
        )
        resp.raise_for_status()
        blocks = [b.get("text", "") for b in resp.json().get("content", []) if b.get("type") == "text"]
        return _extract_json_object("\n".join(blocks))

    else:
        raise ValueError(f"Unsupported provider: {provider!r}")


# ── Top-level HTML → WorkshopPayload ─────────────────────────────────────────


def _parse_workshop_html(
    raw_html: str,
    row: WorkshopImportRow,
    provider: str = "none",
    model: str = "gpt-4o-mini",
) -> WorkshopPayload:
    """
    Parse a Google Docs workshop HTML export into a WorkshopPayload.

    When provider != 'none', uses an LLM to reliably extract metadata regardless
    of document styling changes. Falls back to basic heuristics when provider='none'.
    """
    html = _clean_google_export_html(raw_html)

    if provider != "none":
        parsed = _call_llm_workshop(provider=provider, model=model, html=html)
        name = row.name or parsed.get("name") or "Untitled Workshop"
        description = row.description or parsed.get("description")
        suggested_grades = row.suggested_grades or parsed.get("suggested_grades")
        action_items: list[str] = parsed.get("action_items") or []
        objective_names: list[str] = parsed.get("objective_names") or []

        # Build key_actions Tiptap JSON from action_items
        key_actions_tiptap: str | None = None
        if action_items:
            items_html = "<ul>" + "".join(f"<li>{item}</li>" for item in action_items) + "</ul>"
            key_actions_tiptap = json.dumps(_html_to_tiptap(items_html))

        return WorkshopPayload(
            name=name,
            description=description,
            suggested_grades=suggested_grades,
            key_actions=key_actions_tiptap,
            body=None,  # objectives rendering replaces body
            action_items=action_items,
            objective_names=objective_names,
        )

    # ── Heuristic fallback (provider='none') ─────────────────────────────────
    # Used for dry-runs or when no LLM key is available.
    # Kept intentionally simple — just grab the first non-bracketed <p> as name.
    builder = _DOMBuilder()
    builder.feed(html)
    root = _dom_find(builder.root, "body") or builder.root
    nodes = [c for c in _top_children(root) if isinstance(c, _HN)]

    name = row.name
    for node in nodes:
        t = _get_text(node).strip()
        if t and not t.startswith("["):
            name = name or t
            break

    return WorkshopPayload(
        name=name or "Untitled Workshop",
        description=row.description,
        suggested_grades=row.suggested_grades,
        key_actions=None,
        body=None,
        action_items=[],
        objective_names=_extract_objective_names_from_html(html),
    )





# ── Input loading ─────────────────────────────────────────────────────────────


def _clean(value: Any) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    return s or None


def _parse_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(str(value).strip())
    except ValueError:
        return None


def _load_rows(input_path: Path) -> list[WorkshopImportRow]:
    if not input_path.exists():
        raise FileNotFoundError(f"Input file not found: {input_path}")

    raw_rows: list[dict[str, Any]]
    if input_path.suffix.lower() == ".csv":
        with input_path.open("r", encoding="utf-8-sig", newline="") as f:
            raw_rows = [dict(row) for row in csv.DictReader(f)]
    elif input_path.suffix.lower() == ".json":
        with input_path.open("r", encoding="utf-8") as f:
            payload = json.load(f)
        if not isinstance(payload, list):
            raise ValueError("JSON input must be an array of objects")
        raw_rows = [dict(item) for item in payload if isinstance(item, dict)]
    else:
        raise ValueError("Input must be .csv or .json")

    result: list[WorkshopImportRow] = []
    for idx, raw in enumerate(raw_rows, start=2):
        source = _clean(
            raw.get("source")
            or raw.get("file_path")
            or raw.get("google_doc_url")
            or raw.get("url")
        )
        if not source:
            raise ValueError(f"Row {idx}: missing 'source' column")

        result.append(
            WorkshopImportRow(
                row_number=idx,
                source=source,
                workshop_id=_clean(raw.get("workshop_id")),
                name=_clean(raw.get("name")),
                description=_clean(raw.get("description")),
                sequence_number=_parse_int(raw.get("sequence_number")),
                suggested_grades=_clean(raw.get("suggested_grades")),
                resource_center_slug=_clean(raw.get("resource_center_slug")),
                workshop_art_url=_clean(raw.get("workshop_art_url")),
            )
        )
    return result


# ── Search text ───────────────────────────────────────────────────────────────


def _tiptap_to_text(tiptap_json_str: str | None) -> str:
    """Extract plain text from a Tiptap JSON string (best-effort)."""
    if not tiptap_json_str:
        return ""
    try:
        doc = json.loads(tiptap_json_str)

        def _walk(node: dict) -> str:
            if node.get("type") == "text":
                return node.get("text", "")
            if node.get("type") == "rawHtml":
                return _strip_html(node.get("attrs", {}).get("html", ""))
            return " ".join(
                _walk(c) for c in node.get("content", []) if isinstance(c, dict)
            )

        return _walk(doc)
    except Exception:  # noqa: BLE001
        return _strip_html(tiptap_json_str)


def _compute_search_text(payload: WorkshopPayload) -> str:
    parts = [
        payload.name,
        payload.description or "",
        _tiptap_to_text(payload.body),
        _tiptap_to_text(payload.key_actions),
        " ".join(payload.action_items),
    ]
    return re.sub(r"\s+", " ", " ".join(parts)).strip()


# ── Database write ────────────────────────────────────────────────────────────


def _resolve_objective_ids(
    conn: Any,
    objective_names: list[str],
) -> list[str]:
    """
    Look up objective UUIDs by fuzzy (case-insensitive substring) name match.
    Returns a list of UUID strings for matched objectives, preserving order.
    Warns for names that have no match.
    """
    ids: list[str] = []
    for name in objective_names:
        row = conn.execute(
            text(
                "SELECT id FROM objectives "
                "WHERE LOWER(name) LIKE :pat "
                "ORDER BY name "
                "LIMIT 1"
            ),
            {"pat": f"%{name.lower()}%"},
        ).fetchone()
        if row:
            ids.append(str(row[0]))
            print(f"  [objective] matched '{name}' → {row[0]}")
        else:
            print(f"  [WARN] objective not found: '{name}'")
    return ids


def _upsert_workshop(
    conn: Any,
    row: WorkshopImportRow,
    payload: WorkshopPayload,
    create_missing: bool,
    overwrite: bool,
    dry_run: bool,
) -> tuple[str, str]:
    """
    Write a workshop to the database.

    Lookup precedence: workshop_id → sequence_number.
    Returns (operation, identifier) where operation is one of:
      "inserted", "updated", "skipped"
    """
    existing = None

    if row.workshop_id:
        existing = conn.execute(
            text("SELECT id, name, sequence_number FROM workshops WHERE id = :id LIMIT 1"),
            {"id": row.workshop_id},
        ).fetchone()
    elif row.sequence_number is not None:
        existing = conn.execute(
            text(
                "SELECT id, name, sequence_number FROM workshops "
                "WHERE sequence_number = :seq LIMIT 1"
            ),
            {"seq": row.sequence_number},
        ).fetchone()

    label = row.name or payload.name

    if existing:
        wid = str(existing[0])
        if not overwrite:
            return "skipped", f"id={wid}"

        if dry_run:
            return "updated", f"id={wid}"

        search_text = _compute_search_text(payload)

        conn.execute(
            text(
                """
                UPDATE workshops
                SET
                    name             = :name,
                    description      = :description,
                    key_actions      = :key_actions,
                    body             = :body,
                    suggested_grades = :suggested_grades,
                    sequence_number  = :sequence_number,
                    resource_center_slug = :resource_center_slug,
                    workshop_art_url = :workshop_art_url,
                    action_items     = CAST(:action_items AS jsonb),
                    search_text      = :search_text
                WHERE id = :id
                """
            ),
            {
                "id": wid,
                "name": payload.name,
                "description": payload.description,
                "key_actions": payload.key_actions,
                "body": payload.body,
                "suggested_grades": payload.suggested_grades,
                "sequence_number": row.sequence_number if row.sequence_number is not None else existing[2],
                "resource_center_slug": row.resource_center_slug,
                "workshop_art_url": row.workshop_art_url,
                "action_items": json.dumps(payload.action_items),
                "search_text": search_text,
            },
        )

        # Link objectives
        if payload.objective_names:
            obj_ids = _resolve_objective_ids(conn, payload.objective_names)
            if obj_ids:
                conn.execute(
                    text("DELETE FROM objective_workshops WHERE workshop_id = :wid"),
                    {"wid": wid},
                )
                for oid in obj_ids:
                    conn.execute(
                        text(
                            "INSERT INTO objective_workshops (objective_id, workshop_id) "
                            "VALUES (:oid, :wid) ON CONFLICT DO NOTHING"
                        ),
                        {"oid": oid, "wid": wid},
                    )

        return "updated", f"id={wid}"

    if not create_missing:
        lookup = row.workshop_id or (
            f"sequence_number={row.sequence_number}" if row.sequence_number else "(none)"
        )
        raise RuntimeError(
            f"Workshop not found for row {row.row_number} ({lookup}). "
            "Use --create-missing to insert."
        )

    if dry_run:
        return "inserted", f"name={label!r}"

    new_id = str(uuid.uuid4())
    search_text = _compute_search_text(payload)

    conn.execute(
        text(
            """
            INSERT INTO workshops (
                id, name, description, key_actions, body,
                suggested_grades, sequence_number,
                resource_center_slug, workshop_art_url,
                action_items, search_text
            ) VALUES (
                :id, :name, :description, :key_actions, :body,
                :suggested_grades, :sequence_number,
                :resource_center_slug, :workshop_art_url,
                CAST(:action_items AS jsonb), :search_text
            )
            """
        ),
        {
            "id": new_id,
            "name": payload.name,
            "description": payload.description,
            "key_actions": payload.key_actions,
            "body": payload.body,
            "suggested_grades": payload.suggested_grades,
            "sequence_number": row.sequence_number,
            "resource_center_slug": row.resource_center_slug,
            "workshop_art_url": row.workshop_art_url,
            "action_items": json.dumps(payload.action_items),
            "search_text": search_text,
        },
    )

    # Link objectives
    if payload.objective_names:
        obj_ids = _resolve_objective_ids(conn, payload.objective_names)
        for oid in obj_ids:
            conn.execute(
                text(
                    "INSERT INTO objective_workshops (objective_id, workshop_id) "
                    "VALUES (:oid, :wid) ON CONFLICT DO NOTHING"
                ),
                {"oid": oid, "wid": new_id},
            )

    return "inserted", f"id={new_id}"


# ── Main ──────────────────────────────────────────────────────────────────────


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Batch import Google Docs workshop HTML exports into the workshops table"
    )
    parser.add_argument("--input", required=True, help="Path to CSV or JSON input file")
    parser.add_argument(
        "--provider",
        default="none",
        choices=["none", "openai", "claude"],
        help="LLM provider for metadata extraction (default: none — heuristic fallback)",
    )
    parser.add_argument(
        "--model",
        default="gpt-4o-mini",
        help="LLM model name (default: gpt-4o-mini)",
    )
    parser.add_argument(
        "--create-missing",
        action="store_true",
        help="Insert workshops that do not yet exist",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite existing workshop content when a match is found (default: skip existing)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse and display changes without writing to the database",
    )
    args = parser.parse_args()

    input_path = Path(args.input).resolve()
    rows = _load_rows(input_path)

    _sep = "─" * 60
    print(_sep)
    print("  CMM Workshop Import")
    print(f"  Input : {input_path}")
    print(f"  Rows  : {len(rows)}")
    if args.dry_run:
        print("  Mode  : DRY RUN — no database writes")
    print(f"  LLM   : {args.provider}" + (f" / {args.model}" if args.provider != "none" else " (heuristic)"))
    if args.create_missing:
        print("  Mode  : --create-missing enabled")
    if args.overwrite:
        print("  Mode  : --overwrite enabled (existing workshops will be overwritten)")
    else:
        print("  Mode  : existing workshops will be skipped (use --overwrite to update)")
    print(_sep)

    inserted = updated = skipped = failed = 0

    with get_engine().begin() as conn:
        for row in rows:
            try:
                print(f"\n{'━' * 60}")
                print(f"  Row {row.row_number}/{len(rows)}  {row.source}")
                print(f"{'━' * 60}")

                # 1. Load HTML
                print("  [1/3] Loading source HTML …", end="", flush=True)
                raw_html, _ = _load_source(row.source)
                print(f" done  ({len(raw_html):,} chars)")

                # 2. Parse to WorkshopPayload
                print("  [2/3] Parsing workshop content …", end="", flush=True)
                payload = _parse_workshop_html(raw_html, row, provider=args.provider, model=args.model)

                body_blocks = (
                    len(json.loads(payload.body).get("content", []))
                    if payload.body
                    else 0
                )
                raw_blocks = (
                    sum(
                        1
                        for b in json.loads(payload.body).get("content", [])
                        if b.get("type") == "rawHtml"
                    )
                    if payload.body
                    else 0
                )
                print(
                    f" done\n"
                    f"       name         : {payload.name!r}\n"
                    f"       grades       : {payload.suggested_grades or '—'}\n"
                    f"       action_items : {len(payload.action_items)}\n"
                    f"       objectives   : {len(payload.objective_names)} found in HTML\n"
                    f"       body blocks  : {body_blocks} total, {raw_blocks} resource cards"
                )

                # 3. Write to database
                print("  [3/3] Writing to database …", end="", flush=True)
                operation, identifier = _upsert_workshop(
                    conn=conn,
                    row=row,
                    payload=payload,
                    create_missing=args.create_missing,
                    overwrite=args.overwrite,
                    dry_run=args.dry_run,
                )
                if operation == "inserted":
                    inserted += 1
                elif operation == "updated":
                    updated += 1
                else:
                    skipped += 1
                print(f" done  [{operation}] {identifier}")

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
