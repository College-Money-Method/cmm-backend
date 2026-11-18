"""Turn the Counselor Hub user guide (Markdown) into a Tiptap document.

The assistant splits a resource at its Tiptap heading nodes, one chunk per idea,
so the guide's structure has to survive as real headings rather than "## " text:

* ``## Page`` becomes a level-2 heading; ``### Task: Do X`` becomes a level-3
  heading named "Page: Do X", so a chunk says which page it is about.
* Tables become bullet lists of "first cell: Column value; Column value" lines,
  because the indexer reads table cells one per line and loses their labels.
* ``**bold**`` stays bold (it marks exact UI labels); backticks and link
  syntax are dropped to plain text. ``# Title`` and ``---`` rules are skipped.

Only nodes the admin editor's StarterKit understands are produced, so the
resource stays editable in Admin -> Content.
"""

from __future__ import annotations

import re

INTRO_HEADING = "About the Counselor Hub"

_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_LIST_RE = re.compile(r"^\s*(?:[-*]|(\d+)\.)\s+(.*)$")


def _inline(text: str) -> list[dict]:
    """Text nodes for one line: bold kept as a mark, code and links as plain text."""
    text = _LINK_RE.sub(r"\1", text).replace("`", "").strip()
    nodes = []
    for i, part in enumerate(_BOLD_RE.split(text)):
        if part:
            node = {"type": "text", "text": part}
            if i % 2:
                node["marks"] = [{"type": "bold"}]
            nodes.append(node)
    return nodes


def _paragraph(text: str) -> dict:
    content = _inline(text)
    return {"type": "paragraph", "content": content} if content else {"type": "paragraph"}


def _heading(level: int, text: str) -> dict:
    return {"type": "heading", "attrs": {"level": level}, "content": _inline(text)}


def _list(items: list[str], ordered: bool) -> dict:
    return {
        "type": "orderedList" if ordered else "bulletList",
        "content": [{"type": "listItem", "content": [_paragraph(item)]} for item in items],
    }


def _cells(row: str) -> list[str]:
    return [cell.strip() for cell in row.strip().strip("|").split("|")]


def table_lines(rows: list[str]) -> list[str]:
    """One "first cell: Column value; ..." line per body row of a Markdown table."""
    header, body = _cells(rows[0]), [r for r in rows[2:] if r.strip()]
    lines = []
    for row in body:
        cells = _cells(row)
        if len(header) == 2:
            rest = cells[1] if len(cells) > 1 else ""
        else:
            rest = "; ".join(f"{name}: {value}" for name, value in zip(header[1:], cells[1:], strict=False))
        lines.append(f"{cells[0]}: {rest}")
    return lines


def markdown_to_tiptap(markdown: str) -> dict:
    """The guide as a Tiptap ``doc``; see the module docstring for the rules."""
    blocks: list[dict] = []
    page: str | None = None
    lines = markdown.splitlines()
    i = 0

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if not stripped or stripped == "---":
            i += 1
        elif stripped.startswith("# "):
            # The resource's name is the title; the intro gets a heading of its own.
            blocks.append(_heading(2, INTRO_HEADING))
            i += 1
        elif stripped.startswith("## "):
            page = stripped[3:].strip()
            blocks.append(_heading(2, page))
            i += 1
        elif stripped.startswith("### "):
            sub = re.sub(r"^Task:\s*", "", stripped[4:].strip())
            blocks.append(_heading(3, f"{page}: {sub}" if page else sub))
            i += 1
        elif stripped.startswith("|"):
            rows = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                rows.append(lines[i])
                i += 1
            blocks.append(_list(table_lines(rows), ordered=False))
        elif _LIST_RE.match(line):
            ordered = _LIST_RE.match(line).group(1) is not None
            items = []
            while i < len(lines) and (match := _LIST_RE.match(lines[i])):
                items.append(match.group(2))
                i += 1
            blocks.append(_list(items, ordered))
        else:
            # Consecutive plain lines form one paragraph.
            parts = []
            while i < len(lines) and lines[i].strip() and not _starts_block(lines[i]):
                parts.append(lines[i].strip())
                i += 1
            blocks.append(_paragraph(" ".join(parts)))

    return {"type": "doc", "content": blocks}


def _starts_block(line: str) -> bool:
    stripped = line.strip()
    return stripped == "---" or stripped.startswith(("#", "|")) or bool(_LIST_RE.match(line))
