"""Link + merge-tag resolution for Tiptap docs, ported from the TS reference.

Line-for-line port of the resolution logic in cmm-frontend's
``app/lib/tiptap/merge-tag-utils.ts`` (``resolveMarks``, ``buildInternalLinkHref``,
``resolveMergeTag``, ``resolveNode``/``resolveTiptapDoc``). Kept in sync with that
file intentionally — any behavior change there should be mirrored here, and the
golden-fixture tests in both repos are what catch drift.

Origin for backend renders comes from ``settings.app_public_url`` — there is no
``request`` object at send time (unlike interactive routes, which derive origin
from the incoming request).
"""

from __future__ import annotations

import re

# Attributes stamped onto every resolved internal link so it opens safely in an
# external email client (matches the TS `INTERNAL_LINK` constant).
_INTERNAL_LINK_ATTRS = {"target": "_blank", "rel": "noopener noreferrer nofollow"}

# Matches "- Name (https://url)" (bullet optional) — one line of a multi-line
# merge-tag value that should become a hyperlinked name instead of a raw URL.
_NAMED_LINK_RE = re.compile(r"^(-\s+)?(.*\S)\s+\((https?://[^\s)]+)\)$")

_TAG_RE = re.compile(r"\{\{(\w+)\}\}")


def resolve_plain_text(text: str, replacements: dict[str, str]) -> str:
    """Substitute ``{{tag}}`` placeholders in a plain-text string. Unknown tags pass through."""

    def _sub(match: re.Match[str]) -> str:
        key = match.group(1)
        return replacements.get(key, f"{{{{{key}}}}}")

    return _TAG_RE.sub(_sub, text)


def build_internal_link_href(path: str, *, origin: str | None, school_slug: str | None) -> str:
    """Build an absolute, school-scoped href from a school-agnostic internal path.

    ``path`` is stored school-agnostic (``/topic/<slug>`` or ``/resources/<id>``);
    emails are read in external clients, so the href must be absolute.
    """
    if not path:
        return path
    scoped = f"/school/{school_slug}{path}" if school_slug else path
    return f"{origin}{scoped}" if origin else scoped


def _is_internal_link(href: str) -> bool:
    return href.startswith("/topic/") or href.startswith("/resources/")


def resolve_marks(
    marks: list[dict],
    replacements: dict[str, str],
    *,
    origin: str | None = None,
    school_slug: str | None = None,
) -> list[dict]:
    """Resolve a text node's marks: internal links -> absolute, template-var hrefs -> substituted."""
    resolved: list[dict] = []
    for mark in marks:
        mark_type = mark.get("type")

        # Legacy `internalLink` mark (content saved before the migration to
        # plain links) -> absolute, school-scoped `link` mark.
        if mark_type == "internalLink":
            raw_href = mark.get("attrs", {}).get("href")
            path = raw_href if isinstance(raw_href, str) else ""
            resolved.append(
                {
                    "type": "link",
                    "attrs": {
                        "href": build_internal_link_href(path, origin=origin, school_slug=school_slug),
                        **_INTERNAL_LINK_ATTRS,
                    },
                }
            )
            continue

        href = mark.get("attrs", {}).get("href") if mark_type == "link" else None
        if mark_type == "link" and isinstance(href, str):
            if _is_internal_link(href):
                resolved.append(
                    {
                        **mark,
                        "attrs": {
                            **mark["attrs"],
                            "href": build_internal_link_href(href, origin=origin, school_slug=school_slug),
                            **_INTERNAL_LINK_ATTRS,
                        },
                    }
                )
                continue
            # Template-variable link href (e.g. `{{resource_center_url}}`).
            if "{{" in href:
                resolved.append(
                    {**mark, "attrs": {**mark["attrs"], "href": resolve_plain_text(href, replacements)}}
                )
                continue

        resolved.append(mark)

    return resolved


def _link_text_node(text: str, href: str) -> dict:
    return {
        "type": "text",
        "text": text,
        "marks": [{"type": "link", "attrs": {"href": href, **_INTERNAL_LINK_ATTRS}}],
    }


def _merge_line_to_nodes(line: str) -> list[dict]:
    """Convert one line of a merge-tag value into inline nodes, linkifying URLs."""
    if line == "":
        return []
    if line.startswith("http://") or line.startswith("https://"):
        return [_link_text_node(line, line)]
    match = _NAMED_LINK_RE.match(line)
    if match:
        bullet, name, url = match.groups()
        nodes: list[dict] = []
        if bullet:
            nodes.append({"type": "text", "text": bullet})
        nodes.append(_link_text_node(name, url))
        return nodes
    return [{"type": "text", "text": line}]


def resolve_merge_tag(node: dict, replacements: dict[str, str]) -> list[dict]:
    """Resolve a mergeTag node into the inline nodes that replace it.

    A multi-line value (e.g. ``{{resources_list}}``) expands to one line per
    row, separated by hardBreak nodes. Rows shaped like "Name (url)" become
    hyperlinks on the name; bare-URL lines link the URL itself.
    """
    tag = node.get("attrs", {}).get("tag")
    value = replacements.get(tag, f"{{{{{tag}}}}}") if tag is not None else ""
    lines = value.split("\n")

    nodes: list[dict] = []
    for i, line in enumerate(lines):
        if i > 0:
            nodes.append({"type": "hardBreak"})
        nodes.extend(_merge_line_to_nodes(line))
    return nodes


def resolve_node(
    node: dict,
    replacements: dict[str, str],
    *,
    origin: str | None = None,
    school_slug: str | None = None,
) -> dict | list[dict]:
    if node.get("type") == "mergeTag":
        return resolve_merge_tag(node, replacements)

    out = dict(node)

    marks = out.get("marks")
    if marks:
        out["marks"] = resolve_marks(marks, replacements, origin=origin, school_slug=school_slug)

    content = out.get("content")
    if content:
        new_content: list[dict] = []
        for child in content:
            resolved = resolve_node(child, replacements, origin=origin, school_slug=school_slug)
            if isinstance(resolved, list):
                new_content.extend(resolved)
            else:
                new_content.append(resolved)
        out["content"] = new_content

    return out


def resolve_tiptap_doc(
    doc: dict,
    replacements: dict[str, str],
    *,
    origin: str | None = None,
    school_slug: str | None = None,
) -> dict:
    """Resolve mergeTag nodes and link/internalLink marks throughout a Tiptap doc."""
    resolved = resolve_node(doc, replacements, origin=origin, school_slug=school_slug)
    # A top-level doc node never resolves to a list (only mergeTag nodes do).
    assert isinstance(resolved, dict)
    return resolved
