"""Golden-fixture parity test — shared cross-repo fixture asserts the Python
renderer produces the same resolved hrefs / substituted text / plain text as
the TS implementation (app/lib/tiptap/__tests__/email-render-golden.test.ts
in cmm-frontend loads the byte-identical copy of this fixture).

Also covers behavior with no TS counterpart (full HTML shell output, escaping,
and the "unknown node type raises" contract) Python-side only, per the phase
spec's parity-scoping note.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.emails.link_resolver import resolve_tiptap_doc
from src.emails.renderer import render_email
from src.emails.tiptap_render import node_to_html, tiptap_to_plain_text

_FIXTURE_PATH = Path(__file__).resolve().parent.parent.parent / "src" / "emails" / "fixtures" / "email-render-golden.json"


def _load_fixture() -> list[dict]:
    return json.loads(_FIXTURE_PATH.read_text())


def _first_link_href(node: dict) -> str | None:
    """Depth-first search for the first `link` mark's href in a resolved doc."""
    for mark in node.get("marks", []) or []:
        if mark.get("type") == "link":
            return mark.get("attrs", {}).get("href")
    for child in node.get("content", []) or []:
        found = _first_link_href(child)
        if found is not None:
            return found
    return None


@pytest.mark.parametrize("case", _load_fixture(), ids=lambda c: c["id"])
def test_golden_fixture_case(case: dict):
    opts = case.get("resolveOptions", {})
    resolved = resolve_tiptap_doc(
        case["doc"],
        case["replacements"],
        origin=opts.get("origin"),
        school_slug=opts.get("schoolSlug"),
    )

    if case["expectedHref"] is not None:
        assert _first_link_href(resolved) == case["expectedHref"]

    assert tiptap_to_plain_text(resolved) == case["expectedPlainText"]


# ── Python-only: full HTML shell + escaping + schema enforcement ────────────


def _kitchen_sink_doc() -> dict:
    return {
        "type": "doc",
        "content": [
            {"type": "heading", "attrs": {"level": 1}, "content": [{"type": "text", "text": "Title"}]},
            {"type": "heading", "attrs": {"level": 2}, "content": [{"type": "text", "text": "Subtitle"}]},
            {"type": "heading", "attrs": {"level": 3}, "content": [{"type": "text", "text": "Small heading"}]},
            {
                "type": "blockquote",
                "content": [{"type": "paragraph", "content": [{"type": "text", "text": "A quoted line"}]}],
            },
            {
                "type": "bulletList",
                "content": [
                    {"type": "listItem", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "Bullet one"}]}]},
                ],
            },
            {
                "type": "orderedList",
                "content": [
                    {"type": "listItem", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "Step one"}]}]},
                ],
            },
            {
                "type": "paragraph",
                "content": [
                    {"type": "text", "text": "bold", "marks": [{"type": "bold"}]},
                    {"type": "hardBreak"},
                    {"type": "text", "text": "italic", "marks": [{"type": "italic"}]},
                    {
                        "type": "text",
                        "text": "link",
                        "marks": [{"type": "link", "attrs": {"href": "https://zoom.us/j/1"}}],
                    },
                ],
            },
        ],
    }


def test_render_email_produces_full_html_shell_with_restricted_node_set():
    html, text = render_email(_kitchen_sink_doc(), {}, "Weekly update", school_slug=None, origin="https://x.com")

    assert "<!DOCTYPE html>" in html
    assert "<h1" in html and "Title" in html
    assert "<h2" in html and "Subtitle" in html
    assert "<h3" in html and "Small heading" in html
    assert "<blockquote" in html and "A quoted line" in html
    assert "<ul" in html and "<li" in html and "Bullet one" in html
    assert "<ol" in html and "Step one" in html
    assert "<strong>bold</strong>" in html
    assert "<em>italic</em>" in html
    assert '<a href="https://zoom.us/j/1"' in html
    # premailer's lxml-based CSS inliner normalizes void elements to `<br>`
    # (no self-closing slash) — both render identically in email clients.
    assert "<br>" in html
    assert text.startswith("Title\nSubtitle\nSmall heading\n")


def test_render_email_escapes_merge_tag_values_no_raw_html_injection():
    doc = {
        "type": "doc",
        "content": [
            {"type": "paragraph", "content": [{"type": "mergeTag", "attrs": {"tag": "untrusted"}}]}
        ],
    }
    html, _text = render_email(doc, {"untrusted": "<script>alert(1)</script>"}, "Subject")
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def _hi_doc() -> dict:
    return {"type": "doc", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "hi"}]}]}


def test_render_email_defaults_to_plain_shell_without_branding_or_unsubscribe():
    """The default send reads like a plain message: no logo, no branded footer,
    and — with no unsubscribe URL supplied — nothing but the authored body."""
    html, _text = render_email(_hi_doc(), {}, "Subject")

    assert "hi" in html
    assert "College Money Method" not in html
    assert "<img" not in html
    assert "Unsubscribe" not in html


def test_render_email_plain_shell_keeps_the_unsubscribe_link():
    """Branding is optional; the unsubscribe link is not — it is required on
    subscriber mail regardless of which shell renders it."""
    html, _text = render_email(_hi_doc(), {}, "Subject", unsubscribe_url="https://x.com/u?t=tok")

    assert 'href="https://x.com/u?t=tok"' in html
    assert "Unsubscribe" in html


def test_render_email_branded_shell_adds_logo_and_footer():
    html, _text = render_email(_hi_doc(), {}, "Subject", include_branding=True)

    assert "<img" in html
    assert "College Money Method" in html


def test_node_to_html_raises_on_unsupported_node_type():
    with pytest.raises(ValueError, match="Unsupported Tiptap node type"):
        node_to_html({"type": "image", "attrs": {"src": "https://x.com/y.png"}})


def test_plain_shell_renders_the_body_with_no_styling_at_all():
    """Non-branded sends must carry zero inline styling — not on the shell, and
    not on any body node. Brand fonts/colors belong to the branded shell only."""
    html, _text = render_email(
        _kitchen_sink_doc(), {}, "Subject", unsubscribe_url="https://x.com/u?t=tok"
    )

    assert "style=" not in html
    assert "font-family" not in html
    # Structure still survives — only the styling is gone.
    assert "<h1>Title</h1>" in html
    assert "<blockquote>" in html
    assert "<ul>" in html and "<li>" in html
    assert '<a href="https://zoom.us/j/1"' in html
    assert 'href="https://x.com/u?t=tok"' in html


def test_branded_shell_keeps_the_brand_inline_styles_on_the_body():
    html, _text = render_email(_kitchen_sink_doc(), {}, "Subject", include_branding=True)

    assert "font-family: 'Lora'" in html or "Lora" in html
    assert "font-family" in html
