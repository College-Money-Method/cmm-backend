"""
One-time backfill: populate read_time_minutes and video_duration_seconds
for Topics and ContentAssets.

    read_time_minutes      — estimated from content + summary word count (~200 wpm)
    video_duration_seconds — fetched from Vimeo oEmbed API or YouTube Data API

Usage:
    python -m scripts.backfill_read_watch_times            # NULL records only
    python -m scripts.backfill_read_watch_times --all      # recalculate all
    python -m scripts.backfill_read_watch_times --dry-run  # preview, no DB writes

Note: YouTube requires YOUTUBE_API_KEY env var. Vimeo works without any key.
"""

from __future__ import annotations

import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

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

import requests
from src.content.models import ContentAsset, Topic
from src.db.base import get_session_factory
from src.utils.tiptap import extract_text


def _calculate_read_time(content: str | None, summary: str | None = None) -> int | None:
    combined = " ".join(filter(None, [extract_text(content), extract_text(summary)]))
    if not combined:
        raw = (content or "") + " " + (summary or "")
        combined = re.sub(r"<[^>]+>", " ", raw)
    words = len(combined.split())
    return max(1, round(words / 200)) if words > 0 else None


def _extract_video_duration(embed_code: str | None) -> int | None:
    if not embed_code:
        return None

    vimeo_match = re.search(r'player\.vimeo\.com/video/(\d+)', embed_code)
    if vimeo_match:
        try:
            resp = requests.get(
                f"https://vimeo.com/api/oembed.json?url=https://vimeo.com/{vimeo_match.group(1)}",
                timeout=5,
            )
            if resp.ok:
                return resp.json().get("duration")
        except Exception:
            pass

    yt_match = re.search(r'youtube(?:-nocookie)?\.com/embed/([a-zA-Z0-9_-]+)', embed_code)
    if yt_match:
        api_key = os.environ.get("YOUTUBE_API_KEY")
        if api_key:
            try:
                resp = requests.get(
                    "https://www.googleapis.com/youtube/v3/videos",
                    params={"id": yt_match.group(1), "part": "contentDetails", "key": api_key},
                    timeout=5,
                )
                if resp.ok:
                    items = resp.json().get("items", [])
                    if items:
                        dur = items[0]["contentDetails"]["duration"]
                        h = int(re.search(r'(\d+)H', dur).group(1)) if re.search(r'(\d+)H', dur) else 0
                        m = int(re.search(r'(\d+)M', dur).group(1)) if re.search(r'(\d+)M', dur) else 0
                        s = int(re.search(r'(\d+)S', dur).group(1)) if re.search(r'(\d+)S', dur) else 0
                        return h * 3600 + m * 60 + s
            except Exception:
                pass
        else:
            return None  # YouTube embed but no API key — skip silently

    return None


def _backfill_video_durations(records: list, embed_attr: str, dry_run: bool) -> tuple[int, int]:
    """Returns (updated, unresolved)."""
    updated = 0
    unresolved = 0
    for i, obj in enumerate(records, 1):
        embed = getattr(obj, embed_attr)
        val = _extract_video_duration(embed)
        if val is None:
            unresolved += 1
        elif val != obj.video_duration_seconds:
            if not dry_run:
                obj.video_duration_seconds = val
            updated += 1
        if i % 5 == 0:
            time.sleep(0.3)  # gentle rate limit for Vimeo oEmbed
    return updated, unresolved


def main(all_records: bool = False, dry_run: bool = False) -> None:
    prefix = "[dry-run] " if dry_run else ""
    SessionFactory = get_session_factory()

    with SessionFactory() as db:
        # ── Topics: read_time_minutes ─────────────────────────────────────────
        topics_all = db.query(Topic).all()
        topics_read = topics_all if all_records else [t for t in topics_all if t.read_time_minutes is None]
        read_updated = 0
        for obj in topics_read:
            val = _calculate_read_time(obj.content, obj.summary)
            if val != obj.read_time_minutes:
                if not dry_run:
                    obj.read_time_minutes = val
                read_updated += 1
        print(f"{prefix}Topics       read_time_minutes:      {read_updated:>3} updated  (of {len(topics_read)} eligible)")

        # ── Topics: video_duration_seconds ────────────────────────────────────
        topics_vid = (
            [t for t in topics_all if t.video_embed_code]
            if all_records
            else [t for t in topics_all if t.video_embed_code and t.video_duration_seconds is None]
        )
        vid_updated, vid_unresolved = _backfill_video_durations(topics_vid, "video_embed_code", dry_run)
        print(f"{prefix}Topics       video_duration_seconds: {vid_updated:>3} updated  (of {len(topics_vid)} eligible, {vid_unresolved} unresolved)")

        # ── ContentAssets: read_time_minutes ──────────────────────────────────
        assets_all = db.query(ContentAsset).all()
        assets_read = assets_all if all_records else [a for a in assets_all if a.read_time_minutes is None]
        asset_read_updated = 0
        for obj in assets_read:
            val = _calculate_read_time(obj.content, obj.summary)
            if val != obj.read_time_minutes:
                if not dry_run:
                    obj.read_time_minutes = val
                asset_read_updated += 1
        print(f"{prefix}ContentAssets read_time_minutes:      {asset_read_updated:>3} updated  (of {len(assets_read)} eligible)")

        # ── ContentAssets: video_duration_seconds ─────────────────────────────
        assets_vid = (
            [a for a in assets_all if a.embed_code]
            if all_records
            else [a for a in assets_all if a.embed_code and a.video_duration_seconds is None]
        )
        asset_vid_updated, asset_vid_unresolved = _backfill_video_durations(assets_vid, "embed_code", dry_run)
        print(f"{prefix}ContentAssets video_duration_seconds: {asset_vid_updated:>3} updated  (of {len(assets_vid)} eligible, {asset_vid_unresolved} unresolved)")

        if not dry_run:
            db.commit()
            print("\nDone.")
        else:
            print("\nDry-run complete — no changes written.")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Backfill read_time_minutes and video_duration_seconds")
    parser.add_argument("--all", dest="all_records", action="store_true", help="Recalculate all records, not just NULL")
    parser.add_argument("--dry-run", action="store_true", help="Preview without writing to DB")
    args = parser.parse_args()
    main(all_records=args.all_records, dry_run=args.dry_run)
