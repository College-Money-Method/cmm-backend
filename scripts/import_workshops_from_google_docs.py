#!/usr/bin/env python3
"""Batch import Google Docs workshop detail pages into the workshops table.

Parses HTML exported from Google Docs ("Web page (.html)") and writes structured
content into `workshops.key_actions` and `workshops.body` as Tiptap JSON.

Resource card tables (▶ Title / Resource: link / description) are converted to
styled rawHtml blocks. Once a dedicated Tiptap `resourceCard` extension is built,
re-run with --overwrite to migrate them to structured nodes.

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
      --input scripts/input/workshops.csv --dry-run

  uv run python scripts/import_workshops_from_google_docs.py \\
      --input scripts/input/workshops.csv --create-missing

  uv run python scripts/import_workshops_from_google_docs.py \\
      --input scripts/input/workshops.csv --overwrite
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

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Reuse parsing infrastructure from the topics import script.
# It has an if __name__ == "__main__" guard so importing is safe.
from scripts.import_topics_from_google_docs import (
    _DOMBuilder,
    _HN,
    _clean_google_export_html,
    _dom_find,
    _html_to_tiptap,
    _load_source,
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
    body: str | None  # Tiptap JSON string
    action_items: list[str]


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


# ── HTML metadata extraction ──────────────────────────────────────────────────


def _split_preamble_and_body(html: str) -> tuple[str, str]:
    """
    Split workshop HTML into preamble (everything before the first <h1>)
    and body (from the first <h1> to end, excluding trailing annotation divs).
    """
    builder = _DOMBuilder()
    builder.feed(html)
    root = _dom_find(builder.root, "body") or builder.root

    preamble: list[str] = []
    body: list[str] = []
    in_body = False

    for child in _top_children(root):
        if isinstance(child, _HN):
            if not in_body and child.tag == "h1":
                in_body = True
            if in_body:
                # Skip Google Docs comment annotation divs ("[a]", "[b]", etc.)
                text = _get_text(child).strip()
                if re.match(r"^\[[a-z0-9]\]", text):
                    continue
                body.append(_serialize_node(child))
            else:
                preamble.append(_serialize_node(child))
        elif isinstance(child, str):
            (body if in_body else preamble).append(child)

    return "".join(preamble), "".join(body)


def _extract_preamble_metadata(
    preamble_html: str, row: WorkshopImportRow
) -> dict[str, Any]:
    """
    Extract name, description, suggested_grades, key_actions (Tiptap JSON),
    and action_items (list[str]) from the preamble HTML.

    Values from the CSV row take precedence over extracted values.
    """
    builder = _DOMBuilder()
    builder.feed(preamble_html)
    root = _dom_find(builder.root, "body") or builder.root

    nodes = [c for c in _top_children(root) if isinstance(c, _HN)]

    # ── Name: first <p class="...title..."> ──────────────────────────────────
    name = row.name
    for node in nodes:
        if "title" in node.attrs.get("class", ""):
            text = _get_text(node).strip()
            # Strip trailing Google Docs annotation markers like [a], [b]
            text = re.sub(r"\[[a-z0-9]\]\s*$", "", text).strip()
            if text and not text.startswith("["):
                name = name or text
                break

    # ── Description: italic/colored <p> immediately after title ─────────────
    description = row.description
    title_seen = False
    for node in nodes:
        if "title" in node.attrs.get("class", ""):
            title_seen = True
            continue
        if title_seen and node.tag == "p":
            text = _get_text(node).strip()
            if text and not text.startswith("["):
                description = description or text
                break

    # ── Suggested grades: "[9th grade]" / "[10th grade]" pattern ────────────
    suggested_grades = row.suggested_grades
    for node in nodes:
        m = re.search(r"\[(\d+(?:th|st|nd|rd)\s+grade)\]", _get_text(node), re.IGNORECASE)
        if m:
            suggested_grades = suggested_grades or m.group(1)
            break

    # ── Key actions: <ul> bullet list in preamble ────────────────────────────
    action_items: list[str] = []
    key_actions_html_parts: list[str] = []

    for node in nodes:
        if node.tag == "ul":
            for li in _top_children(node):
                if isinstance(li, _HN) and li.tag == "li":
                    text = _get_text(li).strip()
                    if text:
                        action_items.append(text)
            key_actions_html_parts.append(_serialize_node(node))

    key_actions_tiptap: str | None = None
    if key_actions_html_parts:
        key_actions_tiptap = json.dumps(_html_to_tiptap("".join(key_actions_html_parts)))

    return {
        "name": name or "Untitled Workshop",
        "description": description,
        "suggested_grades": suggested_grades,
        "key_actions": key_actions_tiptap,
        "action_items": action_items,
    }


# ── Top-level HTML → WorkshopPayload ─────────────────────────────────────────


def _parse_workshop_html(raw_html: str, row: WorkshopImportRow) -> WorkshopPayload:
    """
    Parse a Google Docs workshop HTML export into a WorkshopPayload.
    """
    html = _clean_google_export_html(raw_html)
    preamble_html, body_html = _split_preamble_and_body(html)

    meta = _extract_preamble_metadata(preamble_html, row)

    body_tiptap: str | None = None
    if body_html.strip():
        body_tiptap = json.dumps(_build_body_tiptap(body_html))

    return WorkshopPayload(
        name=meta["name"],
        description=meta["description"],
        suggested_grades=meta["suggested_grades"],
        key_actions=meta["key_actions"],
        body=body_tiptap,
        action_items=meta["action_items"],
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
    return "inserted", f"id={new_id}"


# ── Main ──────────────────────────────────────────────────────────────────────


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Batch import Google Docs workshop HTML exports into the workshops table"
    )
    parser.add_argument("--input", required=True, help="Path to CSV or JSON input file")
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
                payload = _parse_workshop_html(raw_html, row)

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
