"""
One-time backfill script: populate search_text for existing Topics, Workshops,
and ContentAssets. The DB trigger then auto-populates search_vector on each UPDATE.

Usage:
    python -m scripts.backfill_search_text
"""

from __future__ import annotations

import sys
import os

# Allow running from the project root
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

# Import all models so SQLAlchemy can resolve relationships before the session is used
import src.assets.models  # noqa: F401
import src.auth.models  # noqa: F401
import src.calendar.models  # noqa: F401
import src.content.models  # noqa: F401
import src.cycles.models  # noqa: F401
import src.meetings.models  # noqa: F401
import src.sales.models  # noqa: F401
import src.schools.models  # noqa: F401
import src.settings.models  # noqa: F401
import src.workshops.models  # noqa: F401
import src.guest_contacts.models  # noqa: F401

from src.content.models import ContentAsset, Topic
from src.db.base import get_session_factory
from src.utils.tiptap import extract_text
from src.workshops.models import Workshop


def build_topic_search_text(obj: Topic) -> str:
    return " ".join(filter(None, [
        obj.title or "",
        obj.description or "",
        extract_text(obj.summary),
        extract_text(obj.content),
    ]))


def build_workshop_search_text(obj: Workshop) -> str:
    return " ".join(filter(None, [
        obj.name or "",
        obj.description or "",
        extract_text(obj.body),
        extract_text(obj.key_actions),
    ]))


def build_asset_search_text(obj: ContentAsset) -> str:
    return " ".join(filter(None, [
        obj.name or "",
        obj.description or "",
        extract_text(obj.summary),
        extract_text(obj.content),
    ]))


def main() -> None:
    SessionFactory = get_session_factory()

    with SessionFactory() as db:
        # ── Topics ────────────────────────────────────────────────────────────
        topics = db.query(Topic).all()
        print(f"Backfilling {len(topics)} topics...")
        for obj in topics:
            obj.search_text = build_topic_search_text(obj)
        db.flush()

        # ── Workshops ─────────────────────────────────────────────────────────
        workshops = db.query(Workshop).all()
        print(f"Backfilling {len(workshops)} workshops...")
        for obj in workshops:
            obj.search_text = build_workshop_search_text(obj)
        db.flush()

        # ── Content assets ────────────────────────────────────────────────────
        assets = db.query(ContentAsset).all()
        print(f"Backfilling {len(assets)} content assets...")
        for obj in assets:
            obj.search_text = build_asset_search_text(obj)
        db.flush()

        db.commit()
        print("Done. search_text populated; DB trigger has updated search_vector.")


if __name__ == "__main__":
    main()
