#!/usr/bin/env python3
"""Match uploaded thumbnail SVGs to database records and optionally write them.

Mapping strategy
----------------
  goal-*.svg       → goals.icon_url            (fuzzy match on goals.name)
  topic-*.svg      → topics.image_url           (fuzzy match on topics.title)
  workshop-*.svg   → workshops.workshop_art_url  (fuzzy match on workshops.name)
  resource-*.svg   → asset_types.icon_url        (keyword match on name/display_bucket)
  grade-*.svg      → skipped (frontend static assets, no DB table)

Usage
-----
  # Dry-run — show proposed matches with confidence scores
  uv run python scripts/attach-thumbnails-to-db.py

  # Apply changes to the database
  uv run python scripts/attach-thumbnails-to-db.py --apply

  # Show asset_types table so you can tune resource mappings
  uv run python scripts/attach-thumbnails-to-db.py --list-asset-types
"""

from __future__ import annotations

import argparse
import sys
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.parent))

import src.assets.models  # noqa: F401 — register all ORM mappings before opening a session
import src.auth.models  # noqa: F401
import src.calendar.models  # noqa: F401
import src.content.models  # noqa: F401
import src.cycles.models  # noqa: F401
import src.guest_contacts.models  # noqa: F401
import src.meetings.models  # noqa: F401
import src.sales.models  # noqa: F401
import src.schools.models  # noqa: F401
import src.settings.models  # noqa: F401
import src.storage.models  # noqa: F401
import src.workshops.models  # noqa: F401
from src.db.base import get_session_factory
from src.content.models import AssetType, Goal, Topic
from src.workshops.models import Workshop

S3_BASE = "https://cmm-general.s3.us-east-1.amazonaws.com/portal/assets/thumbnails"

# Human-readable label for each slug, used as the fuzzy-match query string.
GOAL_LABELS: dict[str, str] = {
    "goal-understanding-financial-aid-basics": "Understanding Financial Aid Basics",
    "goal-creating-financial-game-plan": "Creating Your Financial Game Plan",
    "goal-assessing-aid-eligibility": "Assessing Aid Eligibility",
    "goal-exploring-borrowing-options": "Exploring Borrowing Options",
    "goal-building-college-list": "Building a College List for Your Budget",
    "goal-preparing-scholarship-applications": "Preparing Scholarship Applications",
    "goal-applying-for-need-based-aid": "Applying for Need-based Aid",
    "goal-appealing-financial-aid-offers": "Appealing Financial Aid Offers",
    "goal-putting-money-to-work": "Putting Your Money to Work",
}

TOPIC_LABELS: dict[str, str] = {
    "topic-real-cost": "Real Cost of College",
    "topic-types-of-aid": "Types of Financial Aid",
    "topic-need-based-aid": "How Need-Based Aid Is Awarded",
    "topic-merit-aid": "How Merit Aid Is Awarded",
}

WORKSHOP_LABELS: dict[str, str] = {
    "workshop-navigating-college-pricing": "Navigating New System of College Pricing and Aid",
    "workshop-how-colleges-assess-ability-to-pay": "How Colleges Assess Ability to Pay",
    "workshop-comparing-awards-and-appeals": "Comparing Awards and Strategies to Appeal",
    "workshop-building-award-generosity-into-school-list": "Building Award Generosity into School List",
    "workshop-evaluating-loans-and-borrowing": "Evaluating Loans and Borrowing",
    "workshop-succeeding-in-financial-aid-process": "Succeeding in the Financial Aid Process",
}

# Keywords used to match resource SVGs to asset_type records.
# Each value is a list of substrings checked against lowercased asset_type.name
# and asset_type.display_bucket.
RESOURCE_KEYWORDS: dict[str, list[str]] = {
    "resource-article": ["article"],
    "resource-calculator": ["calculator"],
    "resource-guide": ["guide"],
    "resource-worksheet": ["spreadsheet", "worksheet", "sheet"],
    "resource-email": ["email"],
    "resource-online": ["online"],
    "resource-video": ["video"],
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()


def _best_match(label: str, candidates: list[tuple[Any, str]]) -> tuple[Any, str, float]:
    """Return (record, matched_text, score) for the best candidate."""
    best_rec, best_text, best_score = None, "", 0.0
    for rec, text in candidates:
        score = _similarity(label, text)
        if score > best_score:
            best_rec, best_text, best_score = rec, text, score
    return best_rec, best_text, best_score


def _s3_url(slug: str) -> str:
    return f"{S3_BASE}/{slug}.svg"


def _score_badge(score: float) -> str:
    if score >= 0.85:
        return "✓ HIGH"
    if score >= 0.60:
        return "~ MED"
    return "✗ LOW"


# ---------------------------------------------------------------------------
# Matching logic
# ---------------------------------------------------------------------------

def match_goals(db) -> list[dict]:
    goals = db.query(Goal).all()
    candidates = [(g, g.name) for g in goals]
    results = []
    for slug, label in GOAL_LABELS.items():
        rec, matched, score = _best_match(label, candidates)
        results.append({
            "slug": slug, "label": label, "record": rec,
            "matched_text": matched, "score": score,
            "url_field": "icon_url",
        })
    return results


def match_topics(db) -> list[dict]:
    topics = db.query(Topic).all()
    candidates = [(t, t.title) for t in topics]
    results = []
    for slug, label in TOPIC_LABELS.items():
        rec, matched, score = _best_match(label, candidates)
        results.append({
            "slug": slug, "label": label, "record": rec,
            "matched_text": matched, "score": score,
            "url_field": "image_url",
        })
    return results


def match_workshops(db) -> list[dict]:
    workshops = db.query(Workshop).all()
    candidates = [(w, w.name) for w in workshops]
    results = []
    for slug, label in WORKSHOP_LABELS.items():
        rec, matched, score = _best_match(label, candidates)
        results.append({
            "slug": slug, "label": label, "record": rec,
            "matched_text": matched, "score": score,
            "url_field": "workshop_art_url",
        })
    return results


def match_resource_types(db) -> list[dict]:
    asset_types = db.query(AssetType).all()
    results = []
    for slug, keywords in RESOURCE_KEYWORDS.items():
        matched_type = None
        for at in asset_types:
            haystack = f"{(at.name or '').lower()} {(at.display_bucket or '').lower()}"
            if any(kw in haystack for kw in keywords):
                matched_type = at
                break
        results.append({
            "slug": slug,
            "record": matched_type,
            "matched_text": matched_type.name if matched_type else "—",
            "score": 1.0 if matched_type else 0.0,
            "url_field": "default_thumbnail_url",
        })
    return results


# ---------------------------------------------------------------------------
# Display
# ---------------------------------------------------------------------------

def print_section(title: str, rows: list[dict], is_resource: bool = False) -> None:
    print(f"\n{'─' * 70}")
    print(f"  {title}")
    print(f"{'─' * 70}")
    for r in rows:
        rec = r["record"]
        score = r["score"]
        badge = _score_badge(score) if not is_resource else ("✓ MATCH" if rec else "✗ NO MATCH")
        current = getattr(rec, r["url_field"], None) if rec else None
        current_label = "(already set)" if current else "(empty)"
        slug_label = r.get("label", r["slug"])
        print(f"  {badge}  {slug_label}")
        print(f"         → DB: {r['matched_text']!r:45s}  {'' if is_resource else f'score={score:.2f}'}")
        print(f"         → URL: {_s3_url(r['slug'])}")
        print(f"         → Field: {r['url_field']}  {current_label}")
        print()


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------

def apply_matches(db, all_matches: list[dict]) -> int:
    updated = 0
    for r in all_matches:
        rec = r["record"]
        if rec is None:
            continue
        if r.get("score", 1.0) < 0.50:
            print(f"  SKIP (low confidence): {r['slug']}")
            continue
        setattr(rec, r["url_field"], _s3_url(r["slug"]))
        updated += 1
    db.commit()
    return updated


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="Write changes to the database")
    parser.add_argument("--list-asset-types", action="store_true", help="Print all asset_types and exit")
    args = parser.parse_args()

    SessionFactory = get_session_factory()
    with SessionFactory() as db:
        if args.list_asset_types:
            types = db.query(AssetType).order_by(AssetType.name).all()
            print(f"\n{'─' * 60}")
            print("  asset_types table")
            print(f"{'─' * 60}")
            for at in types:
                print(f"  {at.name!r:30s}  bucket={at.display_bucket!r:10s}  is_tool={at.is_tool}")
            return

        goal_matches = match_goals(db)
        topic_matches = match_topics(db)
        workshop_matches = match_workshops(db)
        resource_matches = match_resource_types(db)

        print_section("GOALS  →  goals.icon_url", goal_matches)
        print_section("TOPICS  →  topics.image_url", topic_matches)
        print_section("WORKSHOPS  →  workshops.workshop_art_url", workshop_matches)
        print_section("RESOURCE TYPES  →  asset_types.default_thumbnail_url", resource_matches, is_resource=True)

        print(f"\n{'─' * 70}")
        print("  GRADE thumbnails (grade-9/10/11/12.svg) — no DB table.")
        print("  Use directly in the frontend by grade level.")
        print(f"{'─' * 70}\n")

        if args.apply:
            all_matches = goal_matches + topic_matches + workshop_matches + resource_matches
            count = apply_matches(db, all_matches)
            print(f"Applied {count} updates to the database.\n")
        else:
            print("Dry-run complete. Pass --apply to write changes.\n")


if __name__ == "__main__":
    main()
