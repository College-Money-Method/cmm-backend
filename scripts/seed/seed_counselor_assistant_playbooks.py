"""Save the counselor assistant's playbooks as team-guidance sections.

Reads ``scripts/assistant_eval/counselor_playbooks.json`` (the file the eval
grades with ``--guidance-file``) and creates or updates one counselor-audience
prompt section per entry, matched by title. Sections stay editable in
Admin -> Assistant afterwards; re-running overwrites their body with the file's.

Usage (from the repo root; dry run unless --apply):
    uv run python scripts/seed/seed_counselor_assistant_playbooks.py
    uv run python scripts/seed/seed_counselor_assistant_playbooks.py --apply
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy import func, select

import src.db.models  # noqa: F401 - registers every mapper before the first query
from src.assistant.models import AssistantPromptSection
from src.db.base import get_session_factory

PLAYBOOKS = PROJECT_ROOT / "scripts" / "assistant_eval" / "counselor_playbooks.json"
AUDIENCE = "counselor"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true", help="write the sections (default: dry run)")
    parser.add_argument("--file", type=Path, default=PLAYBOOKS)
    args = parser.parse_args()

    entries = json.loads(args.file.read_text())
    with get_session_factory()() as db:
        # Sections go live in every school's counselor chat as soon as they are applied.
        print(f"Database: {db.get_bind().url.database} (live for all schools once applied)")
        for entry in entries:
            title, body = entry["title"].strip(), entry["body"].strip()
            row = db.scalars(
                select(AssistantPromptSection).where(
                    func.lower(func.trim(AssistantPromptSection.title)) == title.lower(),
                    AssistantPromptSection.audience == AUDIENCE,
                )
            ).first()
            if row is None:
                print(f"create  {title} ({len(body)} chars)")
                db.add(AssistantPromptSection(title=title, body=body, audience=AUDIENCE, enabled=True))
            elif row.body.strip() != body:
                print(f"update  {title} ({len(row.body)} -> {len(body)} chars)")
                row.body, row.updated_at = body, datetime.now(UTC)
            else:
                print(f"same    {title}")
        if args.apply:
            db.commit()
            print("Applied.")
        else:
            db.rollback()
            print("Dry run: nothing written. Re-run with --apply.")


if __name__ == "__main__":
    main()
