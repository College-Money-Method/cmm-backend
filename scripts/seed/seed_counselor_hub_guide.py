"""Load the Counselor Hub user guide into the counselor assistant's knowledge.

Reads the guide's Markdown (kept in the frontend repo at
``docs/user-guides/counselor-hub-user-guide.md``), converts it to a Tiptap
document (``counselor_guide_markdown``) and creates or updates one resource:
counselor-only, published, CMM-authored, every school. Then it reindexes that
resource straight away, using the same hash as the nightly sync, so neither
redoes the other's work. Families never see it: it has no public page and
``for_family`` is off.

The Markdown file is the source of truth: re-running overwrites the resource's
text, including edits made in Admin -> Content.

Usage (from the repo root; dry run unless --apply):
    uv run python scripts/seed/seed_counselor_hub_guide.py --file ../../WebstormProjects/cmm-frontend/docs/user-guides/counselor-hub-user-guide.md
    uv run python scripts/seed/seed_counselor_hub_guide.py --file <path> --apply
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy import select

import src.db.models  # noqa: F401 - registers every mapper before the first query
from scripts.seed.counselor_guide_markdown import markdown_to_tiptap
from src.assistant.chunking import chunk_sections
from src.assistant.sources import resource_document
from src.assistant.sync import index_hash, lock_source, replace_source, stored_hash
from src.content.models import AssetType, ContentAsset
from src.db.base import get_session_factory

NAME = "Counselor Hub user guide"
DESCRIPTION = (
    "How to use each page of the Counselor Hub: signing in, workshops, the program calendar and "
    "email text, analytics, the Assistant, team members, the school profile and preferences."
)
ASSET_TYPE = "Guide"


def apply_guide(db, content: str) -> tuple[ContentAsset, str]:
    """Create or update the guide resource (not committed); returns it and what changed."""
    asset = db.scalars(select(ContentAsset).where(ContentAsset.name == NAME)).first()
    action = "create" if asset is None else ("same" if asset.content == content else "update")
    if asset is None:
        asset = ContentAsset(name=NAME)
        db.add(asset)
    asset.description = DESCRIPTION
    asset.content = content
    asset.asset_type_id = db.scalars(select(AssetType.id).where(AssetType.name == ASSET_TYPE)).first()
    asset.status, asset.source = "published", "cmm"
    asset.for_counselor, asset.for_family, asset.is_public = True, False, False
    asset.updated_at = datetime.now(UTC)
    db.flush()
    return asset, action


def reindex(db, asset: ContentAsset, enrich: bool) -> str:
    """Rebuild the resource's chunks unless they already match; commits."""
    lock_source(db, "resource", asset.id)
    doc = resource_document(asset)
    if stored_hash(db, "resource", asset.id) == index_hash(doc, enrich):
        db.commit()
        return "index unchanged"
    return f"indexed {replace_source(db, doc, enrich)} chunks"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--file", type=Path, required=True, help="counselor-hub-user-guide.md")
    parser.add_argument("--apply", action="store_true", help="write and index the guide (default: dry run)")
    parser.add_argument("--no-enrich", action="store_true", help="skip the Haiku question pass when indexing")
    args = parser.parse_args()

    content = json.dumps(markdown_to_tiptap(args.file.read_text()), ensure_ascii=False)
    with get_session_factory()() as db:
        print(f"Database: {db.get_bind().url.database} (counselor assistant, every school, once applied)")
        asset, action = apply_guide(db, content)
        chunks = chunk_sections(resource_document(asset).sections)
        print(f"{action}  {NAME}: {len(content)} chars of content -> {len(chunks)} chunks")
        for chunk in chunks:
            print(f"  - {chunk.heading} ({len(chunk.text)} chars)")
        if not args.apply:
            db.rollback()
            print("Dry run: nothing written. Re-run with --apply.")
            return
        db.commit()
        print(f"Applied. {reindex(db, asset, enrich=not args.no_enrich)}.")


if __name__ == "__main__":
    main()
