#!/usr/bin/env python3
"""Upload topic artwork SVGs and grade hero banners to S3, then link them in the DB.

Handles two file patterns from --svg-dir:
  grade-{N}-hero.svg         → S3 upload + grade_configs.banner_image_url  (matched by grade number)
  grade-{N}-{title-slug}.svg → S3 upload + topics.image_url                (fuzzy-matched by title)

Usage:
    # Dry-run (default) — show proposed matches with confidence
    uv run python scripts/upload-topic-art-to-s3.py

    # Apply — upload to S3 and write DB changes
    uv run python scripts/upload-topic-art-to-s3.py --apply

    # Custom source directory
    uv run python scripts/upload-topic-art-to-s3.py --svg-dir /path/to/svgs --apply
"""
from __future__ import annotations

import argparse
import re
import sys
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

import boto3
from botocore.exceptions import BotoCoreError, ClientError

sys.path.insert(0, str(Path(__file__).parent.parent))

import src.assets.models  # noqa: F401
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
from src.config import settings
from src.content.models import GradeConfig, Goal, Topic
from src.db.base import get_session_factory

S3_PREFIX = "portal/assets/thumbnails"
MIN_MATCH_SCORE = 0.65

# Exact DB title overrides for slugs where fuzzy matching is unreliable
# (title uses &/vs/abbreviations/parenthetical suffixes that differ from slug).
TITLE_OVERRIDES: dict[str, str] = {
    "completing the income sections": "Completing the Income Sections on Your Applications",
    "completing the assets special circumstances sectio": "Completing the Assets & Special Circumstances Sections on Your Applications",
    # & vs and, (Scholarships) suffix, etc.
    "calculating student income and assets": "Calculating Student Income & Assets",
    "developing a merit aid strategy": "Developing a Merit Aid Strategy (Scholarships)",
    "loan repayment and management": "Loan Repayment & Management (Overview)",
    # Grade 12 "Putting Your Money to Work" — vs. vs vs, hyphens, etc.
    "applying for additional loans": "Applying for Additional Loans",
    "calculating your four year commitment": "Calculating Your Four-Year Commitment",
    "using college savings vs current income vs loans": "Using College Savings vs. Current Income vs. Loans",
    "utilizing tax strategies": "Utilizing Tax Strategies",
    "planning for contingencies": "Planning for Contingencies",
}

# Same SVG reused for multiple topics (e.g. overview vs. detail version of same topic).
# Key = primary DB title (from TITLE_OVERRIDES or fuzzy match); value = extra titles to also set.
ALSO_APPLY_TO: dict[str, list[str]] = {
    "Loan Repayment & Management (Overview)": ["Loan Repayment & Management"],
}


# ---------------------------------------------------------------------------
# S3
# ---------------------------------------------------------------------------

def s3_url(bucket: str, region: str, key: str) -> str:
    return f"https://{bucket}.s3.{region}.amazonaws.com/{key}"


def upload_svg(s3_client, bucket: str, region: str, svg_path: Path, key: str) -> str:
    s3_client.put_object(
        Bucket=bucket,
        Key=key,
        Body=svg_path.read_bytes(),
        ContentType="image/svg+xml",
    )
    return s3_url(bucket, region, key)


# ---------------------------------------------------------------------------
# Fuzzy matching
# ---------------------------------------------------------------------------

def slug_to_label(slug: str) -> str:
    """Convert a filename slug to a readable label for fuzzy matching.

    'grade-9-the-real-cost-of-college'       → 'the real cost of college'
    'grade-10-01-calculating-your-sai'        → 'calculating your sai'
    """
    label = re.sub(r"^grade-\d+-", "", slug)   # strip grade-N-
    label = re.sub(r"^\d{2}-", "", label)       # strip optional NN- seq prefix
    return label.replace("-", " ")


def similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()


def best_match(label: str, candidates: list[tuple[Any, str]]) -> tuple[Any, str, float]:
    best_rec, best_text, best_score = None, "", 0.0
    for rec, text in candidates:
        score = similarity(label, text)
        if score > best_score:
            best_rec, best_text, best_score = rec, text, score
    return best_rec, best_text, best_score


def score_badge(score: float) -> str:
    if score >= 0.85:
        return "✓ HIGH"
    if score >= 0.60:
        return "~ MED"
    return "✗ LOW"


# ---------------------------------------------------------------------------
# Matching logic
# ---------------------------------------------------------------------------

def parse_svgs(svg_dir: Path) -> tuple[list[tuple[int, Path]], list[tuple[int, str, Path]]]:
    """Return (hero_svgs, topic_svgs).

    hero_svgs:  list of (grade_num, path)
    topic_svgs: list of (grade_num, label, path)
    """
    hero_pattern = re.compile(r"^grade-(\d+)-hero\.svg$")
    topic_pattern = re.compile(r"^grade-(\d+)-(.+)\.svg$")
    heroes, topics = [], []
    for p in sorted(svg_dir.glob("*.svg")):
        if m := hero_pattern.match(p.name):
            heroes.append((int(m.group(1)), p))
        elif m := topic_pattern.match(p.name):
            grade_num = int(m.group(1))
            label = slug_to_label(p.stem)
            topics.append((grade_num, label, p))
    return heroes, topics


def match_grade_heroes(db, heroes: list[tuple[int, Path]]) -> list[dict]:
    configs = {gc.grade: gc for gc in db.query(GradeConfig).all()}
    results = []
    for grade_num, path in heroes:
        gc = configs.get(grade_num)
        results.append({
            "path": path,
            "grade": grade_num,
            "record": gc,
            "field": "banner_image_url",
            "current": gc.banner_image_url if gc else None,
            "score": 1.0,
            "matched_text": gc.label if gc else "—",
        })
    return results


def match_topic_artworks(db, topics: list[tuple[int, str, Path]]) -> list[dict]:
    from sqlalchemy.orm import joinedload
    all_topics = db.query(Topic).options(joinedload(Topic.goal)).all()
    # Prefer topics that have a goal assigned (avoid orphaned/draft duplicates)
    with_goal = [(t, t.title) for t in all_topics if t.goal_id is not None and t.title != "test"]
    all_candidates = [(t, t.title) for t in all_topics if t.title != "test"]

    title_map = {t.title: t for t in all_topics}
    results = []
    for grade_num, label, path in topics:
        if label in TITLE_OVERRIDES:
            exact_title = TITLE_OVERRIDES[label]
            rec = title_map.get(exact_title)
            results.append({"path": path, "grade": grade_num, "label": label,
                            "record": rec, "field": "image_url",
                            "current": rec.image_url if rec else None,
                            "score": 1.0 if rec else 0.0,
                            "matched_text": exact_title if rec else f"OVERRIDE NOT FOUND: {exact_title}"})
            continue

        candidates = with_goal if with_goal else all_candidates
        rec, matched, score = best_match(label, candidates)
        if score < MIN_MATCH_SCORE:
            rec2, matched2, score2 = best_match(label, all_candidates)
            if score2 > score:
                rec, matched, score = rec2, matched2, score2
        results.append({
            "path": path, "grade": grade_num, "label": label,
            "record": rec, "field": "image_url",
            "current": rec.image_url if rec else None,
            "score": score, "matched_text": matched,
        })
    return results


# ---------------------------------------------------------------------------
# Display
# ---------------------------------------------------------------------------

def print_hero_section(results: list[dict]) -> None:
    print(f"\n{'─' * 72}")
    print("  GRADE HEROES  →  grade_configs.banner_image_url")
    print(f"{'─' * 72}")
    for r in results:
        gc = r["record"]
        status = "✓" if gc else "✗ NOT FOUND"
        current = f"(already: {r['current'][:50]}…)" if r["current"] else "(empty)"
        print(f"  {status}  grade {r['grade']:2d}  →  {r['matched_text']}  {current}")
        print(f"           File: {r['path'].name}")
    print()


def print_topic_section(results: list[dict]) -> None:
    print(f"\n{'─' * 72}")
    print("  TOPIC ARTWORKS  →  topics.image_url")
    print(f"{'─' * 72}")
    for r in results:
        badge = score_badge(r["score"])
        skip = "  (SKIP – low score)" if r["score"] < MIN_MATCH_SCORE else ""
        current = "(already set)" if r["current"] else "(empty)"
        print(f"  {badge}  [{r['grade']}]  {r['label']}")
        print(f"         → DB title: {r['matched_text']!r}   score={r['score']:.2f}  {current}{skip}")
        for alias in ALSO_APPLY_TO.get(r["matched_text"], []):
            print(f"         → also: {alias!r}")
    print()


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------

def apply_all(db, s3_client, bucket: str, region: str,
              hero_results: list[dict], topic_results: list[dict]) -> None:
    title_map = {t.title: t for t in db.query(Topic).all()}
    uploaded = 0
    updated = 0

    for r in hero_results:
        if not r["record"]:
            print(f"  SKIP (no grade_config): grade {r['grade']}")
            continue
        key = f"{S3_PREFIX}/{r['path'].name}"
        try:
            url = upload_svg(s3_client, bucket, region, r["path"], key)
            print(f"  ↑ {r['path'].name} → {url}")
            uploaded += 1
        except (BotoCoreError, ClientError, OSError) as exc:
            print(f"  ✗ upload failed {r['path'].name}: {exc}", file=sys.stderr)
            sys.exit(1)
        r["record"].banner_image_url = url
        updated += 1

    for r in topic_results:
        if not r["record"] or r["score"] < MIN_MATCH_SCORE:
            print(f"  SKIP: {r['label']} (score={r['score']:.2f})")
            continue
        key = f"{S3_PREFIX}/{r['path'].name}"
        try:
            url = upload_svg(s3_client, bucket, region, r["path"], key)
            print(f"  ↑ {r['path'].name} → {url}")
            uploaded += 1
        except (BotoCoreError, ClientError, OSError) as exc:
            print(f"  ✗ upload failed {r['path'].name}: {exc}", file=sys.stderr)
            sys.exit(1)
        r["record"].image_url = url
        updated += 1

        # Propagate the same URL to aliased topics (same content, different title variant)
        for alias_title in ALSO_APPLY_TO.get(r["matched_text"], []):
            alias_rec = title_map.get(alias_title)
            if alias_rec:
                alias_rec.image_url = url
                updated += 1
                print(f"       ↳ also set → {alias_title!r}")
            else:
                print(f"       ↳ alias not found in DB: {alias_title!r}")

    db.commit()
    print(f"\nUploaded {uploaded} SVGs, updated {updated} DB records.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--svg-dir", default="/tmp/topic_svgs",
                        help="Directory containing grade-N-hero.svg and grade-N-*.svg files")
    parser.add_argument("--apply", action="store_true",
                        help="Upload to S3 and write DB changes (default: dry-run)")
    args = parser.parse_args()

    svg_dir = Path(args.svg_dir)
    if not svg_dir.exists():
        print(f"Error: --svg-dir {svg_dir} does not exist", file=sys.stderr)
        sys.exit(1)

    heroes, topics = parse_svgs(svg_dir)
    print(f"Found {len(heroes)} hero SVGs and {len(topics)} topic artwork SVGs in {svg_dir}")

    if not settings.s3_bucket_name:
        print("Error: s3_bucket_name not set in .env", file=sys.stderr)
        sys.exit(1)

    SessionFactory = get_session_factory()
    with SessionFactory() as db:
        hero_results = match_grade_heroes(db, heroes)
        topic_results = match_topic_artworks(db, topics)

        print_hero_section(hero_results)
        print_topic_section(topic_results)

        if args.apply:
            s3 = boto3.client(
                "s3",
                region_name=settings.aws_region,
                aws_access_key_id=settings.aws_access_key_id,
                aws_secret_access_key=settings.aws_secret_access_key,
            )
            print(f"\n{'─' * 72}")
            print("  APPLYING...")
            print(f"{'─' * 72}")
            apply_all(db, s3, settings.s3_bucket_name, settings.aws_region,
                      hero_results, topic_results)
        else:
            low = [r for r in topic_results if r["score"] < MIN_MATCH_SCORE]
            print(f"Dry-run complete. {len(low)} topic(s) below score threshold ({MIN_MATCH_SCORE}).")
            print("Pass --apply to upload to S3 and write DB changes.\n")


if __name__ == "__main__":
    main()
