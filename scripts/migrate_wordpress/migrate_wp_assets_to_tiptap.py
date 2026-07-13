#!/usr/bin/env python3
"""Migrate content_assets whose link points to collegemoneymethod.com WordPress posts.

For each matching asset:
  1. Fetch the WordPress post HTML via REST API
  2. Download images embedded in the content
  3. Upload images to S3 (under assets/{asset_id}/images/)
  4. Replace img src with S3 URLs
  5. Use LLM to produce clean content_html → convert to Tiptap JSON
  6. Save content (Tiptap JSON) + description + audit columns to DB
  7. Clear the link field (content is now hosted here)

Usage:
  uv run python scripts/migrate_wp_assets_to_tiptap.py --wp-domain https://collegemoneymethod.com --dry-run
  uv run python scripts/migrate_wp_assets_to_tiptap.py --wp-domain https://collegemoneymethod.com
  uv run python scripts/migrate_wp_assets_to_tiptap.py --wp-domain https://collegemoneymethod.com --provider claude --overwrite
  uv run python scripts/migrate_wp_assets_to_tiptap.py --wp-domain https://collegemoneymethod.com --asset-id <uuid>
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import re
import sys
import urllib.parse
from datetime import datetime, timezone
from html import escape as _html_escape
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import text

from src.db.base import get_engine

load_dotenv()

# ── CMM Design palette ────────────────────────────────────────────────────────
_CMM_TEAL = "#4F788D"
_CMM_FOREST = "#2E5E4A"
_CMM_NAVY = "#1E3A5F"
_CMM_SEA_GLASS = "#B0C8C0"

# ── Void / block / wrapper HTML tag sets ─────────────────────────────────────
_VOID_TAGS = frozenset({
    "area", "base", "br", "col", "embed", "hr", "img", "input",
    "link", "meta", "param", "source", "track", "wbr",
})
_SIMPLE_BLOCK_TAGS = frozenset({"h1", "h2", "h3", "h4", "h5", "h6", "p", "ul", "ol", "blockquote", "hr"})
_WRAPPER_TAGS = frozenset({"main", "article", "section", "div", "aside", "header", "footer", "nav"})
_COMPLEX_TAGS = frozenset({"canvas", "script", "style", "svg", "object", "embed", "figure", "form"})


# ── Minimal DOM builder ───────────────────────────────────────────────────────

class _HN:
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
    if isinstance(n, str):
        return _html_escape(n)
    tag = n.tag
    attrs_str = "".join(
        f' {k}="{_html_escape(v, quote=True)}"' for k, v in n.attrs.items()
    )
    if tag in _VOID_TAGS:
        return f"<{tag}{attrs_str}>"
    if tag in ("script", "style"):
        inner = "".join(c if isinstance(c, str) else _serialize_node(c) for c in n.children)
        return f"<{tag}{attrs_str}>{inner}</{tag}>"
    inner = "".join(_serialize_node(c) for c in n.children)
    return f"<{tag}{attrs_str}>{inner}</{tag}>"


# ── HTML → Tiptap JSON (ported from import_topics_from_google_docs.py) ────────

def _is_complex_node(el: _HN) -> bool:
    if el.tag in _COMPLEX_TAGS:
        return True
    if el.tag == "a":
        return False
    if el.tag == "span":
        style = el.attrs.get("style", "")
        other_styles = re.sub(r"(?:^|;)?\s*(?:background-)?color\s*:[^;]+", "", style).strip("; ")
        if not other_styles:
            return False
    if el.attrs.get("style"):
        return True
    return any(isinstance(c, _HN) and _is_complex_node(c) for c in el.children)


def _has_block_child(el: _HN) -> bool:
    return any(isinstance(c, _HN) and c.tag in _SIMPLE_BLOCK_TAGS for c in el.children)


def _inline_to_content(node: _HN | str) -> list[dict]:
    if isinstance(node, str):
        text_val = re.sub(r"\s+", " ", node)
        return [{"type": "text", "text": text_val}] if text_val else []

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


def _apply_color_to_content(content: list[dict], color: str) -> list[dict]:
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
        inline = _trim_content([item for c in el.children for item in _inline_to_content(c)])
        style = el.attrs.get("style", "")
        fg_match = re.search(r"(?<!background-)color\s*:\s*([^;]+)", style)
        if fg_match:
            inline = _apply_color_to_content(inline, fg_match.group(1).strip())
        return {"type": "heading", "attrs": {"level": level}, "content": inline}

    if tag == "p":
        inline = _trim_content([item for c in el.children for item in _inline_to_content(c)])
        if not inline:
            return {"type": "paragraph"}
        return {"type": "paragraph", "content": inline}

    if tag in ("ul", "ol"):
        list_type = "bulletList" if tag == "ul" else "orderedList"
        items: list[dict] = []
        for child in el.children:
            if isinstance(child, _HN) and child.tag == "li":
                inline = _trim_content([item for c in child.children for item in _inline_to_content(c)])
                items.append({"type": "listItem", "content": [{"type": "paragraph", "content": inline}]})
        return {"type": list_type, "content": items} if items else None

    if tag == "blockquote":
        inline = _trim_content([item for c in el.children for item in _inline_to_content(c)])
        return {"type": "blockquote", "content": [{"type": "paragraph", "content": inline}]}

    if tag == "hr":
        return {"type": "horizontalRule"}

    return None


def _callout_to_tiptap_node(el: _HN) -> dict:
    """Convert a <div data-callout ...> element to a native Tiptap callout node."""
    style = el.attrs.get("style", "")
    bg_match = re.search(r"background-color\s*:\s*([^;]+)", style)
    fg_match = re.search(r"(?<!background-)color\s*:\s*([^;]+)", style)
    align_match = re.search(r"text-align\s*:\s*([^;]+)", style)
    padding_match = re.search(r"\bpadding\s*:\s*([^;]+)", style)
    fontsize_match = re.search(r"font-size\s*:\s*([^;]+)", style)
    fontweight_match = re.search(r"font-weight\s*:\s*([^;]+)", style)

    attrs = {
        "backgroundColor": bg_match.group(1).strip() if bg_match else "#4F788D",
        "textColor": fg_match.group(1).strip() if fg_match else "#ffffff",
        "borderRadius": "4px",
        "textAlign": align_match.group(1).strip() if align_match else "center",
        "textTransform": "none",
        "fontSize": fontsize_match.group(1).strip() if fontsize_match else None,
        "fontWeight": fontweight_match.group(1).strip() if fontweight_match else None,
        "padding": padding_match.group(1).strip() if padding_match else "20px 24px",
    }

    content: list[dict] = []
    _walk_to_tiptap(el, content)
    if not content:
        content = [{"type": "paragraph"}]

    return {"type": "callout", "attrs": attrs, "content": content}


def _walk_to_tiptap(el: _HN, nodes: list[dict]) -> None:
    for child in el.children:
        if isinstance(child, str):
            continue
        tag = child.tag

        if tag in ("script", "style"):
            continue

        if tag == "table":
            nodes.append({"type": "rawHtml", "attrs": {"html": _serialize_node(child)}})
            continue

        if tag in _COMPLEX_TAGS:
            nodes.append({"type": "rawHtml", "attrs": {"html": _serialize_node(child)}})
            continue

        # Callout block — must be checked before the generic _WRAPPER_TAGS branch
        if tag == "div" and "data-callout" in child.attrs:
            nodes.append(_callout_to_tiptap_node(child))
            continue

        if tag in _SIMPLE_BLOCK_TAGS:
            if re.match(r"^h[1-6]$", tag):
                node = _block_to_tiptap_node(child)
                if node:
                    nodes.append(node)
            elif _is_complex_node(child):
                nodes.append({"type": "rawHtml", "attrs": {"html": _serialize_node(child)}})
            else:
                node = _block_to_tiptap_node(child)
                if node:
                    nodes.append(node)
            continue

        if tag in _WRAPPER_TAGS:
            if _is_complex_node(child):
                nodes.append({"type": "rawHtml", "attrs": {"html": _serialize_node(child)}})
            else:
                _walk_to_tiptap(child, nodes)
            continue

        if _has_block_child(child):
            _walk_to_tiptap(child, nodes)
            continue

        nodes.append({"type": "rawHtml", "attrs": {"html": _serialize_node(child)}})


def _html_to_tiptap(html: str) -> dict:
    builder = _DOMBuilder()
    builder.feed(html)
    container = _dom_find(builder.root, "body") or builder.root
    nodes: list[dict] = []
    _walk_to_tiptap(container, nodes)
    if not nodes:
        nodes = [{"type": "paragraph"}]
    return {"type": "doc", "content": nodes}


# ── HTML sanitization ─────────────────────────────────────────────────────────

def _sanitize_html(html: str) -> str:
    cleaned = re.sub(r"<style[^>]*>.*?</style>", "", html, flags=re.IGNORECASE | re.DOTALL)
    cleaned = re.sub(r"<script[^>]*>.*?</script>", "", cleaned, flags=re.IGNORECASE | re.DOTALL)
    cleaned = re.sub(r"\s+on\w+\s*=\s*(?:\"[^\"]*\"|'[^']*'|[^\s>]+)", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"(href|src)\s*=\s*\"javascript:[^\"]*\"", r"\1=\"#\"", cleaned, flags=re.IGNORECASE)
    return cleaned.strip()


def _extract_body_html(html: str) -> str:
    body_match = re.search(r"<body[^>]*>(.*?)</body>", html, flags=re.IGNORECASE | re.DOTALL)
    if body_match:
        return body_match.group(1).strip()
    return html


# ── WordPress REST API ────────────────────────────────────────────────────────

def _is_wp_upload_link(link: str) -> bool:
    return "/wp-content/" in link


def _normalize_netloc(netloc: str) -> str:
    """Strip www. prefix for loose domain matching."""
    return netloc.lower().removeprefix("www.")


def _extract_wp_slug(link: str, wp_domain: str) -> str | None:
    try:
        parsed = urlparse(link)
        wp_parsed = urlparse(wp_domain)
        if _normalize_netloc(parsed.netloc) != _normalize_netloc(wp_parsed.netloc):
            return None
        parts = [p for p in parsed.path.strip("/").split("/") if p]
        return parts[-1] if parts else None
    except Exception:
        return None


def _fetch_wp_post(wp_domain: str, slug: str) -> dict | None:
    """Fetch post from WP REST API by slug. Tries posts then pages."""
    base = wp_domain.rstrip("/")
    for endpoint in ("posts", "pages"):
        try:
            resp = requests.get(
                f"{base}/wp-json/wp/v2/{endpoint}",
                params={"slug": slug, "_fields": "id,title,content,excerpt,link"},
                timeout=20,
            )
            resp.raise_for_status()
            items = resp.json()
            if items:
                return items[0]
        except Exception as exc:
            print(f"    [warn] WP API {endpoint} error for slug '{slug}': {exc}")
    return None


def _fetch_wp_post_by_id(wp_domain: str, wp_post_id: str) -> dict | None:
    """Fetch a previously migrated post by its stored wp_post_id. Tries posts then pages."""
    base = wp_domain.rstrip("/")
    for endpoint in ("posts", "pages"):
        try:
            resp = requests.get(
                f"{base}/wp-json/wp/v2/{endpoint}/{wp_post_id}",
                params={"_fields": "id,title,content,excerpt,link"},
                timeout=20,
            )
            if resp.status_code == 404:
                continue
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            print(f"    [warn] WP API {endpoint}/{wp_post_id} error: {exc}")
    return None


# ── Image download & S3 upload ────────────────────────────────────────────────

_IMG_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"})


def _download_image(url: str) -> bytes | None:
    try:
        resp = requests.get(url, timeout=20)
        resp.raise_for_status()
        return resp.content
    except Exception as exc:
        print(f"    [warn] Failed to download image {url}: {exc}")
        return None


def _s3_client():
    import boto3
    from src.config import settings
    return boto3.client(
        "s3",
        aws_access_key_id=settings.aws_access_key_id,
        aws_secret_access_key=settings.aws_secret_access_key,
        region_name=settings.aws_region,
    ), settings


def _upload_image_to_s3(
    data: bytes,
    filename: str,
    asset_id: str,
    dry_run: bool,
) -> str:
    """Upload image bytes to S3 and return the public URL."""
    content_type, _ = mimetypes.guess_type(filename)
    content_type = content_type or "image/png"
    s3_key = f"assets/{asset_id}/images/{filename}"

    s3, settings = _s3_client()
    s3_url = (
        f"https://{settings.s3_bucket_name}.s3.{settings.aws_region}.amazonaws.com/{s3_key}"
    )

    if dry_run:
        print(f"      [dry-run] would upload {len(data)} bytes → s3://{settings.s3_bucket_name}/{s3_key}")
    else:
        s3.put_object(
            Bucket=settings.s3_bucket_name,
            Key=s3_key,
            Body=data,
            ContentType=content_type,
        )

    return s3_url


def _rehost_images(html: str, asset_id: str, dry_run: bool, wp_domain: str) -> str:
    """Download images from WP and re-upload to S3. Replaces src in-place."""
    img_pattern = re.compile(r'<img([^>]*?)src="([^"]+)"([^>]*?)/?>', re.IGNORECASE)
    seen: dict[str, str] = {}  # original_url → s3_url

    def _replace(m: re.Match) -> str:
        pre, src, post = m.group(1), m.group(2), m.group(3)
        if src in seen:
            return f'<img{pre}src="{seen[src]}"{post}>'

        # Only rehost images that are on the WP domain or relative
        parsed_src = urlparse(src)
        wp_parsed = urlparse(wp_domain)
        is_wp_image = (
            not parsed_src.scheme  # relative
            or _normalize_netloc(parsed_src.netloc) == _normalize_netloc(wp_parsed.netloc)
        )
        if not is_wp_image:
            seen[src] = src
            return m.group(0)

        ext = Path(parsed_src.path).suffix.lower() or ".jpg"
        if ext not in _IMG_EXTENSIONS:
            seen[src] = src
            return m.group(0)

        # Build an absolute URL for downloading
        full_url = src if parsed_src.scheme else urllib.parse.urljoin(wp_domain, src)
        data = _download_image(full_url)
        if not data:
            seen[src] = src
            return m.group(0)

        # Derive a safe filename (strip query params, deduplicate)
        filename = Path(parsed_src.path).name or "image.jpg"
        # If duplicate filename, suffix with a counter
        counter = sum(1 for k in seen if Path(urlparse(k).path).name == filename)
        if counter:
            stem, sfx = filename.rsplit(".", 1) if "." in filename else (filename, "jpg")
            filename = f"{stem}-{counter}.{sfx}"

        s3_url = _upload_image_to_s3(data, filename, asset_id, dry_run)
        seen[src] = s3_url
        return f'<img{pre}src="{s3_url}"{post}>'

    return img_pattern.sub(_replace, html)


# ── LLM normalization ─────────────────────────────────────────────────────────

_LLM_SYSTEM = (
    "You are a content processing assistant for College Money Method (CMM), "
    "an educational platform helping families navigate college financial aid. "
    "Convert WordPress HTML into clean CMS resource content. "
    "Return valid JSON only — no markdown fences, no extra text."
)

_LLM_PROMPT = """\
Convert this WordPress page HTML into structured CMS resource fields for College Money Method.

## Output JSON Schema
{{
  "title": "string — clean page title, remove site name suffix like '| College Money Method'",
  "description": "string | null — 1–2 sentence plain-text summary of the resource",
  "content_html": "string — clean article body HTML (see rules below)"
}}

## Content Rules
- Remove the page title/headline from content_html (it is stored separately as title)
- Remove navigation menus, breadcrumbs, sidebars, footers, newsletter signup blocks
- Remove social share buttons, author bios, "related posts" sections
- Remove WordPress plugin shortcodes like [gallery], [contact-form-7], etc.
- Keep tables — format them with CMM styles (see below)
- Keep all substantive text, headings, lists, and images
- No outer <html>/<head>/<body> wrappers — return only inner body HTML

## CMM Table Styling (apply inline styles)
- <table>: style="border-collapse: collapse; width: 100%;"
- <th>: style="background-color: {teal}; color: white; padding: 8px 12px; text-align: left; font-weight: 700; border-right: 1px solid rgba(255,255,255,0.2); border-bottom: 2px solid rgba(0,0,0,0.15); white-space: nowrap;"
- <td>: style="padding: 8px 12px; border: 1px solid {sea_glass}; color: {navy}; vertical-align: top;"
- <a>: style="color: {navy};"
- Do NOT add inline styles to headings, paragraphs, or lists

## WordPress Plugin Blocks → Callout

### Getwid Icon Box (wp-block-getwid-icon-box)
Any `<div class="wp-block-getwid-icon-box ...">` is a highlighted callout/tip block.
Convert its text content (from `wp-block-getwid-icon-box__content`) to a callout div:
```html
<div data-callout style="background-color:#4F788D;color:#ffffff;text-align:left;font-size:24px;font-weight:700;padding:20px 24px;">
  <p>Callout text here</p>
</div>
```
Drop the icon element (`wp-block-getwid-icon-box__icon-container`) entirely.

### Getwid Tabs (wp-block-getwid-tabs)
A `<div class="wp-block-getwid-tabs ...">` contains the same article repeated for each tab (e.g. "2025-26 FAFSA", "2026-27 FAFSA").
CRITICAL — do NOT duplicate the content:
- Render each tab as: a heading (`<h5>`) using the tab title, followed by its inner content
- Each tab's content appears ONCE — never repeat the same table or text for multiple tabs

## Equation / Formula Blocks → Callout
Identify any equation or formula content and wrap it in a callout div.
Use `text-align` from the source element's `has-text-align-left/center/right` class (default: center).
Always include `font-size:24px;font-weight:700`:
```html
<div data-callout style="background-color:#4F788D;color:#ffffff;text-align:left;font-size:24px;font-weight:700;padding:20px 24px;">
  <p>Term A</p>
  <p>× Term B</p>
  <p>= Result</p>
</div>
```
Detect formula blocks by these signals:
- Content styled with `has-blue-color`, `has-primary-color`, `has-background`, or similar WP color classes that looks like an equation
- Lines starting with a math operator (×, ÷, +, −, =, x, /)
- A horizontal rule (`<hr>`) or underscores separating terms above and below (the rule means "equals")
- Content that reads like an arithmetic expression: "A × B", "A – B = C", "Net Price = ..."
- WP paragraph with `has-primary-background-color` or `has-text-color` and math-like content

Alignment: read `has-text-align-left` / `has-text-align-center` / `has-text-align-right` from the source
element's class list and use it as the `text-align` value. Default to `center` if none found.

Rules:
- Preserve each term as its own `<p>` inside the callout div
- Replace any `<hr>` divider inside the formula with a plain "─────" text line or omit it
- Do NOT wrap regular paragraph text, tips, or step-by-step lists as formula callouts

## Styled HTML Blocks — Fallback Styling
For any other visually distinct block (colored container, notice, tip, info box) that you output as HTML
but cannot cleanly convert to a callout div, wrap it with CMM teal background at minimum:
```html
<div style="background-color:#4F788D;color:#ffffff;padding:16px 20px;border-radius:4px;margin:16px 0;">
  ...content...
</div>
```

## HTML Cleanup
- Remove Google redirect wrapper URLs (href="https://www.google.com/url?q=REAL&...") → use the real URL
- Strip ALL WordPress class attributes (class="wp-block-*", class="has-*", class="is-*", etc.)
- Strip inline color/font styles inherited from WP theme classes (e.g. style="color:var(--wp--...)")
- Keep only meaningful inline styles: background-color, color, padding, border, text-align on structural divs
- Keep `<strong>`, `<em>`, `<a href="...">`, `<br>` inline formatting
- Images: leave `<img>` tags as-is — src attributes have already been replaced with S3 URLs

ASSET NAME (fallback title if title not found in HTML): {asset_name}

SOURCE HTML:
{raw_html}
""".format(
    teal=_CMM_TEAL,
    sea_glass=_CMM_SEA_GLASS,
    navy=_CMM_NAVY,
    asset_name="{asset_name}",
    raw_html="{raw_html}",
)


def _build_prompt(raw_html: str, asset_name: str) -> str:
    return _LLM_PROMPT.replace("{asset_name}", asset_name).replace("{raw_html}", raw_html)


def _extract_json_object(text_val: str) -> dict[str, Any]:
    text_val = text_val.strip()
    try:
        parsed = json.loads(text_val)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{[\s\S]*\}", text_val)
    if not match:
        raise ValueError("LLM response did not include valid JSON")
    parsed = json.loads(match.group(0))
    if not isinstance(parsed, dict):
        raise ValueError("LLM response JSON must be an object")
    return parsed


def _call_openai(model: str, api_key: str, prompt: str) -> dict[str, Any]:
    schema = {
        "name": "resource_payload",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "description": {"type": ["string", "null"]},
                "content_html": {"type": "string"},
            },
            "required": ["title", "description", "content_html"],
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
                {"role": "system", "content": _LLM_SYSTEM},
                {"role": "user", "content": prompt},
            ],
            "response_format": {"type": "json_schema", "json_schema": schema},
        },
        timeout=120,
    )
    resp.raise_for_status()
    content = resp.json()["choices"][0]["message"]["content"]
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
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=180,
    )
    resp.raise_for_status()
    payload = resp.json()
    text_blocks = [b.get("text", "") for b in payload.get("content", []) if b.get("type") == "text"]
    return _extract_json_object("\n".join(text_blocks))


def _normalize_with_llm(
    provider: str,
    model: str,
    raw_html: str,
    asset_name: str,
) -> tuple[str, str | None, dict]:
    """Return (title, description, tiptap_doc)."""
    body_html = _sanitize_html(_extract_body_html(raw_html))
    prompt = _build_prompt(raw_html=body_html, asset_name=asset_name)

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

    title = (parsed.get("title") or asset_name or "Untitled").strip()
    description = (parsed.get("description") or "").strip() or None
    content_html = _sanitize_html(parsed.get("content_html") or body_html)
    tiptap = _html_to_tiptap(content_html)

    return title, description, tiptap


def _choose_provider(provider: str) -> str:
    if provider != "auto":
        return provider
    if os.getenv("OPENAI_API_KEY", "").strip():
        return "openai"
    if os.getenv("ANTHROPIC_API_KEY", "").strip():
        return "claude"
    raise RuntimeError("No LLM API key found. Set OPENAI_API_KEY or ANTHROPIC_API_KEY.")


def _default_model(provider: str) -> str:
    if provider == "openai":
        return "gpt-4.1"
    if provider == "claude":
        return "claude-sonnet-4-6"
    return "none"


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Migrate collegemoneymethod.com WordPress assets → Tiptap content"
    )
    parser.add_argument(
        "--wp-domain",
        default="https://collegemoneymethod.com",
        help="WordPress site base URL (default: https://collegemoneymethod.com)",
    )
    parser.add_argument(
        "--provider",
        choices=["auto", "openai", "claude"],
        default="auto",
        help="LLM provider for HTML normalization (default: auto)",
    )
    parser.add_argument("--model", default=None, help="Override LLM model name")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would happen without writing to DB or uploading to S3",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Re-migrate assets that already have content (default: skip them)",
    )
    parser.add_argument(
        "--keep-link",
        action="store_true",
        help="Keep the original link field after migration (default: clear it)",
    )
    parser.add_argument(
        "--asset-id",
        default=None,
        help="Migrate a single asset by its UUID (for testing)",
    )
    args = parser.parse_args()

    provider = _choose_provider(args.provider)
    model = args.model or _default_model(provider)
    wp_domain = args.wp_domain.rstrip("/")

    sep = "─" * 60
    print(sep)
    print("  CMM WordPress → Tiptap Asset Migration")
    print(f"  WP domain : {wp_domain}")
    print(f"  LLM       : {provider} / {model}")
    if args.dry_run:
        print("  Mode      : DRY RUN — no DB writes, no S3 uploads")
    if args.overwrite:
        print("  Mode      : --overwrite (re-migrate assets with existing content)")
    if args.keep_link:
        print("  Mode      : --keep-link (link field will NOT be cleared)")
    print(sep)

    engine = get_engine()
    migrated = skipped = failed = not_found = 0

    with engine.begin() as conn:
        if args.asset_id:
            rows = conn.execute(
                text(
                    "SELECT id, name, link, content, description, wp_post_id "
                    "FROM content_assets WHERE id = :id"
                ),
                {"id": args.asset_id},
            ).fetchall()
        elif args.overwrite:
            # Include both unprocessed (link set) and previously migrated (wp_post_id set)
            rows = conn.execute(
                text(
                    """
                    SELECT id, name, link, content, description, wp_post_id
                    FROM content_assets
                    WHERE (
                        (link IS NOT NULL
                          AND link ILIKE '%collegemoneymethod.com%'
                          AND link NOT ILIKE '%/wp-content/%')
                        OR wp_post_id IS NOT NULL
                    )
                    ORDER BY created_at
                    """
                )
            ).fetchall()
        else:
            rows = conn.execute(
                text(
                    """
                    SELECT id, name, link, content, description, wp_post_id
                    FROM content_assets
                    WHERE link IS NOT NULL
                      AND link ILIKE '%collegemoneymethod.com%'
                      AND link NOT ILIKE '%/wp-content/%'
                    ORDER BY created_at
                    """
                )
            ).fetchall()

        print(f"Found {len(rows)} asset(s) to migrate\n")

        for row in rows:
            asset_id, name, link, existing_content, existing_desc, stored_wp_post_id = (
                str(row[0]), row[1], row[2], row[3], row[4], row[5]
            )

            print(f"{'━' * 60}")
            print(f"  [{asset_id}] {name}")
            print(f"  link: {link or '(cleared — re-fetching by wp_post_id)'}")

            # Skip file uploads
            if link and _is_wp_upload_link(link):
                print("  [skip] wp-content file upload — not a post")
                skipped += 1
                continue

            # Skip already-migrated unless --overwrite
            if existing_content and not args.overwrite:
                print("  [skip] content already exists (use --overwrite to re-migrate)")
                skipped += 1
                continue

            # 1. Fetch WP post — by slug (new) or by stored wp_post_id (re-migration)
            if link:
                slug = _extract_wp_slug(link, wp_domain)
                if not slug:
                    print(f"  [skip] could not extract slug from link: {link}")
                    skipped += 1
                    continue
                print(f"  [1/4] Fetching WP post (slug: {slug}) ...", end=" ", flush=True)
                post = _fetch_wp_post(wp_domain, slug)
            else:
                print(f"  [1/4] Fetching WP post (id: {stored_wp_post_id}) ...", end=" ", flush=True)
                post = _fetch_wp_post_by_id(wp_domain, stored_wp_post_id)

            if not post:
                print("not found")
                not_found += 1
                continue

            raw_html = post.get("content", {}).get("rendered", "")
            wp_title = (post.get("title", {}).get("rendered") or "").strip()
            excerpt_text = re.sub(
                r"<[^>]+>", "",
                post.get("excerpt", {}).get("rendered", "")
            ).strip()
            wp_post_id = str(post.get("id", ""))
            print(f"ok (wp_id={wp_post_id}, {len(raw_html):,} chars)")

            if not raw_html:
                print("  [skip] empty content from WP")
                not_found += 1
                continue

            # 2. Rehost images to S3
            print("  [2/4] Downloading & uploading images to S3 ...", end=" ", flush=True)
            raw_html_with_s3 = _rehost_images(
                html=raw_html,
                asset_id=asset_id,
                dry_run=args.dry_run,
                wp_domain=wp_domain,
            )
            img_count = len(re.findall(r'amazonaws\.com', raw_html_with_s3))
            print(f"done ({img_count} image(s) re-hosted)")

            # 3. LLM normalization → Tiptap
            print(f"  [3/4] Sending to {provider} ({model}) ...", end=" ", flush=True)
            try:
                title, description, tiptap = _normalize_with_llm(
                    provider=provider,
                    model=model,
                    raw_html=raw_html_with_s3,
                    asset_name=wp_title or name,
                )
            except Exception as exc:
                print(f"\n  [ERROR] LLM failed: {exc}")
                failed += 1
                continue

            block_count = len(tiptap.get("content", []))
            raw_count = sum(1 for b in tiptap.get("content", []) if b.get("type") == "rawHtml")
            print(f"done — title={title!r}, {block_count} blocks ({raw_count} rawHtml)")

            # 4. Write to DB
            print("  [4/4] Writing to database ...", end=" ", flush=True)
            if args.dry_run:
                print("skipped (dry-run)")
                migrated += 1
                continue

            final_description = description or excerpt_text or existing_desc or None
            conn.execute(
                text(
                    """
                    UPDATE content_assets SET
                        name         = :name,
                        description  = :description,
                        content      = :content,
                        link         = :link,
                        wp_post_id   = :wp_post_id,
                        wp_synced_at = :wp_synced_at
                    WHERE id = :id
                    """
                ),
                {
                    "id": asset_id,
                    "name": title or name,
                    "description": final_description,
                    "content": json.dumps(tiptap),
                    "link": link if args.keep_link else None,
                    "wp_post_id": wp_post_id,
                    "wp_synced_at": datetime.now(timezone.utc),
                },
            )
            print("done")
            migrated += 1

    print(f"\n{sep}")
    print("  Summary")
    print(f"{sep}")
    print(f"  migrated  : {migrated}")
    print(f"  skipped   : {skipped}")
    print(f"  not found : {not_found}")
    print(f"  failed    : {failed}")
    if args.dry_run:
        print("  (dry-run — no data written)")
    print(sep)

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
