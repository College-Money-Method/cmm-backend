"""convert internalLink marks to plain link marks

Retires the frontend `internalLink` Tiptap mark. Rewrites all stored rich
content so internal links become plain `link` marks (and legacy
`<a data-internal data-href="X">` HTML becomes `<a href="X">`). The href
(school-agnostic `/topic/<slug>` or `/resources/<id>`) is preserved; the
optional `label` (topic/resource name, used for the editor tooltip) is kept.

Covers every column that stores Tiptap JSON / rich HTML. Idempotent: rows
without the marker are skipped, and re-running is a no-op.

Revision ID: 0084
Revises: 0083
Create Date: 2026-07-24
"""
from __future__ import annotations

import json
import re
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0084"
down_revision: Union[str, None] = "0083"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# (table, column) pairs that store Tiptap JSON (or legacy rich HTML).
COLUMNS: list[tuple[str, str]] = [
    ("workshop_email_templates", "body"),
    ("communication_templates", "content"),
    ("topics", "content"),
    ("topics", "summary"),
    ("content_assets", "content"),
    ("content_assets", "summary"),
    ("pages", "content"),
    ("workshops", "body"),
]

_A_TAG = re.compile(r"<a\b[^>]*>", re.IGNORECASE)


def _rewrite_anchor_tag(tag: str) -> str:
    """Turn a legacy `<a data-internal data-href="X" …>` opening tag into a
    plain `<a href="X" …>` tag (strip the data-internal/-label/-href attrs)."""
    if "data-internal" not in tag:
        return tag
    out = tag
    # Promote data-href → href when there's no real href yet.
    if not re.search(r"\shref\s*=", out, re.IGNORECASE):
        m = re.search(r'\sdata-href\s*=\s*"([^"]*)"', out, re.IGNORECASE)
        if m:
            out = '<a href="' + m.group(1) + '"' + out[2:]
    # Drop the now-unused data-* attributes.
    out = re.sub(r'\sdata-internal(?:-label)?\s*=\s*"[^"]*"', "", out, flags=re.IGNORECASE)
    out = re.sub(r'\sdata-href\s*=\s*"[^"]*"', "", out, flags=re.IGNORECASE)
    return out


def _rewrite_html(html: str) -> str:
    return _A_TAG.sub(lambda m: _rewrite_anchor_tag(m.group(0)), html)


def _convert_marks(marks: list) -> bool:
    """In-place: internalLink marks → plain link marks. Returns True if changed."""
    changed = False
    for i, mark in enumerate(marks):
        if isinstance(mark, dict) and mark.get("type") == "internalLink":
            attrs = mark.get("attrs") or {}
            new_attrs = {"href": attrs.get("href") or ""}
            if attrs.get("label"):
                new_attrs["label"] = attrs["label"]
            marks[i] = {"type": "link", "attrs": new_attrs}
            changed = True
    return changed


def _walk(node: object) -> bool:
    """Recursively rewrite a Tiptap node tree in place. Returns True if changed."""
    if not isinstance(node, dict):
        return False
    changed = False

    marks = node.get("marks")
    if isinstance(marks, list) and _convert_marks(marks):
        changed = True

    if node.get("type") == "rawHtml":
        attrs = node.get("attrs") or {}
        html = attrs.get("html")
        if isinstance(html, str) and "data-internal" in html:
            new_html = _rewrite_html(html)
            if new_html != html:
                attrs["html"] = new_html
                node["attrs"] = attrs
                changed = True

    content = node.get("content")
    if isinstance(content, list):
        for child in content:
            if _walk(child):
                changed = True

    return changed


def _rewrite_value(value: str) -> str | None:
    """Rewrite one stored column value. Returns the new value, or None if
    unchanged. Handles both Tiptap JSON docs and raw HTML strings."""
    stripped = value.lstrip()
    if stripped.startswith("{"):
        try:
            doc = json.loads(value)
        except (ValueError, TypeError):
            doc = None
        if isinstance(doc, dict):
            if _walk(doc):
                return json.dumps(doc, ensure_ascii=False)
            return None
    # Fallback: treat as a raw HTML string.
    if "data-internal" in value:
        new_html = _rewrite_html(value)
        if new_html != value:
            return new_html
    return None


def upgrade() -> None:
    conn = op.get_bind()
    for table, column in COLUMNS:
        rows = conn.execute(
            sa.text(
                f"SELECT id, {column} AS val FROM {table} "
                f"WHERE {column} IS NOT NULL "
                f"AND ({column} LIKE '%internalLink%' OR {column} LIKE '%data-internal%')"
            )
        ).mappings().all()
        for row in rows:
            new_val = _rewrite_value(row["val"])
            if new_val is not None:
                conn.execute(
                    sa.text(f"UPDATE {table} SET {column} = :v WHERE id = :id"),
                    {"v": new_val, "id": row["id"]},
                )


def downgrade() -> None:
    # Data migration — original internalLink marks are not restorable.
    pass
