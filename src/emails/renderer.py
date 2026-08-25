"""Top-level Tiptap JSON -> email (HTML + plain text) render pipeline.

Pipeline: resolve merge tags/links (``link_resolver``) -> walk the resolved doc
into an HTML fragment + plain text (``tiptap_render``) -> inject into a Jinja2
shell (plain ``base.html`` by default, ``base_branded.html`` when the sender
opted into CMM branding) -> inline CSS (``inliner``). Content is authored via a
restricted Tiptap schema and rendered fresh at send time (not pre-rendered/
stored) so the same source of truth backs preview and send.
"""

from __future__ import annotations

import json
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup

from src.emails.inliner import inline_css
from src.emails.link_resolver import resolve_plain_text, resolve_tiptap_doc
from src.emails.tiptap_render import render_doc_to_html, tiptap_to_plain_text

_TEMPLATE_DIR = Path(__file__).resolve().parent.parent.parent / "templates" / "email"

_jinja_env = Environment(
    loader=FileSystemLoader(str(_TEMPLATE_DIR)),
    autoescape=select_autoescape(["html"]),
)


def render_email(
    doc_json: str | dict,
    merge_tag_replacements: dict[str, str],
    subject: str,
    *,
    school_slug: str | None = None,
    unsubscribe_url: str | None = None,
    origin: str | None = None,
    include_branding: bool = False,
) -> tuple[str, str]:
    """Render a Tiptap JSON document into inlined-CSS HTML + plain text.

    Args:
        doc_json: Tiptap JSON document, as a JSON string or already-parsed dict.
        merge_tag_replacements: ``{{tag}}`` -> value map. Values are escaped
            before HTML injection — never rendered as raw HTML.
        subject: Email subject line (may itself contain ``{{tag}}`` placeholders).
        school_slug: Scopes resolved internal links to ``/school/<slug>/...``.
        unsubscribe_url: Populated by the caller (phase 2); renders empty-safe
            when omitted.
        origin: Absolute origin for internal links. Defaults to
            ``settings.app_public_url`` when omitted.
        include_branding: Wrap the body in the CMM shell (logo, card, footer
            rule) instead of the default plain, Gmail-like message. Opt-in per
            template — see ``EmailTemplate.include_branding``.

    Returns:
        ``(html, text)`` — inlined-CSS HTML ready for SES, and a plain-text
        alternative for the multipart message.
    """
    # Imported lazily so tests can monkeypatch `settings.app_public_url` freely
    # without import-order surprises.
    from src.config import settings

    doc = json.loads(doc_json) if isinstance(doc_json, str) else doc_json
    effective_origin = origin if origin is not None else (settings.app_public_url or None)

    resolved_doc = resolve_tiptap_doc(
        doc,
        merge_tag_replacements,
        origin=effective_origin,
        school_slug=school_slug,
    )

    body_html = render_doc_to_html(resolved_doc)
    plain_text = tiptap_to_plain_text(resolved_doc)
    resolved_subject = resolve_plain_text(subject, merge_tag_replacements)

    template = _jinja_env.get_template("base_branded.html" if include_branding else "base.html")
    html_out = template.render(
        subject=resolved_subject,
        # Already HTML-escaped node-by-node in tiptap_render — marking safe here
        # avoids Jinja double-escaping the fragment's own tags.
        body_html=Markup(body_html),
        unsubscribe_url=unsubscribe_url or "",
    )

    return inline_css(html_out), plain_text
