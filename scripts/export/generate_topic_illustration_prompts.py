"""
Generate one standalone CMM brand illustration prompt per topic, from the PROD database.

Reads `scripts/input/cmm-topic-illustration-prompt-template.md` and fills it per topic:

    {{SUBJECT}}       topic.title
    {{CONCEPT}}       topic.description + topic.summary_items (key takeaways), verbatim
    {{SCENE}}         concrete art direction — the actual object to draw
    {{DESK_OBJECTS}}  2–3 contextual props
    {{LABELS}}        Pass 2 type-overlay labels
    {{SCRIPT_NOTE}}   handwritten sticker phrase
    {{PANEL_TINT}}    rotated pale blue -> peach -> cream -> sage so adjacent topics differ
    {{ASPECT_RATIO}}  3:2 for topic cards (override with --aspect-ratio)

SCENE / DESK_OBJECTS / LABELS / SCRIPT_NOTE are art-directed per topic by an LLM
(same provider pattern as scripts/import_ingest/import_topics_from_google_docs.py).
Without them every prompt is identical apart from the title, and the image model has
no visual metaphor to work from. Use --provider none to emit direction placeholders
for a human to fill instead.

Each output file is self-contained: Pass 1 prompt + Pass 2 label spec + brand tokens,
with no cross-references back to the template.

Target DB: PROD, read-only. DATABASE_URL comes from `.env.prod` unless overridden.

Usage:
    uv run python -m scripts.export.generate_topic_illustration_prompts --dry-run
    uv run python -m scripts.export.generate_topic_illustration_prompts
    uv run python -m scripts.export.generate_topic_illustration_prompts --slug real-cost-of-college
    uv run python -m scripts.export.generate_topic_illustration_prompts --provider none
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from html import unescape
from pathlib import Path

import requests
from dotenv import dotenv_values
from sqlalchemy import text

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

from src.db.base import get_engine  # noqa: E402

PROJECT_ROOT = Path(__file__).parent.parent.parent
TEMPLATE_PATH = PROJECT_ROOT / "scripts" / "input" / "cmm-topic-illustration-prompt-template.md"
OUTPUT_DIR = PROJECT_ROOT / "scripts" / "output" / "topic_illustration_prompts"

BLOCKS = {
    "pass1": ("<!-- PROMPT_TEMPLATE_START -->", "<!-- PROMPT_TEMPLATE_END -->"),
    "pass2": ("<!-- PASS2_TEMPLATE_START -->", "<!-- PASS2_TEMPLATE_END -->"),
}

# Rotated so neighbouring cards in a topic list never share a panel colour. Wording,
# not hex — image models respond to colour names; exact values live in the Pass 2 table.
#
# All four are COOL. Warm tints (peach, cream) render as a yellow wash that fights the
# muted cool palette and reads as a different brand. The warm accents belong on objects
# inside the scene, never on the panel behind them.
PANEL_TINTS = ["cool off-white", "pale blue-grey", "soft sea-glass", "pale sage-grey"]

TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"\s+")

ART_DIRECTOR_SYSTEM = """You are the art director for College Money Method, a warm, \
editorial college-financial-planning brand for parents and teenagers.

Given an article title and what it teaches, direct ONE illustration.

Rules:
- YOU ARE NOT ILLUSTRATING EVERY FACT. Pick the single most important idea in the article \
and draw only that. The article text is background for choosing well, not a checklist to \
depict. Most of what you are told will not appear in the picture, and that is correct.
- Never try to show each item in a list. Enumerated details are added later as text labels \
placed over the finished art, so the drawing does not need to contain them. Attempting to \
fit every listed item into one picture is what produces a container full of labelled \
objects — a bag, box, tray, fridge, backpack or shelf holding one of each thing. That is \
the single most common failure here. Do not do it.
- Choose one clear focal subject, invented fresh from what THIS topic teaches. Do not reach \
for a stock visual metaphor or one commonly associated with the subject.
- NEVER people, faces, hands, or body parts. Tell the story through objects only.
- Prefer everyday household/desk/school objects. Avoid abstract shapes, arrows-as-subject, \
logos, currency symbols floating alone, or anything requiring readable text to make sense.
- LEGIBILITY IS THE HARD CONSTRAINT. The final art is displayed 350px wide. Use ONE bold \
silhouette with large, simple, well-separated shapes. Aim for 5 or fewer distinct elements \
in the whole scene. No crumbs, wrappers, scattered papers, fine texture, or small fiddly \
details — they turn to mud at thumbnail size.
- Do not restate the title. Describe what a person would SEE.
- Write plainly. Do not pad objects with filler adjectives ("sturdy", "classic", "neat", \
"large"); name the object and its arrangement. Vary how you open the description — these \
scenes are read side by side and formulaic phrasing makes 36 illustrations feel identical.

Return:
- core_idea: FIRST, before designing anything, distil the article to ONE relationship in at \
most 12 words. It must contain NO lists, NO commas separating items, and NO enumeration of \
components. Write it as a comparison or a change: "X is far bigger than it appears", \
"X shrinks after Y is applied", "X must happen before Y". This single sentence — not the \
article — is what you will illustrate.
- scene: 2-3 sentences describing the central object and its arrangement, in plain visual \
language an image model can follow. Present tense, concrete nouns. It must depict core_idea \
and nothing else. If your scene contains more than two distinct kinds of object, you have \
drifted back to illustrating the article — start over.
- desk_objects: EXACTLY 2 simple contextual props resting nearby, each a single clean shape \
(e.g. "a steaming coffee mug", "a chunky yellow pencil", "a small potted plant"). Never a \
prop made of many small parts.
- labels: 3-6 SHORT labels to be typeset over the finished art later. Uppercase. Each must \
name a FINANCIAL CONCEPT the illustration is teaching (TUITION, NET PRICE, GRANT, INTEREST, \
DEADLINE) — never the drawn prop itself. "CALCULATOR" or "PENCIL" is always wrong; the label \
carries the idea, the drawing carries the object. Use " · " to join enumerated lists inside \
one label. No sentences.
- script_note: a short lowercase handwritten aside in the brand's voice, 2-5 words, \
encouraging and plain (e.g. "let's break it down", "worth a second look")."""

ART_DIRECTOR_SCHEMA = {
    "name": "illustration_direction",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            # Ordered first deliberately: the model emits fields in schema order, so
            # distilling the idea happens BEFORE it designs a scene. Asking for the
            # scene first lets the article's enumerated lists drive the composition.
            "core_idea": {"type": "string"},
            "scene": {"type": "string"},
            "desk_objects": {"type": "array", "items": {"type": "string"}},
            "labels": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "text": {"type": "string"},
                        "placement": {"type": "string"},
                    },
                    "required": ["text", "placement"],
                    "additionalProperties": False,
                },
            },
            "script_note": {"type": "string"},
        },
        "required": ["core_idea", "scene", "desk_objects", "labels", "script_note"],
        "additionalProperties": False,
    },
}

PLACEHOLDER_DIRECTION = {
    "core_idea": "[TODO — state the one relationship this illustration should show]",
    "scene": "[TODO — art direction needed: describe the single central object that carries this idea]",
    "desk_objects": ["a steaming coffee mug", "a chunky yellow pencil", "a folded sticky note"],
    "labels": [],
    "script_note": "let's break it down",
}


def resolve_database_url(cli_url: str | None, env_file: str) -> str:
    """PROD url precedence: --database-url > env DATABASE_URL > <env_file>."""
    if cli_url:
        return cli_url
    if os.environ.get("DATABASE_URL"):
        return os.environ["DATABASE_URL"]
    url = dotenv_values(PROJECT_ROOT / env_file).get("DATABASE_URL")
    if not url:
        raise SystemExit(f"No DATABASE_URL found (checked --database-url, env, {env_file}).")
    return url


def load_block(raw: str, key: str) -> str:
    start, end = BLOCKS[key]
    try:
        return raw.split(start, 1)[1].split(end, 1)[0].strip()
    except IndexError:
        raise SystemExit(f"Template missing {start} / {end}: {TEMPLATE_PATH}")


def clean(value: str | None) -> str:
    """Strip stray markup/entities and collapse whitespace to one prompt-safe line."""
    if not value:
        return ""
    return WS_RE.sub(" ", unescape(TAG_RE.sub(" ", value))).strip()


def tiptap_text(node) -> str:
    """Recursively concatenate every text node in a TipTap/ProseMirror doc."""
    if isinstance(node, dict):
        own = node.get("text", "") if node.get("type") == "text" else ""
        return own + "".join(tiptap_text(c) for c in node.get("content", []) or [])
    if isinstance(node, list):
        return "".join(tiptap_text(c) for c in node)
    return ""


def takeaway_text(item) -> str:
    """
    Normalize one `summary_items` entry to plain text.

    The column is typed `list[str]`, but prod stores each entry as a JSON-encoded
    TipTap doc (e.g. '{"type":"doc","content":[...]}'). Feeding that raw to an image
    model injects literal JSON into the prompt — and an empty doc yields a 67-char
    string of pure punctuation. So: parse TipTap when present, fall back to the
    plain string, and return "" for an empty doc so the caller drops it.
    """
    if isinstance(item, dict):
        return clean(tiptap_text(item))
    if not isinstance(item, str):
        return ""
    stripped = item.strip()
    if stripped.startswith("{") or stripped.startswith("["):
        try:
            return clean(tiptap_text(json.loads(stripped)))
        except json.JSONDecodeError:
            pass
    return clean(item)


def build_concept(description: str | None, summary_items: list | None, max_chars: int) -> str:
    """
    Join description + every key takeaway into the concept text.

    Untruncated by default: the concept is what makes the prompt topic-specific, and
    cutting it mid-sentence drops the very details the illustration should convey.
    `max_chars` (0 = unlimited) trims on a sentence boundary when a cap is wanted.
    """
    parts: list[str] = []
    desc = clean(description)
    if desc:
        parts.append(desc)

    items = [t for t in (takeaway_text(i) for i in (summary_items or [])) if t]
    if items:
        parts.append("Key takeaways: " + " ".join(
            t if t.endswith((".", "!", "?")) else f"{t}." for t in items
        ))

    concept = " ".join(parts).strip()
    if max_chars and len(concept) > max_chars:
        head = concept[:max_chars]
        cut = max(head.rfind(". "), head.rfind("! "), head.rfind("? "))
        concept = head[: cut + 1] if cut > 0 else head.rsplit(" ", 1)[0] + "…"
    return concept


def direct_art(title: str, concept: str, model: str, api_key: str, pinned_scene: str | None = None) -> dict:
    """Ask the LLM for the per-topic scene, props, labels and sticker note."""
    user_msg = f"Title: {title}\n\nWhat it teaches: {concept or '(no description available)'}"
    if pinned_scene:
        user_msg += (
            f"\n\nThe central metaphor is already decided — use it verbatim as `scene`, "
            f"and direct the props, labels and note to suit it:\n{pinned_scene}"
        )
    resp = requests.post(
        "https://api.openai.com/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={
            "model": model,
            # 0.4 made 34 of 36 scenes open with the same adjective and pulled repeatedly
            # toward the same container metaphor. Higher temperature buys visual variety
            # across the library, which matters more here than determinism.
            "temperature": 0.8,
            "messages": [
                {"role": "system", "content": ART_DIRECTOR_SYSTEM},
                {"role": "user", "content": user_msg},
            ],
            "response_format": {"type": "json_schema", "json_schema": ART_DIRECTOR_SCHEMA},
        },
        timeout=120,
    )
    resp.raise_for_status()
    return json.loads(resp.json()["choices"][0]["message"]["content"])


def fill(block: str, values: dict[str, str]) -> str:
    for key, val in values.items():
        block = block.replace("{{" + key + "}}", val)
    return block


def build_file(row, pass1: str, pass2: str, tint: str, aspect: str, direction: dict) -> str:
    """Assemble the standalone per-topic file."""
    labels = direction.get("labels") or []
    label_lines = "\n".join(f"- `{l['text']}` — {l['placement']}" for l in labels) or \
        "- (none — add labels naming the key parts of the scene)"
    return (
        f"# {row['title']}\n\n"
        f"- **Slug:** `{row['slug']}`\n"
        f"- **Goal:** {row['goal_title'] or '—'}\n"
        f"- **Status:** {row['status']}\n"
        f"- **Panel tint:** {tint}  ·  **Aspect ratio:** {aspect}\n"
        f"- **Core idea being illustrated:** {direction.get('core_idea', '—')}\n"
        f"- **Save master as:** `public/illustrations/cmm-{row['slug']}.png`\n\n"
        "Generate textless, then apply the Pass 2 labels below. Style comes from the written\n"
        "prompt alone — do not feed existing CMM artwork in as a style reference.\n\n"
        "---\n\n## Pass 1 — paste this into the image model\n\n"
        f"{pass1}\n\n"
        "---\n\n## Pass 2 — type overlay\n\n"
        f"{fill(pass2, {'LABELS': label_lines, 'SCRIPT_NOTE': direction.get('script_note', '')})}\n"
    )


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--database-url", help="Override the DB URL (defaults to .env.prod)")
    ap.add_argument("--env-file", default=".env.prod", help="Env file to read DATABASE_URL from")
    ap.add_argument("--status", default="published", help="Topic status filter, or 'all' (default: published)")
    ap.add_argument("--slug", action="append", help="Only this slug (repeatable)")
    ap.add_argument("--limit", type=int, help="Cap the number of topics processed")
    ap.add_argument("--aspect-ratio", default="3:2", help="Aspect ratio for the prompt (default: 3:2)")
    ap.add_argument("--max-concept-chars", type=int, default=0, help="Trim CONCEPT at a sentence boundary (0 = no limit)")
    ap.add_argument("--provider", choices=["auto", "openai", "none"], default="auto",
                    help="LLM used for per-topic art direction (default: auto)")
    ap.add_argument("--model", default="gpt-4.1", help="LLM model (default: gpt-4.1)")
    ap.add_argument("--scene", help="Pin the central metaphor by hand instead of asking the LLM "
                                    "(requires a single --slug; labels/props are still directed for it)")
    ap.add_argument("--skip-existing", action="store_true", help="Leave already-generated files untouched")
    ap.add_argument("--dry-run", action="store_true", help="Print a summary; write nothing, call no LLM")
    args = ap.parse_args()

    if args.scene and len(args.slug or []) != 1:
        raise SystemExit("--scene applies to one topic; pass exactly one --slug.")

    raw_template = TEMPLATE_PATH.read_text(encoding="utf-8") if TEMPLATE_PATH.exists() else None
    if raw_template is None:
        raise SystemExit(f"Template not found: {TEMPLATE_PATH}")
    pass1_tpl, pass2_tpl = load_block(raw_template, "pass1"), load_block(raw_template, "pass2")

    api_key = os.environ.get("OPENAI_API_KEY") or dotenv_values(PROJECT_ROOT / args.env_file).get("OPENAI_API_KEY")
    provider = args.provider
    if provider == "auto":
        provider = "openai" if api_key else "none"
    if provider == "openai" and not api_key:
        raise SystemExit("OPENAI_API_KEY is required when --provider openai.")

    params = {}
    clause = ""
    if args.status != "all":
        clause = "WHERE t.status = :status"
        params["status"] = args.status

    with get_engine(resolve_database_url(args.database_url, args.env_file)).connect() as conn:
        all_rows = conn.execute(text(f"""
            SELECT t.id, t.title, t.slug, t.description, t.summary_items,
                   t.status, t.image_url, g.name AS goal_title
            FROM topics t
            LEFT JOIN goals g ON g.id = t.goal_id
            {clause}
            ORDER BY g.sort_order NULLS LAST, t.sort_order, t.title
        """), params).mappings().all()

    # Tint is assigned over the FULL ordered list, then rows are filtered. Indexing the
    # filtered set instead would hand a topic a different tint on a --slug re-run than
    # it got in the full run, breaking the alternation it was spaced against.
    tint_by_slug = {r["slug"]: PANEL_TINTS[i % len(PANEL_TINTS)] for i, r in enumerate(all_rows)}

    rows = [r for r in all_rows if r["slug"] in set(args.slug)] if args.slug else list(all_rows)
    if args.limit:
        rows = rows[: args.limit]
    if not rows:
        raise SystemExit("No topics matched. Check --status / --slug.")

    print(f"Fetched {len(rows)} topics from {args.env_file}. Art direction: {provider}"
          f"{'/' + args.model if provider != 'none' else ''}")
    if not args.dry_run:
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    manifest, no_concept, failed = [], [], []

    for idx, row in enumerate(rows):
        out_path = OUTPUT_DIR / f"{row['slug']}.md"
        if args.skip_existing and out_path.exists():
            print(f"  [{idx + 1}/{len(rows)}] {row['slug']} — exists, skipped")
            continue

        concept = build_concept(row["description"], row["summary_items"], args.max_concept_chars)
        tint = tint_by_slug[row["slug"]]
        if not concept:
            no_concept.append(row["slug"])

        direction = dict(PLACEHOLDER_DIRECTION)
        if provider == "openai" and not args.dry_run:
            print(f"  [{idx + 1}/{len(rows)}] {row['slug']} — directing …", end="", flush=True)
            try:
                direction = direct_art(row["title"], concept, args.model, api_key, args.scene)
                print(" ok")
            except Exception as exc:  # noqa: BLE001 — one topic failing must not lose the rest
                failed.append(row["slug"])
                print(f" FAILED ({type(exc).__name__}) — placeholder direction used")

        pass1 = fill(pass1_tpl, {
            "SUBJECT": clean(row["title"]),
            "CONCEPT": concept or "(no description available — see the title)",
            "SCENE": direction["scene"],
            "DESK_OBJECTS": ", ".join(direction["desk_objects"]),
            "PANEL_TINT": tint,
            "ASPECT_RATIO": args.aspect_ratio,
        })

        if not args.dry_run:
            out_path.write_text(build_file(row, pass1, pass2_tpl, tint, args.aspect_ratio, direction), encoding="utf-8")

        manifest.append({
            "id": str(row["id"]), "title": row["title"], "slug": row["slug"],
            "goal": row["goal_title"], "status": row["status"],
            "panel_tint": tint, "aspect_ratio": args.aspect_ratio,
            "concept_chars": len(concept),
            "takeaways": sum(1 for i in (row["summary_items"] or []) if takeaway_text(i)),
            "core_idea": direction.get("core_idea", ""),
            "scene": direction["scene"],
            "labels": [l["text"] for l in direction.get("labels", [])],
            "script_note": direction.get("script_note", ""),
            "has_existing_image": bool(row["image_url"]),
            "prompt_file": f"{row['slug']}.md",
        })

    if args.dry_run:
        for m in manifest:
            print(f"  {m['slug']:<50} tint={m['panel_tint']:<11} concept={m['concept_chars']:>4}ch "
                  f"takeaways={m['takeaways']}")
        print(f"\nDry run — nothing written, no LLM calls. Would write {len(manifest)} files to {OUTPUT_DIR}")
    else:
        # Merge into any existing manifest so a --slug/--limit run updates just those
        # entries instead of truncating the manifest to the topics it happened to touch.
        manifest_path = OUTPUT_DIR / "_manifest.json"
        merged: dict[str, dict] = {}
        if manifest_path.exists():
            try:
                merged = {m["slug"]: m for m in json.loads(manifest_path.read_text(encoding="utf-8"))}
            except (json.JSONDecodeError, KeyError, TypeError):
                pass  # unreadable manifest — rebuild from this run
        merged.update({m["slug"]: m for m in manifest})
        ordered = [merged[s] for s in tint_by_slug if s in merged]
        manifest_path.write_text(json.dumps(ordered, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Wrote {len(manifest)} prompt files; manifest now has {len(ordered)} entries -> {OUTPUT_DIR}")

    if already := sum(1 for m in manifest if m["has_existing_image"]):
        print(f"Note: {already} topic(s) already have an image_url — regenerating replaces artwork.")
    if no_concept:
        print(f"WARNING: no description or takeaways (title-only prompt): {', '.join(no_concept)}")
    if failed:
        print(f"WARNING: art direction failed, placeholder scene written: {', '.join(failed)}")


if __name__ == "__main__":
    main()
