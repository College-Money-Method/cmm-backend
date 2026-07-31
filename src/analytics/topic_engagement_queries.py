"""Topic-engagement tree: the school's Grade → Goal → Topic hierarchy from
Postgres, joined with per-topic PostHog metrics (page engagement + video views).

Two halves, deliberately kept together (both are topic-engagement specific):
  • get_school_topic_tree  — pure DB: the published hierarchy a school's
    families actually see (same grade-set resolution as the public
    /grade-configs/public endpoint).
  • get_topic_metrics      — ONE cached HogQL round trip: topic_viewed count +
    video_view count (object_type='topic'), grouped by topic id.
"""

from __future__ import annotations

import logging
import re
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session, contains_eager, selectinload

from src.analytics import posthog as ph
from src.analytics.query_cache import single_flight
from src.content.models import Goal, GradeConfig, GradeConfigGoal, GradeSet
from src.schools.models import School

logger = logging.getLogger(__name__)

# Cap on the id list interpolated into HogQL. Well above any real grade set
# (~10 topics per goal × a handful of goals per grade); guards the query size.
_MAX_TOPIC_IDS = 500

_UUID_RE = re.compile(r"^[0-9a-fA-F-]{32,36}$")


# ── Postgres: the hierarchy ───────────────────────────────────────────────────

def get_school_topic_tree(db: Session, school_id: str | None) -> tuple[str | None, list[dict]]:
    """Return (school_slug, grades) for the school's assigned grade set.

    Falls back to the default grade set when the school has none (or no school is
    selected — super_admin viewing "All schools"), matching the public site.
    Only PUBLISHED topics are included, in the site's own display order
    (grade → goal sort_order → topic sort_order).

    NOTE: a goal can be attached to several grade configs, so the same topic can
    legitimately appear under more than one grade. Rollups then count it under
    each grade it appears in — the tree mirrors site navigation, not a partition.
    """
    school_slug: str | None = None
    grade_set_id: uuid.UUID | None = None

    if school_id:
        school = db.query(School).filter(School.id == school_id).first()
        if school:
            school_slug = school.slug
            grade_set_id = school.grade_set_id

    if grade_set_id is None:
        gs = db.query(GradeSet).filter(GradeSet.is_default.is_(True)).first()
        grade_set_id = gs.id if gs else None
    if grade_set_id is None:
        return school_slug, []

    stmt = (
        select(GradeConfig)
        .where(GradeConfig.grade_set_id == grade_set_id)
        .outerjoin(GradeConfigGoal, GradeConfig.id == GradeConfigGoal.grade_config_id)
        .outerjoin(Goal, GradeConfigGoal.goal_id == Goal.id)
        .options(contains_eager(GradeConfig.goals).selectinload(Goal.topics))
        .order_by(GradeConfig.grade, GradeConfigGoal.sort_order)
    )
    configs = db.execute(stmt).unique().scalars().all()

    grades: list[dict] = []
    for gc in configs:
        goals: list[dict] = []
        for goal in gc.goals:
            topics = [
                {"topic_id": str(t.id), "title": t.title, "slug": t.slug}
                for t in sorted(
                    (t for t in goal.topics if t.status == "published"),
                    key=lambda t: (t.sort_order, t.title),
                )
            ]
            if not topics:
                continue  # nothing published under this goal — hide the section
            goals.append({"goal_id": str(goal.id), "name": goal.name, "topics": topics})
        if not goals:
            continue
        grades.append({
            "grade": gc.grade,
            "label": gc.label or f"Grade {gc.grade}",
            # Shown next to the label so the section reads like the grade page
            # itself ("9th Grade — Learn How Financial Aid Works"); often unset.
            "page_title": gc.page_title,
            "goals": goals,
        })
    return school_slug, grades


# ── PostHog: per-topic metrics ────────────────────────────────────────────────

def get_topic_metrics(
    api_key: str,
    project_id: str,
    topic_ids: list[str],
    *,
    school_id: str | None,
    date_from: str,
    date_to: str | None,
    cycle_name: str | None,
    db: Session | None = None,
    force_refresh: bool = False,
) -> dict[str, dict[str, int]]:
    """{topic_id: {"engagement": n, "video_views": n}} for the given topics.

    Engagement = `topic_viewed` (the same event behind the "Topic Engagement"
    tile). Video views = `video_view` with object_type='topic', whose object_id
    IS the topic id — so both metrics key off the topic's UUID.

    Cached like every other analytics query (60 min school / 30 min admin);
    `force_refresh` (the Refresh button) bypasses and rewrites the entry. On a
    PostHog error a stale entry is served if one exists, else empty metrics —
    the tree still renders, with zeros.
    """
    ids = [i for i in topic_ids if _UUID_RE.match(i)][:_MAX_TOPIC_IDS]
    if not ids:
        return {}

    cache_key = ph._key(
        fn="topic_metrics",
        ids=sorted(ids),
        school_id=school_id,
        df=date_from,
        dt=date_to,
        cyc=cycle_name,
    )
    cached = ph._db_get(db, cache_key, school_id, force=force_refresh)
    if cached is not None:
        return cached

    with single_flight(db, cache_key):
        # Re-check: a request we waited on may have just filled the cache.
        # (force_refresh still bypasses this, so Refresh keeps recomputing.)
        cached = ph._db_get(db, cache_key, school_id, force=force_refresh)
        if cached is not None:
            return cached

        id_list = ", ".join(f"'{i}'" for i in ids)
        # The id filter is spelled out per event (NOT via the `tid` alias): each event
        # carries the topic id under a different property, and referencing a SELECT
        # alias in WHERE is not something to rely on across query engines.
        hogql = (
            "SELECT "
            "  toString(if(event = 'topic_viewed', properties.topic_id, properties.object_id)) AS tid, "
            "  countIf(event = 'topic_viewed') AS engagement, "
            "  countIf(event = 'video_view') AS video_views "
            "FROM events "
            f"WHERE {ph._hogql_date_clause(date_from, date_to)}"
            f"{ph._hogql_school_clause(school_id)}{ph._hogql_cycle_clause(cycle_name)} "
            f"  AND ((event = 'topic_viewed' AND toString(properties.topic_id) IN ({id_list})) "
            "       OR (event = 'video_view' AND properties.object_type = 'topic' "
            f"           AND toString(properties.object_id) IN ({id_list}))) "
            "GROUP BY tid"
        )
        try:
            rows = ph.get_hogql_query(api_key, project_id, hogql)
        except Exception:
            logger.warning("PostHog error computing topic metrics", exc_info=True)
            stale = ph._db_get_stale(db, cache_key)
            return stale if stale is not None else {}

        metrics = {
            str(r[0]): {"engagement": int(r[1] or 0), "video_views": int(r[2] or 0)}
            for r in rows
            if r and r[0]
        }
        ph._db_set(db, cache_key, metrics)
        return metrics
