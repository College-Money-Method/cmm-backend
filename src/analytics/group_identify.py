"""Sync school attributes to the PostHog "school" group via $groupidentify.

Uses the public project token (phc_...) with the capture endpoint — the same
mechanism posthog-python uses under the hood, without the SDK's background
consumer thread. Fire-and-forget: failures are logged, never raised, so a
PostHog outage can never break a school create/update.
"""

import logging
from typing import TYPE_CHECKING

import httpx

from src.config import settings

if TYPE_CHECKING:
    from src.schools.models import School

logger = logging.getLogger(__name__)

POSTHOG_CAPTURE_URL = "https://us.i.posthog.com/i/v0/e/"
POSTHOG_BATCH_URL = "https://us.i.posthog.com/batch/"


def _group_properties(school: "School") -> dict:
    """Group props powering PostHog-native breakdowns (enrollment, state, cohort, tier)."""
    # Admins group schools by cohort in practice — send the human-readable name
    # for dashboard breakdowns (cohort_id stays for joins). Lazy-loads within
    # the caller's session when the relationship isn't already eager-loaded.
    try:
        cohort_name = school.cohort.name if school.cohort else None
    except Exception:
        cohort_name = None
    return {
        "name": school.name,
        "slug": school.slug,
        "school_state": school.state,
        "upper_school_enrollment": school.enrollment_9_12,
        "enrollment_range": school.enrollment_range,
        "is_current_customer": school.is_current_customer,
        "cohort_id": str(school.cohort_id) if school.cohort_id else None,
        "cohort_name": cohort_name,
        # Placeholder — computed by a scheduled job once post-launch event
        # history exists to calibrate tier thresholds (see analytics plan)
        "engagement_tier": None,
    }


def identify_school_group(school: "School") -> bool:
    """Push current school attributes onto the PostHog group. Returns success."""
    if not settings.posthog_project_token:
        logger.debug("PostHog project token not configured — skipping group identify")
        return False
    try:
        with httpx.Client(timeout=10) as client:
            r = client.post(
                POSTHOG_CAPTURE_URL,
                json={
                    "api_key": settings.posthog_project_token,
                    "event": "$groupidentify",
                    "distinct_id": "cmm-backend-group-sync",
                    "properties": {
                        "$group_type": "school",
                        "$group_key": str(school.id),
                        "$group_set": _group_properties(school),
                    },
                },
            )
            r.raise_for_status()
        return True
    except Exception:
        logger.warning("PostHog group identify failed for school %s", school.id, exc_info=True)
        return False


def identify_school_groups_batch(schools: list["School"]) -> int:
    """Push group props for many schools in one /batch/ call (Airtable sync, backfill).

    Returns the number of schools sent (0 on failure or missing config).
    """
    if not settings.posthog_project_token or not schools:
        return 0
    events = [
        {
            "event": "$groupidentify",
            "distinct_id": "cmm-backend-group-sync",
            "properties": {
                "$group_type": "school",
                "$group_key": str(school.id),
                "$group_set": _group_properties(school),
            },
        }
        for school in schools
    ]
    try:
        with httpx.Client(timeout=30) as client:
            r = client.post(
                POSTHOG_BATCH_URL,
                json={"api_key": settings.posthog_project_token, "batch": events},
            )
            r.raise_for_status()
        logger.info("PostHog group identify batch sent for %d schools", len(events))
        return len(events)
    except Exception:
        logger.warning("PostHog group identify batch failed (%d schools)", len(events), exc_info=True)
        return 0
