"""
One-time migration: split workshop.key_actions into workshop.key_action_items.

Handles two source formats:
  • HTML (<ul><li>, <p>, headings) — each block element becomes one TipTap doc item
  • TipTap JSON doc              — each top-level content node becomes one item

Inline formatting (bold, italic, links) is preserved where possible.
Workshops that already have key_action_items are skipped unless --overwrite is passed.

Usage:
    python -m scripts.migrate_key_actions_to_key_action_items           # skip existing
    python -m scripts.migrate_key_actions_to_key_action_items --dry-run # preview only
    python -m scripts.migrate_key_actions_to_key_action_items --overwrite
"""

from __future__ import annotations

import json
import os
import sys
from html.parser import HTMLParser

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import src.assets.models  # noqa: F401
import src.auth.models  # noqa: F401
import src.calendar.models  # noqa: F401
import src.content.models  # noqa: F401
import src.cycles.models  # noqa: F401
import src.meetings.models  # noqa: F401
import src.sales.models  # noqa: F401
import src.schools.models  # noqa: F401
import src.settings.models  # noqa: F401
import src.workshops.models  # noqa: F401
import src.guest_contacts.models  # noqa: F401

from sqlalchemy import select
from src.workshops.models import Workshop
from src.db.base import get_session_factory

# ── HTML → TipTap paragraph converter ────────────────────────────────────────

_BLOCK_TAGS = {"p", "li", "h1", "h2", "h3", "h4", "h5", "h6", "dt", "dd"}
_MARK_MAP = {"strong": "bold", "b": "bold", "em": "italic", "i": "italic", "u": "underline"}


class _HtmlSplitter(HTMLParser):
    """Split an HTML string into a list of TipTap paragraph JSON strings."""

    def __init__(self):
        super().__init__()
        self.items: list[str] = []
        self._in_block = False
        self._marks: list[str] = []
        self._link_href: str | None = None
        self._nodes: list[dict] = []

    def handle_starttag(self, tag: str, attrs: list[tuple]) -> None:
        tag = tag.lower()
        if tag in _BLOCK_TAGS:
            self._nodes = []
            self._marks = []
            self._link_href = None
            self._in_block = True
        elif tag == "a":
            href = dict(attrs).get("href", "")
            self._link_href = href
            self._marks.append("link")
        elif tag in _MARK_MAP and _MARK_MAP[tag] not in self._marks:
            self._marks.append(_MARK_MAP[tag])

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in _BLOCK_TAGS:
            self._flush_block()
            self._in_block = False
        elif tag == "a":
            if "link" in self._marks:
                self._marks.remove("link")
            self._link_href = None
        elif tag in _MARK_MAP:
            mark = _MARK_MAP[tag]
            if mark in self._marks:
                self._marks.remove(mark)

    def handle_data(self, data: str) -> None:
        if not self._in_block or not data.strip():
            return
        node: dict = {"type": "text", "text": data}
        marks = self._build_marks()
        if marks:
            node["marks"] = marks
        self._nodes.append(node)

    def _build_marks(self) -> list[dict]:
        result = []
        for m in self._marks:
            if m == "link" and self._link_href:
                result.append({"type": "link", "attrs": {"href": self._link_href, "target": "_blank"}})
            elif m in ("bold", "italic", "underline"):
                result.append({"type": m})
        return result

    def _flush_block(self) -> None:
        if not self._nodes:
            return
        doc = {"type": "doc", "content": [{"type": "paragraph", "content": self._nodes}]}
        self.items.append(json.dumps(doc, ensure_ascii=False))
        self._nodes = []


def _split_html(html: str) -> list[str]:
    parser = _HtmlSplitter()
    parser.feed(html)
    return parser.items


# ── TipTap JSON splitter ──────────────────────────────────────────────────────

def _split_tiptap(raw: str) -> list[str]:
    """Split a TipTap JSON doc into per-item doc strings.

    bulletList/orderedList nodes are expanded: each listItem becomes
    its own doc so every takeaway ends up as a separate entry.
    """
    doc = json.loads(raw)
    nodes = doc.get("content") or []
    items: list[str] = []
    for node in nodes:
        if node.get("type") in ("bulletList", "orderedList"):
            for list_item in (node.get("content") or []):
                inner = list_item.get("content") or []
                if inner:
                    items.append(json.dumps({"type": "doc", "content": inner}, ensure_ascii=False))
        else:
            items.append(json.dumps({"type": "doc", "content": [node]}, ensure_ascii=False))
    return items


# ── Main splitter ─────────────────────────────────────────────────────────────

def split_key_actions(raw: str) -> list[str]:
    if not raw or not raw.strip():
        return []

    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        parsed = None

    if isinstance(parsed, list):
        return [item if isinstance(item, str) else json.dumps(item) for item in parsed if item]

    if isinstance(parsed, dict) and parsed.get("type") == "doc":
        items = _split_tiptap(raw)
        return items if items else [raw]

    items = _split_html(raw)
    return items if items else [raw]


# ── Entry point ───────────────────────────────────────────────────────────────

def main(dry_run: bool = False, overwrite: bool = False) -> None:
    Session = get_session_factory()

    with Session() as session:
        workshops = session.scalars(
            select(Workshop).where(Workshop.key_actions.isnot(None)).order_by(Workshop.name)
        ).all()

        print(f"Workshops with key_actions: {len(workshops)}\n")
        updated = skipped = 0

        for workshop in workshops:
            if workshop.key_action_items and not overwrite:
                print(f"  SKIP  {workshop.name[:55]!r:60} {len(workshop.key_action_items)} items already")
                skipped += 1
                continue

            items = split_key_actions(workshop.key_actions)
            if not items:
                print(f"  EMPTY {workshop.name[:55]!r:60} nothing extracted")
                skipped += 1
                continue

            tag = "DRY " if dry_run else "OK  "
            print(f"  {tag}  {workshop.name[:55]!r:60} → {len(items)} item(s)")
            if not dry_run:
                workshop.key_action_items = items
            updated += 1

        if not dry_run:
            session.commit()
            print(f"\nDone — updated {updated}, skipped {skipped}")
        else:
            print(f"\nDry run — would update {updated}, skip {skipped}")


if __name__ == "__main__":
    args = set(sys.argv[1:])
    main(dry_run="--dry-run" in args, overwrite="--overwrite" in args)
