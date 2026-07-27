#!/usr/bin/env python3
"""
Regenerate content_assets.content from the original WordPress page using Claude
on Amazon Bedrock, styled per the CMM design guidelines.

For each asset with a wp_source_url:
  1. Fetch the WP page HTML and extract the main article content
     (scripts/styles/nav stripped, external embeds like iframes preserved).
  2. Send it to Claude (Bedrock) with the CMM design-guidelines skill as a
     prompt-cached system block, asking for a single self-contained HTML
     fragment (scoped CSS, no JS) matching the brand.
  3. Wrap the result in a Tiptap doc with one rawHtml block — the exact shape
     app/components/content/content-renderer.tsx renders in a sandboxed iframe.

Rendering constraints baked into the prompt (see content-renderer.tsx +
sandbox-iframe.ts in cmm-frontend):
  - Fragment only; host wraps it and zeroes body margin/padding.
  - If the fragment contains an external <iframe>, the renderer skips the
    sandbox and injects directly into the page → all CSS must be scoped under
    one root class and inline <script> cannot be relied on. So: scoped CSS,
    no JS, ever.
  - No horizontal padding on the root; host page provides gutters.

Auth: AWS creds from env (.env AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY) or
--aws-profile (e.g. cmm).

Usage (from project root):
  uv run python scripts/migrate_wordpress/regenerate_content_html_with_claude.py \
      --name "Student Aid Index" --out-dir /tmp/regen --dry-run
  uv run python scripts/migrate_wordpress/regenerate_content_html_with_claude.py --apply
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from sqlalchemy import text

from src.db.base import get_engine

DEFAULT_MODEL = "global.anthropic.claude-sonnet-4-6"
DEFAULT_REGION = "us-east-1"
DEFAULT_GUIDELINES = (
    Path.home()
    / "WebstormProjects/cmm-frontend/.claude/skills/cmm-design-guidelines/SKILL.md"
)

# ── Source page extraction ────────────────────────────────────────────────────

_STRIP_TAGS = re.compile(
    r"<(script|style|noscript|form|svg)\b.*?</\1>", re.IGNORECASE | re.DOTALL
)
_STRIP_COMMENTS = re.compile(r"<!--.*?-->", re.DOTALL)
_STRIP_SELF_CLOSING = re.compile(r"<(link|meta)\b[^>]*>", re.IGNORECASE)


def fetch_page(url: str) -> str:
    resp = requests.get(url, timeout=30, headers={"User-Agent": "cmm-migration/1.0"})
    resp.raise_for_status()
    return resp.text


def extract_main_content(html: str) -> str:
    """Extract the article content: <main>, else <article>, else <body>.
    Strips scripts/styles/nav noise but PRESERVES iframes (calculator embeds)."""
    for pattern in (r"<main\b.*?</main>", r"<article\b.*?</article>", r"<body\b.*?</body>"):
        m = re.search(pattern, html, re.DOTALL | re.IGNORECASE)
        if m:
            content = m.group(0)
            break
    else:
        content = html

    content = _STRIP_COMMENTS.sub("", content)
    content = _STRIP_TAGS.sub("", content)
    content = _STRIP_SELF_CLOSING.sub("", content)
    # Collapse blank lines left behind by stripping
    content = re.sub(r"\n\s*\n+", "\n", content)
    return content.strip()


# ── Prompt ────────────────────────────────────────────────────────────────────

INSTRUCTIONS = """\
You are a senior front-end designer for College Money Method (CMM), a college
financial-aid advisory. You convert legacy WordPress article pages into clean,
beautifully branded, self-contained HTML content blocks for the CMM web app.

The HTML you produce is stored as a single block and rendered inside the app,
usually in a sandboxed iframe (scripts disabled in some render paths). Follow
this OUTPUT CONTRACT exactly:

1. Output ONLY the HTML fragment. No markdown fences, no explanation, no
   <!DOCTYPE>, <html>, <head>, or <body> tags.
2. Single root element: <div class="cmm-article"> ... </div>. Its first
   children are the Font Awesome <link> (only if icons are used) followed
   by one <style> tag with ALL styling.
3. Every CSS rule MUST be scoped under .cmm-article (the fragment may be
   injected directly into the host page, so unscoped rules would leak).
   Start the <style> with:
   @import url('https://fonts.googleapis.com/css2?family=Lora:wght@400;500;600&family=Inter:wght@400;500;600&display=swap');
4. NO <script> tags — they will not execute in any render path. Interactive
   behavior is allowed ONLY via inline event-handler attributes (onclick)
   on elements, as in the quick-check wizard recipe below. Prefer CSS-only;
   reach for inline handlers only when a widget genuinely helps.
5. Layout: the root must have NO horizontal padding or margin and no
   max-width — the host page provides gutters. No background color on the
   root (the host paints the page background). Cards/callouts inside may have
   their own backgrounds.
6. Fully responsive, mobile-first. Tables must be wrapped in an
   overflow-x:auto container. Images/iframes: max-width 100%.
7. PRESERVE any external embeds (e.g. <iframe src="https://app.calculatorstudio.co/...">)
   exactly as given — same src AND all original attributes (data-*,
   referrerpolicy, etc.) — restyled to width:100%; border:none. Because
   auto-resize embed scripts cannot run here, give interactive embeds
   (calculators, tools) a generous fixed height: height="800" with
   style min-height:800px. Video embeds keep a 16/9 aspect-ratio instead.
   These embeds are the heart of calculator pages; never drop them.
8. Content fidelity — wording is sacred, presentation is yours:
   - Keep every sentence, heading, number, and link VERBATIM from the
     source. Do not rephrase, summarize, shorten, or expand anything, and
     never invent facts, summary lines, or stat chips the source does not
     contain. That includes condensed restatements: if a paragraph already
     says "the contribution rate is 50%", do NOT append a badge/chip/box
     repeating "50% over $11,770" — the paragraph alone is the content.
   - Keep the source's headings and reading order. Within that order,
     re-present a passage as a purposeful component when the content has
     that logical shape — a requirements sentence-list SHOULD become a
     checkmark list; "if X then Y because Z" scenario paragraphs SHOULD
     become answer cards (condition → outcome → original rationale as a
     footnote). The component must carry the source's words, not replace
     them with new ones.
   - When the content forms a decision tree (two or more branching if/then
     scenarios, e.g. married vs divorced → filing status → outcome), ALSO
     append ONE interactive quick-check widget (see recipe) after those
     scenarios. It must be derived 100% from the source scenarios and
     reuse their exact wording in results — it AUGMENTS the prose, never
     replaces it. Only skip it when the content has no branching logic.
   - Professional and restrained: no decoration for its own sake, but do
     use the named components above wherever the content shape calls for
     them.
   - Layout robustness: body text is always left-aligned (text-align:
     center only for standalone headings/buttons inside cards). For
     term + description items, put the bold term on its own line with the
     description in a normal paragraph BELOW it — never a side-by-side
     fixed-width label column, never a two-column definition grid. Avoid
     CSS grid/flex layouts for prose; reserve flex rows for icon + single
     line pairs.
9. Keep hyperlinks with their original href values. Drop share buttons,
   author bylines, comment sections, category/tag chrome, "related posts",
   newsletter signups, and any navigation — this block is article content
   only, the app provides the page chrome (title is shown by the app too, so
   do not repeat the page H1 verbatim at the top; start with the content).
10. Icons: allowed ONLY when they carry meaning (a checkmark on a
    requirements list, one-person vs two-person glyphs answering "who?"),
    never as decoration. Use Font Awesome — include
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.0/css/all.min.css" />
    as the first line inside the root div. Prefer outline variants
    (fa-regular); use fa-solid only when no outline variant exists. Color
    icons Green #6B9D81, Teal #4F788D, or Forest #2E5E4A. A page with zero
    icons is fine.

Apply the College Money Method design guidelines below to every visual
decision (typography, palette, spacing, callouts, tables).

## Structural skeleton (follow this shape)

<div class="cmm-article">
<style>
@import url('https://fonts.googleapis.com/css2?family=Lora:wght@400;500;600&family=Inter:wght@400;500;600&display=swap');
.cmm-article { /* CSS custom properties from the guidelines, font-family,
   color, line-height — NO padding, NO margin, NO background, NO max-width */ }
.cmm-article h2 { /* Lora 500, 1.75rem, Teal #4F788D, 3rem top margin */ }
.cmm-article h3 { /* Lora 400, 1.375rem, Forest #2E5E4A */ }
.cmm-article p  { /* Inter 400, 1rem, Navy Ink #1E3A5F, 1.6 line-height */ }
/* ...every rule scoped under .cmm-article... */
</style>
<!-- content: headings, paragraphs, components below -->
</div>

## Component recipes

Apply a recipe only when the content has the corresponding shape — never
force one onto plain prose.

- **Tip callout** (asides, "good to know", reminders): 4px Flax #F7DA7A
  left border, rgba(247,218,122,.12) background, border-radius 0 6px 6px 0,
  1rem 1.25rem padding, 0.95rem text.
- **Action callout** (the source tells the reader to go do something, with
  a link): same shape with Green #6B9D81 left border and
  rgba(107,157,129,.08) background; the link in Forest #2E5E4A, 600 weight.
- **Info box** (source has a highlighted/boxed note): rounded 8px, Sea
  Glass rgba(176,200,192,.12) background with full-strength border or left
  border, 1.5rem padding.
- **Checkmark list** (a list of requirements/benefits): list-style none;
  each li display:flex with a Green fa-check icon, 0.75rem gap.
- **Answer card** (scenario paragraphs of the form condition → outcome →
  reasoning): Sea Glass rgba(176,200,192,.08) background, 1.5px Sea Glass
  border, 8px radius, 1.25rem 1.5rem padding. Inside: condition as a Lora
  1.4rem Teal label; outcome as a flex row with a meaningful icon and the
  outcome sentence in Lora 1.4rem Navy Ink; the source's reasoning
  sentences as a 0.85rem italic footnote separated by a dashed Sea Glass
  top border.
- **Quick-check wizard** (optional, decision-tree content only): a bordered
  Sea Glass panel with a Lora title like "Quick Check: ...". Steps are
  divs toggled via the cmm-active class using ONLY inline onclick handlers
  (no <script>). Pill-shaped white buttons with 1.5px Teal border; result
  shows outcome pills (icon + label) and the source's exact rationale
  text, plus an underlined "Start over" text button. Give every id a
  unique prefix derived from the page slug to avoid collisions.
- **Numbered list** (source has one): standard styled <ol>; numbered
  circles in Green #6B9D81 with white numerals acceptable for top-level
  process steps.
- **Alert emphasis** (source flags something as important/warning): Flax
  background as tip callout; Coral Red #D4604A text/border only for
  genuine alerts and deadlines.
- **Tables** (source has one): header row Teal #4F788D (or Forest)
  background with white Inter 600 text; alternating body rows in
  rgba(176,200,192,.10); 0.75rem cell padding; wrapped in
  <div style="overflow-x:auto">.
- **Formula / emphasis block** (source presents a formula as a distinct
  visual block): Forest #2E5E4A solid background, white Lora text,
  centered, 8px radius.
- **Buttons/CTA links** (source has a button): Green #6B9D81 background,
  white text, 6px radius, 0.75rem × 1.5rem padding, Inter 500. Secondary:
  Forest #2E5E4A.
- **Video embed** (source has one): 16:9 responsive wrapper —
  position:relative; padding-bottom:56.25%; height:0; the iframe
  position:absolute inset 0, width/height 100%, border 0, radius 8px.

Rhythm: 3rem between sections, 1rem heading→body, 1.5rem between paragraphs,
0.75rem between list items. Generous whitespace; warm, approachable,
professional — a trusted advisor, never a sales pitch.
"""

USER_TEMPLATE = """\
Regenerate the following WordPress article as a CMM-branded HTML block per the
output contract.

Page title: {title}
Source URL: {url}

Source page main content (HTML):

{html}
"""


def load_guidelines(path: Path) -> str:
    guidelines = path.read_text()
    # Drop the YAML frontmatter — it's skill metadata, not design content
    guidelines = re.sub(r"\A---\n.*?\n---\n", "", guidelines, flags=re.DOTALL)
    return guidelines


def build_system_blocks(guidelines: str) -> list[dict]:
    """Static system prompt: instructions + design guidelines.
    cache_control on the last block caches the whole prefix across assets."""
    return [
        {"type": "text", "text": INSTRUCTIONS},
        {
            "type": "text",
            "text": "# CMM Design Guidelines\n\n" + guidelines,
            "cache_control": {"type": "ephemeral"},
        },
    ]


# ── Claude via Bedrock ────────────────────────────────────────────────────────

def generate_html(
    client,
    model: str,
    system_blocks: list[dict],
    title: str,
    url: str,
    source_html: str,
) -> tuple[str, dict]:
    """Stream a regeneration from Claude. Returns (html_fragment, usage_dict)."""
    with client.messages.stream(
        model=model,
        max_tokens=32000,
        system=system_blocks,
        messages=[
            {
                "role": "user",
                "content": USER_TEMPLATE.format(title=title, url=url, html=source_html),
            }
        ],
    ) as stream:
        message = stream.get_final_message()

    raw = "".join(b.text for b in message.content if b.type == "text").strip()
    # Strip markdown fences if the model added them despite instructions
    fence = re.match(r"\A```(?:html)?\s*\n(.*)\n```\s*\Z", raw, re.DOTALL)
    if fence:
        raw = fence.group(1).strip()

    usage = {
        "input": message.usage.input_tokens,
        "output": message.usage.output_tokens,
        "cache_write": getattr(message.usage, "cache_creation_input_tokens", None),
        "cache_read": getattr(message.usage, "cache_read_input_tokens", None),
    }
    return raw, usage


def wrap_tiptap(html_fragment: str) -> str:
    """Wrap the fragment in the Tiptap doc shape content-renderer.tsx expects."""
    return json.dumps(
        {"type": "doc", "content": [{"type": "rawHtml", "attrs": {"html": html_fragment}}]}
    )


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(description="Regenerate content from WP source with Claude/Bedrock")
    parser.add_argument("--asset-id", help="Regenerate a single asset by UUID")
    parser.add_argument("--name", help="Filter assets by name substring (ILIKE)")
    parser.add_argument("--limit", type=int, default=0, help="Max assets to process")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--aws-region", default=os.environ.get("AWS_REGION", DEFAULT_REGION))
    parser.add_argument("--aws-profile", default=None, help="AWS profile (e.g. cmm); default: env creds")
    parser.add_argument("--guidelines", type=Path, default=DEFAULT_GUIDELINES)
    parser.add_argument("--out-dir", type=Path, help="Write generated HTML fragments here for preview")
    parser.add_argument("--apply", action="store_true", help="Write regenerated content to the DB")
    args = parser.parse_args()

    from anthropic import AnthropicBedrock

    client = AnthropicBedrock(aws_region=args.aws_region, aws_profile=args.aws_profile)
    system_blocks = build_system_blocks(load_guidelines(args.guidelines))

    where = ["wp_source_url IS NOT NULL"]
    params: dict = {}
    if args.asset_id:
        where.append("id = :id")
        params["id"] = args.asset_id
    if args.name:
        where.append("name ILIKE :name")
        params["name"] = f"%{args.name}%"

    engine = get_engine()
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                f"SELECT id::text, name, wp_source_url FROM content_assets "
                f"WHERE {' AND '.join(where)} ORDER BY name"
                + (f" LIMIT {args.limit}" if args.limit else "")
            ),
            params,
        ).fetchall()

    print(f"Found {len(rows)} asset(s) to regenerate (model: {args.model})\n")
    if args.out_dir:
        args.out_dir.mkdir(parents=True, exist_ok=True)

    ok = failed = 0
    # Pages fetched once per URL; duplicate assets share the generated HTML
    generated_by_url: dict[str, str] = {}

    for asset_id, name, url in rows:
        print(f"▶ {name}\n  {url}")
        try:
            if url in generated_by_url:
                html_fragment = generated_by_url[url]
                print("  reusing HTML generated for the same URL")
            else:
                page = fetch_page(url)
                source = extract_main_content(page)
                print(f"  source extracted: {len(source)} chars — generating ...", flush=True)
                html_fragment, usage = generate_html(
                    client, args.model, system_blocks, name, url, source
                )
                generated_by_url[url] = html_fragment
                print(
                    f"  generated {len(html_fragment)} chars "
                    f"(in={usage['input']}, out={usage['output']}, "
                    f"cache_write={usage['cache_write']}, cache_read={usage['cache_read']})"
                )
        except Exception as e:  # noqa: BLE001
            print(f"  [error] {e}\n")
            failed += 1
            continue

        if args.out_dir:
            out_path = args.out_dir / f"{asset_id}.html"
            out_path.write_text(html_fragment)
            print(f"  preview → {out_path}")

        if args.apply:
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "UPDATE content_assets SET content = :c, updated_at = now() WHERE id = :id"
                    ),
                    {"c": wrap_tiptap(html_fragment), "id": asset_id},
                )
            print("  ✓ written to DB")
        else:
            print("  (dry run — DB not modified)")
        print()
        ok += 1

    print(f"Done: {ok} generated, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
