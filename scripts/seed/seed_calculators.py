#!/usr/bin/env python3
"""Seed the embeddable calculators from authored files in the frontend repo.

The authored markup and its config live in the frontend repo
(``app/lib/calculators/<slug>/`` — ``calculator.html``, ``config.json``) because
that is where they are edited, linted, and self-tested. The authoring briefs live
here in ``scripts/seed/calculator-documentation/<slug>.md``: they are prose about
the database row, nothing in the frontend build reads them, and keeping them
beside this script means seeding a brief needs no second checkout.

This script is the one-way door that puts both in the database, which is the
runtime source of truth — after seeding, an admin editing a calculator in the CMS
wins until the next run.

Calculators with authored files are upserted: re-running picks up authoring
changes to their markup and data. Ones with no authored files yet are inserted
only if absent, so a stub someone has started authoring in the CMS is never
wiped by a re-seed.

The frontend repo is not a sibling of this one on every machine, so the source
path is never guessed in code: it comes from --source, or from CALCULATORS_SOURCE
in the --env-file that already decides which database is being written. Same rule
as the database itself — explicit, and from the environment you named.

Usage (from project root):
    # CALCULATORS_SOURCE=~/WebstormProjects/cmm-frontend/app/lib/calculators
    uv run --env-file .env.local python scripts/seed/seed_calculators.py
    uv run --env-file .env.dev python scripts/seed/seed_calculators.py \
        --source <path> --publish --dry-run
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# Authoring briefs, one ``<slug>.md`` per calculator. In this repo rather than
# behind --source, so a brief can be written and seeded without the frontend
# tree, and keyed by slug rather than by source dir so a stub with no authored
# markup can still carry one.
DOCUMENTATION_ROOT = Path(__file__).resolve().parent / "calculator-documentation"

# Where the authored markup and config are checked out. Read from the --env-file
# so the everyday invocation is just the script and an environment.
SOURCE_ENV_VAR = "CALCULATORS_SOURCE"

from sqlalchemy import select

from src.calculators.models import Calculator
from src.db.deps import get_db

# (slug, title, type, description, source_dir | None, upsert)
# source_dir None = no authored markup yet; seeded as an empty draft so the
# calculator exists to be authored against in the admin editor.
CALCULATORS = [
    (
        "fafsa-sai-2027-28",
        "FAFSA 2027-28 SAI Calculator",
        "fafsa_sai",
        "Estimate your Student Aid Index and Pell Grant eligibility for the "
        "2027-28 award year.",
        "fafsa-sai-2027-28",
        True,
    ),
    (
        "business-net-worth",
        "Net Worth of a Business",
        "business_net_worth",
        "See how much of a family business or farm is actually counted as a "
        "parent asset on the FAFSA.",
        "business-net-worth",
        True,
    ),
    (
        "application-assets",
        "Assets Calculator for Applications",
        "application_assets",
        "Work out what to enter on the investment lines of the FAFSA and the "
        "CSS Profile — the two applications want different totals.",
        "application-assets",
        True,
    ),
    (
        "student-borrowing-8-percent",
        "Student Borrowing: the 8% Rule",
        "student_borrowing_8_percent",
        "Start from the salary a major or career actually pays and work backwards "
        "to the most a student should borrow for the whole degree.",
        "student-borrowing-8-percent",
        True,
    ),
]


def load_source(source_root: Path, source_dir: str) -> tuple[str, dict]:
    """Read the authored markup and data for one calculator."""
    d = source_root / source_dir
    html_path, config_path = d / "calculator.html", d / "config.json"
    if not html_path.is_file():
        raise SystemExit(f"missing {html_path}")
    if not config_path.is_file():
        raise SystemExit(f"missing {config_path}")
    return html_path.read_text(), json.loads(config_path.read_text())


def load_documentation(slug: str) -> str:
    """Read the authoring brief the admin editor's Documentation tab shows.

    The brief covers how the calculator reads its data, the ``window.__CALC_*``
    contract it has to keep, and the self-test cases that lock its arithmetic.
    Optional — a calculator can be seeded before its brief is written — so a
    missing file is an empty string, not a failure.
    """
    path = DOCUMENTATION_ROOT / f"{slug}.md"
    return path.read_text() if path.is_file() else ""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source",
        default=os.environ.get(SOURCE_ENV_VAR),
        help=(
            "path to the frontend repo's app/lib/calculators directory "
            f"(default: ${SOURCE_ENV_VAR} from the --env-file)"
        ),
    )
    parser.add_argument("--dry-run", action="store_true", help="report without writing")
    parser.add_argument(
        "--publish",
        action="store_true",
        help="publish the calculators that have authored markup (embed 404s on drafts)",
    )
    args = parser.parse_args()

    if not args.source:
        raise SystemExit(
            f"no calculator source: pass --source, or set {SOURCE_ENV_VAR} in the "
            "env file passed to `uv run --env-file`"
        )

    source_root = Path(args.source).expanduser().resolve()
    if not source_root.is_dir():
        raise SystemExit(f"--source is not a directory: {source_root}")

    db_gen = get_db()
    db = next(db_gen)
    created = updated = skipped = 0

    try:
        for slug, title, ctype, description, source_dir, upsert in CALCULATORS:
            html, config = ("", {})
            if source_dir:
                html, config = load_source(source_root, source_dir)
            documentation = load_documentation(slug)

            # An empty calculator has nothing to serve, so --publish never
            # promotes a stub — publishing one would ship a blank embed.
            status = "published" if args.publish and source_dir else "draft"

            existing = db.execute(
                select(Calculator).where(Calculator.slug == slug)
            ).scalar_one_or_none()

            if existing and not upsert:
                print(f"  skip    {slug} (exists, insert-only)")
                skipped += 1
                continue

            if existing:
                print(
                    f"  update  {slug} "
                    f"({len(html)} chars of markup, {len(config)} config keys, "
                    f"{len(documentation)} chars of docs)"
                )
                updated += 1
                if not args.dry_run:
                    existing.title = title
                    existing.type = ctype
                    existing.description = description
                    # A stub row receiving its first authored markup is the one
                    # case --publish may promote. Status on a row that already
                    # had content belongs to whoever set it in the admin editor:
                    # re-seeding must never undo a deliberate unpublish.
                    if args.publish and source_dir and not existing.html:
                        existing.status = "published"
                    existing.html = html
                    existing.config = config
                    # Only overwritten when there is an authored brief to
                    # write: a calculator with no brief on disk must not wipe one
                    # an admin typed into the Documentation tab.
                    if documentation:
                        existing.documentation = documentation
                    existing.updated_at = datetime.now(timezone.utc)
            else:
                print(f"  create  {slug} [{ctype}] status={status}")
                created += 1
                if not args.dry_run:
                    db.add(
                        Calculator(
                            slug=slug,
                            title=title,
                            type=ctype,
                            description=description,
                            html=html,
                            deps="",
                            config=config,
                            documentation=documentation or None,
                            embed_allowed_origins=[],
                            status=status,
                        )
                    )

        if args.dry_run:
            print(f"\ndry run — would create {created}, update {updated}, skip {skipped}")
            db.rollback()
        else:
            db.commit()
            print(f"\ncreated {created}, updated {updated}, skipped {skipped}")
    finally:
        db.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
