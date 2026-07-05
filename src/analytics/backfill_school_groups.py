"""One-off backfill: push PostHog group props for ALL schools.

Usage (from repo root, with backend env loaded):
    uv run python -m src.analytics.backfill_school_groups            # dry run
    uv run python -m src.analytics.backfill_school_groups --confirm  # send

The PostHog project the events land in is determined by POSTHOG_PROJECT_TOKEN
in the environment — verify it targets the intended project (dev vs prod)
before running with --confirm.
"""

import argparse
import logging
import sys

from sqlalchemy.orm import joinedload

from src.analytics.group_identify import identify_school_groups_batch
from src.config import settings
from src.db.deps import get_db
# Barrel import registers all mappers so School's relationships resolve outside
# the app; UserRole isn't in the barrel and School references it, so import it too
import src.auth.models  # noqa: F401
from src.db.models import School

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill PostHog school group props")
    parser.add_argument("--confirm", action="store_true", help="Actually send (default: dry run)")
    args = parser.parse_args()

    if not settings.posthog_project_token:
        logger.error("POSTHOG_PROJECT_TOKEN is not set — aborting")
        return 1

    db = next(get_db())
    try:
        # Eager-load cohort — group props include cohort_name
        schools = db.query(School).options(joinedload(School.cohort)).all()

        token_hint = settings.posthog_project_token[:12]
        logger.info("Loaded %d schools; target token=%s… env=%s", len(schools), token_hint, settings.environment)

        if not args.confirm:
            for s in schools[:10]:
                logger.info(
                    "DRY RUN would sync: %s (state=%s enrollment=%s customer=%s cohort=%s)",
                    s.name, s.state, s.enrollment_9_12, s.is_current_customer,
                    s.cohort.name if s.cohort else None,
                )
            logger.info("DRY RUN — %d schools total. Re-run with --confirm to send.", len(schools))
            return 0

        sent = identify_school_groups_batch(schools)
        logger.info("Done — synced %d/%d schools to PostHog", sent, len(schools))
        return 0 if sent == len(schools) else 1
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
