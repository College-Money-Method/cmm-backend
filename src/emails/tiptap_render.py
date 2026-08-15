"""Tiptap JSON -> HTML fragment / plain-text walker for the restricted email schema.

Email content is authored only through the merge-tag-aware rich text editor,
which is restricted to: paragraph, heading (1-3), bulletList, orderedList,
listItem, hardBreak, blockquote, text; with bold/italic/underline/strike/code/
link marks. ``node_to_html`` raises on anything outside that set (fail loud):
an unrecognised node type means either schema drift or a node that was never
resolved (e.g. a stray, unresolved mergeTag — callers must run the doc through
``link_resolver.resolve_tiptap_doc`` first).

``node_to_text``/``tiptap_to_plain_text`` are a line-for-line port of
cmm-frontend's ``merge-tag-utils.ts`` (``nodeToText``/``tiptapToPlainText`),
kept permissive to match its generic fallback case — golden-fixture parity
depends on this behaving identically, byte for byte.
"""

from __future__ import annotations

import html as html_lib

_MARK_TAGS = {
    "bold": "strong",
    "italic": "em",
    "underline": "u",
    "strike": "s",
    "code": "code",
}

# Node types the HTML walker accepts — the restricted "email mode" schema.
_SUPPORTED_NODE_TYPES = {
    "doc",
    "paragraph",
    "heading",
    "bulletList",
    "orderedList",
    "listItem",
    "hardBreak",
    "blockquote",
    "text",
}

# Brand inline styles (table-based email shell, see templates/email/base.html).
_BODY_TEXT_STYLE = (
    "font-family: 'Inter', Arial, Helvetica, sans-serif; font-size: 16px; line-height: 1.4; color: #1e3a5f;"
)
_HEADING_STYLE = "font-family: 'Lora', Georgia, serif; font-weight: 500; color: #4f788d; line-height: 1.2;"
_HEADING_SIZES = {1: "28px", 2: "22px", 3: "18px"}
_LIST_STYLE = f"margin: 0 0 16px; padding-left: 20px; {_BODY_TEXT_STYLE}"
_LIST_ITEM_STYLE = "margin: 0 0 4px;"
_BLOCKQUOTE_STYLE = f"margin: 0 0 16px; padding-left: 16px; border-left: 3px solid #b0c8c0; {_BODY_TEXT_STYLE}"
_LINK_STYLE = "color: #6b9d81;"


def _escape(text: str) -> str:
    return html_lib.escape(text, quote=True)


def _text_node_to_html(node: dict) -> str:
    rendered = _escape(node.get("text", ""))
    for mark in node.get("marks", []):
        mark_type = mark.get("type")
        tag = _MARK_TAGS.get(mark_type)
        if tag:
            rendered = f"<{tag}>{rendered}</{tag}>"
        elif mark_type == "link":
            attrs = mark.get("attrs", {})
            href = _escape(str(attrs.get("href", "")))
            target = _escape(str(attrs.get("target", "_blank")))
            rel = _escape(str(attrs.get("rel", "noopener noreferrer nofollow")))
            rendered = f'<a href="{href}" target="{target}" rel="{rel}" style="{_LINK_STYLE}">{rendered}</a>'
    return rendered


def _children_html(node: dict) -> str:
    return "".join(node_to_html(child) for child in node.get("content", []))


def node_to_html(node: dict) -> str:
    """Render one Tiptap node (already merge-tag/link resolved) to an HTML string."""
    node_type = node.get("type")
    if node_type not in _SUPPORTED_NODE_TYPES:
        raise ValueError(f"Unsupported Tiptap node type for email rendering: {node_type!r}")

    if node_type == "text":
        return _text_node_to_html(node)
    if node_type == "hardBreak":
        return "<br/>"
    if node_type == "paragraph":
        return f'<p style="margin: 0 0 16px; {_BODY_TEXT_STYLE}">{_children_html(node)}</p>'
    if node_type == "heading":
        level = node.get("attrs", {}).get("level") or 2
        size = _HEADING_SIZES.get(level, "18px")
        return f'<h{level} style="margin: 0 0 16px; font-size: {size}; {_HEADING_STYLE}">{_children_html(node)}</h{level}>'
    if node_type == "blockquote":
        return f'<blockquote style="{_BLOCKQUOTE_STYLE}">{_children_html(node)}</blockquote>'
    if node_type == "bulletList":
        return f'<ul style="{_LIST_STYLE}">{_children_html(node)}</ul>'
    if node_type == "orderedList":
        return f'<ol style="{_LIST_STYLE}">{_children_html(node)}</ol>'
    if node_type == "listItem":
        return f'<li style="{_LIST_ITEM_STYLE}">{_children_html(node)}</li>'
    # "doc" — the only remaining member of _SUPPORTED_NODE_TYPES.
    return _children_html(node)


def render_doc_to_html(doc: dict) -> str:
    """Render a resolved Tiptap doc into an HTML fragment (no shell/table)."""
    return node_to_html(doc) if doc.get("type") == "doc" else _children_html(doc)


# ── Plain-text extraction (ports merge-tag-utils.ts nodeToText/tiptapToPlainText) ──


def node_to_text(node: dict) -> str:
    node_type = node.get("type")

    if node_type == "text":
        text = node.get("text", "")
        # Preserve the URL for linked text (e.g. resource names) so it survives
        # a plain-text copy. Bare-URL links (text == href) stay as-is.
        href = next(
            (m.get("attrs", {}).get("href") for m in node.get("marks", []) if m.get("type") == "link"),
            None,
        )
        return f"{text} ({href})" if href and href != text else text

    if node_type == "mergeTag":
        return f"{{{{{node.get('attrs', {}).get('tag', '')}}}}}"

    if node_type == "hardBreak":
        return "\n"

    if node_type in ("paragraph", "heading"):
        inner = "".join(node_to_text(child) for child in node.get("content", []))
        return inner + "\n"

    if node_type == "listItem":
        inner = "".join(node_to_text(child) for child in node.get("content", [])).rstrip()
        return f"- {inner}\n"

    if node_type in ("bulletList", "orderedList"):
        return "".join(node_to_text(child) for child in node.get("content", []))

    # Generic block fallback (mirrors nodeToText's `default` case) — also
    # covers blockquote, which the TS switch leaves uncased.
    content = node.get("content")
    if content:
        inner = "".join(node_to_text(child) for child in content)
        return inner + "\n"
    return ""


def tiptap_to_plain_text(doc: dict) -> str:
    """Extract plain text (with any unresolved {{tag}} markers preserved) from a doc."""
    text = "".join(node_to_text(child) for child in doc.get("content", []))
    return text.rstrip()
