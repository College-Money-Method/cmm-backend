"""Batched PostHog HogQL helpers — many series/breakdowns per round trip.

One dashboard tab used to make one PostHog Query API call PER series (7 for
the content tab), each queueing server-side — slow enough to trip the
frontend's stream timeout. These helpers collapse an endpoint to:
  - ONE query for all its daily trends   (countIf/uniqIf per series)
  - ONE query for all its breakdowns     (UNION ALL with a discriminator col)

Caching mirrors src.analytics.posthog: durable DB cache, stale-on-error.
"""

from __future__ import annotations

import logging
import re
from datetime import date, timedelta
from typing import Any, TypedDict

from sqlalchemy.orm import Session

from src.analytics import posthog as ph
from src.analytics.query_cache import single_flight
from src.analytics.schemas import TopBreakdown, TrendMetric

logger = logging.getLogger(__name__)

# Property names are code-supplied constants, but validate before interpolation
_PROP_RE = re.compile(r"^[A-Za-z_$][A-Za-z0-9_$]*$")
_EVENT_RE = re.compile(r"^[A-Za-z0-9_$]+$")
_MAX_DAYS = 750  # bound the zero-filled series (cycle window is ~15 months)
_MAX_WEBINARS_PER_REQUEST = 50  # log warning if exceeded


class WebinarWindow(TypedDict):
    webinar_id: str       # Zoom string ID (safe: validated before use)
    window_start: date    # Python date (safe: generated in Python, not user input)
    window_end: date      # Python date


class EventSpec(TypedDict, total=False):
    key: str              # result dict key
    event: str            # PostHog event name
    extra_filter: str     # optional extra HogQL WHERE fragment (e.g. "AND properties.via = 'workshop'")
    match_prop: str       # property holding the webinar id for THIS event (default "webinar_id").
    #                       resource_viewed carries the origin webinar in "from", not "webinar_id".


def _validate_webinar_id(webinar_id: str) -> str:
    """Ensure webinar_id is a valid UUID string safe for HogQL interpolation.

    PostHog properties.webinar_id is the internal webinar UUID — same format
    as school_id. Delegates to _validate_school_id for consistency.
    """
    validated = ph._validate_school_id(webinar_id)
    if validated is None:
        raise ValueError(f"Invalid webinar_id for HogQL interpolation: {webinar_id!r}")
    return validated


def _ident(value: str, pattern: re.Pattern, what: str) -> str:
    if not pattern.match(value):
        raise ValueError(f"Invalid {what} for HogQL interpolation: {value!r}")
    return value


def _day_range(date_from: str, date_to: str | None) -> list[str]:
    """Zero-fill day list for the resolved range (relative -Nd or absolute)."""
    today = date.today()
    if ph._RELATIVE_DATE_RE.match(date_from):
        start = today - timedelta(days=int(date_from[1:-1]))
    else:
        start = date.fromisoformat(ph._validate_date(date_from))
    end = date.fromisoformat(ph._validate_date(date_to)) if date_to else today
    end = min(end, today)
    if end < start:
        return []
    n = min((end - start).days + 1, _MAX_DAYS)
    return [(start + timedelta(days=i)).isoformat() for i in range(n)]


def _scope_where(date_from: str, date_to: str | None, school_id: str | None, cycle_name: str | None) -> str:
    return (
        ph._hogql_date_clause(date_from, date_to)
        + ph._hogql_school_clause(school_id)
        + ph._hogql_cycle_clause(cycle_name)
    )


def get_batched_trends(
    api_key: str,
    project_id: str,
    series: list[dict],
    *,
    school_id: str | None = None,
    date_from: str = "-30d",
    date_to: str | None = None,
    cycle_name: str | None = None,
    db: Session | None = None,
    force_refresh: bool = False,
) -> dict[str, TrendMetric]:
    """All daily trends for an endpoint in ONE HogQL query.

    series item: {"key": str, "event": str, "math": "total" | "dau"}
    Returns {key: TrendMetric} with zero-filled aligned days.
    force_refresh: bypass the cache read (Refresh button) — re-query and overwrite.
    """
    cache_key = ph._key(fn="batched_trends", series=series, school_id=school_id, df=date_from, dt=date_to, cyc=cycle_name)
    if (cached := ph._db_get(db, cache_key, school_id, force=force_refresh)) is not None:
        return {k: TrendMetric.model_validate(v) for k, v in cached.items()}

    with single_flight(db, cache_key):
        # Re-check: a request we waited on may have just filled the cache.
        # (force_refresh still bypasses this, so Refresh keeps recomputing.)
        if (cached := ph._db_get(db, cache_key, school_id, force=force_refresh)) is not None:
            return {k: TrendMetric.model_validate(v) for k, v in cached.items()}

        days = _day_range(date_from, date_to)
        cols: list[str] = []
        for s in series:
            ev = _ident(s["event"], _EVENT_RE, "event")
            if s.get("math") == "dau":
                cols.append(f"uniqIf(person_id, event = '{ev}')")
            else:
                cols.append(f"countIf(event = '{ev}')")
        events_in = ", ".join(f"'{_ident(s['event'], _EVENT_RE, 'event')}'" for s in series)
        hogql = (
            f"SELECT toStartOfDay(timestamp) AS day, {', '.join(cols)} "
            "FROM events "
            f"WHERE {_scope_where(date_from, date_to, school_id, cycle_name)} "
            f"AND event IN ({events_in}) "
            "GROUP BY day ORDER BY day"
        )

        try:
            rows = ph.get_hogql_query(api_key, project_id, hogql)
        except Exception as exc:
            logger.warning("PostHog error in get_batched_trends: %s — serving stale", exc)
            stale = ph._db_get_stale(db, cache_key)
            if stale is not None:
                return {k: TrendMetric.model_validate(v) for k, v in stale.items()}
            empty = TrendMetric(total=0, data=[0.0] * len(days), days=days)
            return {s["key"]: empty for s in series}

        by_day = {str(r[0])[:10]: r[1:] for r in rows}
        result: dict[str, TrendMetric] = {}
        for i, s in enumerate(series):
            data = [float(by_day.get(d, [0] * len(series))[i] or 0) for d in days]
            result[s["key"]] = TrendMetric(total=int(sum(data)), data=data, days=days)

        ph._db_set(db, cache_key, {k: v.model_dump() for k, v in result.items()})
        return result


def get_batched_breakdowns(
    api_key: str,
    project_id: str,
    specs: list[dict],
    *,
    school_id: str | None = None,
    date_from: str = "-30d",
    date_to: str | None = None,
    cycle_name: str | None = None,
    db: Session | None = None,
    force_refresh: bool = False,
) -> dict[str, list[TopBreakdown]]:
    """All breakdowns for an endpoint in ONE HogQL query (UNION ALL + kind col).

    spec: {"key": str, "event": str, "prop": str, "math": "count" | "avg",
           "math_prop"?: str, "limit"?: int, "order"?: "desc" | "label_num"}
    order "label_num" keeps numeric-label order (milestone drop-off curves).
    force_refresh: bypass the cache read (Refresh button) — re-query and overwrite.
    """
    cache_key = ph._key(fn="batched_breakdowns", specs=specs, school_id=school_id, df=date_from, dt=date_to, cyc=cycle_name)
    if (cached := ph._db_get(db, cache_key, school_id, force=force_refresh)) is not None:
        return {k: [TopBreakdown.model_validate(r) for r in v] for k, v in cached.items()}

    with single_flight(db, cache_key):
        # Re-check: a request we waited on may have just filled the cache.
        # (force_refresh still bypasses this, so Refresh keeps recomputing.)
        if (cached := ph._db_get(db, cache_key, school_id, force=force_refresh)) is not None:
            return {k: [TopBreakdown.model_validate(r) for r in v] for k, v in cached.items()}

        where = _scope_where(date_from, date_to, school_id, cycle_name)
        branches: list[str] = []
        for sp in specs:
            ev = _ident(sp["event"], _EVENT_RE, "event")
            prop = _ident(sp["prop"], _PROP_RE, "property")
            if sp.get("math") == "avg":
                mp = _ident(sp["math_prop"], _PROP_RE, "math property")
                val = f"avg(toFloat(ifNull(properties.{mp}, '0')))"
            else:
                val = "toFloat(count())"
            branches.append(
                f"SELECT '{sp['key']}' AS kind, toString(properties.{prop}) AS label, {val} AS val "
                f"FROM events WHERE event = '{ev}' AND {where} "
                f"AND isNotNull(properties.{prop}) GROUP BY label"
            )
        hogql = " UNION ALL ".join(branches)

        try:
            rows = ph.get_hogql_query(api_key, project_id, hogql)
        except Exception as exc:
            logger.warning("PostHog error in get_batched_breakdowns: %s — serving stale", exc)
            stale = ph._db_get_stale(db, cache_key)
            if stale is not None:
                return {k: [TopBreakdown.model_validate(r) for r in v] for k, v in stale.items()}
            return {sp["key"]: [] for sp in specs}

        grouped: dict[str, list[TopBreakdown]] = {sp["key"]: [] for sp in specs}
        for r in rows:
            kind, label, val = str(r[0]), str(r[1]), float(r[2] or 0)
            if kind in grouped and label and label != "Other" and not label.startswith("$$_posthog"):
                grouped[kind].append(TopBreakdown(label=label, count=val))

        for sp in specs:
            items = grouped[sp["key"]]
            if sp.get("order") == "label_num":
                # numeric-label curves (e.g. milestone_pct 10..100) keep label order
                items.sort(key=lambda b: float(b.label))
                grouped[sp["key"]] = [TopBreakdown(label=f"{int(float(b.label))}%", count=b.count) for b in items]
            else:
                items.sort(key=lambda b: b.count, reverse=True)
                grouped[sp["key"]] = items[: sp.get("limit", 10)]

        ph._db_set(db, cache_key, {k: [b.model_dump() for b in v] for k, v in grouped.items()})
        return grouped


def get_windowed_trends_by_webinar(
    api_key: str,
    project_id: str,
    webinar_windows: list[WebinarWindow],
    event_specs: list[EventSpec],
    school_id: str | None,
    db: "Session | None" = None,
) -> dict[str, dict[str, list[tuple[str, int]]]]:
    """ONE HogQL round trip for windowed engagement across multiple webinars.

    Builds a UNION ALL query: one subquery per (event, webinar). Each subquery
    filters by the webinar's date window (Python-generated dates, safe) and the
    event name. Returns per-event, per-webinar daily counts.

    Args:
        webinar_windows: list of {webinar_id, window_start, window_end}.
        event_specs: list of {key, event, extra_filter?}.
            extra_filter is a pre-validated HogQL AND fragment for this event.
        school_id: optional school scoping.
        db: DB session for caching.

    Returns:
        {event_key: {webinar_id: [(day_str, count), ...]}}
        Caller zero-fills each series against its window.
    """
    if not webinar_windows or not event_specs:
        return {spec["key"]: {} for spec in event_specs}

    if len(webinar_windows) > _MAX_WEBINARS_PER_REQUEST:
        logger.warning(
            "get_windowed_trends_by_webinar: %d webinars exceeds cap of %d; "
            "query may be slow — consider paginating",
            len(webinar_windows),
            _MAX_WEBINARS_PER_REQUEST,
        )

    school_clause = ph._hogql_school_clause(school_id)

    # Build UNION ALL: one branch per (event_spec × webinar) group.
    # We group all webinars for the same event into a single subquery using OR
    # date-window clauses, then discriminate by webinar_id in GROUP BY.
    branches: list[str] = []
    for spec in event_specs:
        ev = _ident(spec["event"], _EVENT_RE, "event")
        key = spec["key"]
        extra = spec.get("extra_filter") or ""
        # Which property holds the webinar id for this event. Most events use
        # `webinar_id`; resource_viewed carries the origin webinar in `from`
        # (its `webinar_id` is empty), so the match/group key must be `from`.
        match_prop = _ident(spec.get("match_prop") or "webinar_id", _PROP_RE, "match_prop")

        # Build the webinar OR clause: per-webinar date window + id filter.
        # Dates are Python date objects formatted as 'YYYY-MM-DD' — safe.
        webinar_conditions: list[str] = []
        for ww in webinar_windows:
            vid = _validate_webinar_id(ww["webinar_id"])
            ws = ww["window_start"].isoformat()
            we = ww["window_end"].isoformat()
            webinar_conditions.append(
                f"(properties.{match_prop} = '{vid}' "
                f"AND timestamp >= toDateTime('{ws}') "
                f"AND timestamp <= toDateTime('{we} 23:59:59'))"
            )

        webinar_or = " OR ".join(webinar_conditions)

        # Group by the SAME property used to match, so the returned webinar_id
        # aligns with the caller's window regardless of which property carries it.
        branches.append(
            f"SELECT '{key}' AS event_key, "
            f"properties.{match_prop} AS webinar_id, "
            f"toStartOfDay(timestamp) AS day, "
            f"count() AS cnt "
            f"FROM events "
            f"WHERE event = '{ev}'{school_clause} "
            f"AND ({webinar_or}){extra} "
            f"GROUP BY webinar_id, day"
        )

    hogql = " UNION ALL ".join(branches)

    try:
        rows = ph.get_hogql_query(api_key, project_id, hogql)
    except Exception as exc:
        logger.warning("PostHog error in get_windowed_trends_by_webinar: %s", exc)
        return {spec["key"]: {} for spec in event_specs}

    # Accumulate results: {event_key: {webinar_id: [(day_str, count)]}}
    result: dict[str, dict[str, list[tuple[str, int]]]] = {
        spec["key"]: {} for spec in event_specs
    }
    for row in rows:
        event_key = str(row[0])
        vid = str(row[1]) if row[1] else None
        day_str = str(row[2])[:10] if row[2] else None
        cnt = int(row[3] or 0)
        if event_key in result and vid and day_str:
            result[event_key].setdefault(vid, []).append((day_str, cnt))

    return result
