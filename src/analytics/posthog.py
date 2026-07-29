"""PostHog query client with durable DB cache (falls back to in-process dict).

TTL policy:
  - school-scoped calls: 60 min
  - admin/global calls (school_id=None): 30 min

On PostHog HTTP error or timeout, a stale DB entry (any age) is served if one
exists, preventing blank dashboards during PostHog incidents.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from sqlalchemy.orm import Session

from src.analytics.schemas import FunnelStep, TopBreakdown, TrendMetric

logger = logging.getLogger(__name__)

POSTHOG_API = "https://us.posthog.com"
_SCHOOL_TTL = timedelta(minutes=60)
_ADMIN_TTL = timedelta(minutes=30)

# Fallback in-process cache used when db=None (non-request contexts).
_cache: dict[str, tuple[Any, datetime]] = {}
_INPROC_TTL = timedelta(minutes=30)

# Strict allow-list for user-supplied date values inserted into HogQL.
_RELATIVE_DATE_RE = re.compile(r"^-\d{1,4}d$")
_ABS_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# ── Validation helpers ─────────────────────────────────────────────────────────

def _validate_date(value: str) -> str:
    """Raise ValueError if value is not a safe date literal for HogQL."""
    if _RELATIVE_DATE_RE.match(value) or _ABS_DATE_RE.match(value):
        return value
    raise ValueError(f"Invalid date value for HogQL interpolation: {value!r}")


def _validate_school_id(school_id: str | None) -> str | None:
    """Ensure school_id looks like a UUID string before HogQL interpolation."""
    if school_id is None:
        return None
    # UUID chars only: hex + hyphens
    if re.fullmatch(r"[0-9a-fA-F\-]{32,36}", school_id):
        return school_id
    raise ValueError(f"Invalid school_id for HogQL interpolation: {school_id!r}")


# ── Cache helpers ─────────────────────────────────────────────────────────────

def _key(**kwargs: Any) -> str:
    return hashlib.md5(json.dumps(kwargs, sort_keys=True, default=str).encode()).hexdigest()


def _cache_ttl(school_id: str | None) -> timedelta:
    return _SCHOOL_TTL if school_id else _ADMIN_TTL


def _get(key: str) -> Any | None:
    if key in _cache:
        data, ts = _cache[key]
        if datetime.now(timezone.utc) - ts < _INPROC_TTL:
            return data
        del _cache[key]
    return None


def _set(key: str, data: Any) -> None:
    _cache[key] = (data, datetime.now(timezone.utc))


def _db_get(db: Session | None, key: str, school_id: str | None, force: bool = False) -> Any | None:
    # force=True: caller wants fresh data (Refresh button) — treat as a cache
    # miss so the endpoint re-queries PostHog and overwrites the entry.
    if force:
        return None
    if db is None:
        return _get(key)
    from src.analytics.query_cache import db_cache_get
    payload = db_cache_get(db, key, _cache_ttl(school_id))
    if payload is not None:
        return payload.get("v")
    return None


def _db_set(db: Session | None, key: str, data: Any) -> None:
    if db is None:
        _set(key, data)
        return
    from src.analytics.query_cache import db_cache_set
    # Serialize via default=str to handle UUID/datetime values in data.
    payload = json.loads(json.dumps({"v": data}, default=str))
    db_cache_set(db, key, payload)


def _db_get_stale(db: Session | None, key: str) -> Any | None:
    if db is None:
        # In-process: serve whatever's in cache (already expired above)
        if key in _cache:
            return _cache[key][0]
        return None
    from src.analytics.query_cache import db_cache_get_stale
    payload = db_cache_get_stale(db, key)
    if payload is not None:
        return payload.get("v")
    return None


# ── Low-level query ───────────────────────────────────────────────────────────

def _query(api_key: str, project_id: str, query: dict) -> dict:
    with httpx.Client(timeout=30) as client:
        r = client.post(
            f"{POSTHOG_API}/api/projects/{project_id}/query/",
            json={"query": query},
            headers={"Authorization": f"Bearer {api_key}"},
        )
        r.raise_for_status()
        return r.json()


def _school_filter(school_id: str | None, prop_type: str = "event") -> list[dict]:
    if not school_id:
        return []
    return [{"key": "school_id", "value": school_id, "operator": "exact", "type": prop_type}]


def _cycle_filter(cycle_name: str | None) -> list[dict]:
    """Property filter on the cycle_name super-property.

    Events are tagged with the operationally-current cycle at browse time, so
    this captures pre/post-cycle browsing that a start/end date range misses.
    """
    if not cycle_name:
        return []
    return [{"key": "cycle_name", "value": cycle_name, "operator": "exact", "type": "event"}]


def _validate_cycle_name(cycle_name: str) -> str:
    """Ensure cycle_name is safe for HogQL string interpolation."""
    if len(cycle_name) > 100 or "'" in cycle_name or "\\" in cycle_name or "\n" in cycle_name:
        raise ValueError(f"Invalid cycle_name for HogQL interpolation: {cycle_name!r}")
    return cycle_name


def _hogql_cycle_clause(cycle_name: str | None) -> str:
    """Return a safe HogQL WHERE fragment for cycle scoping (empty string if None)."""
    if not cycle_name:
        return ""
    return f" AND properties.cycle_name = '{_validate_cycle_name(cycle_name)}'"


def _date_range(date_from: str, date_to: str | None) -> dict:
    dr: dict = {"date_from": date_from}
    if date_to:
        dr["date_to"] = date_to
    return dr


def _hogql_date_clause(date_from: str, date_to: str | None) -> str:
    """Return a safe HogQL WHERE fragment for the date range.

    Relative (-Nd) → timestamp >= now() - INTERVAL N DAY
    Absolute (YYYY-MM-DD) → timestamp >= toDateTime('YYYY-MM-DD')
    """
    date_from = _validate_date(date_from)

    if _RELATIVE_DATE_RE.match(date_from):
        n = date_from[1:-1]  # strip leading '-' and trailing 'd'
        from_clause = f"timestamp >= now() - INTERVAL {n} DAY"
    else:
        from_clause = f"timestamp >= toDateTime('{date_from}')"

    if date_to:
        date_to = _validate_date(date_to)
        if _RELATIVE_DATE_RE.match(date_to):
            n = date_to[1:-1]
            to_clause = f" AND timestamp <= now() - INTERVAL {n} DAY"
        else:
            to_clause = f" AND timestamp <= toDateTime('{date_to} 23:59:59')"
    else:
        to_clause = ""

    return from_clause + to_clause


def _hogql_school_clause(school_id: str | None) -> str:
    """Return a safe HogQL WHERE fragment for school scoping (empty string if None)."""
    if not school_id:
        return ""
    sid = _validate_school_id(school_id)
    return f" AND properties.school_id = '{sid}'"


# ── HogQL helper ──────────────────────────────────────────────────────────────

def get_hogql_query(
    api_key: str,
    project_id: str,
    hogql: str,
) -> list[list]:
    """Execute a raw HogQL query and return result rows.

    Callers are responsible for building safe HogQL using _hogql_date_clause /
    _hogql_school_clause — never interpolate raw user input directly.
    """
    result = _query(api_key, project_id, {"kind": "HogQLQuery", "query": hogql})
    return result.get("results", [])


# ── Public helpers ────────────────────────────────────────────────────────────
# NOTE: the 4 dashboard endpoints now use src.analytics.posthog_batched (one
# round trip for all trends + one for all breakdowns). The per-series helpers
# below remain for the admin endpoints and one-off queries.

def get_trend(
    api_key: str,
    project_id: str,
    event: str,
    *,
    school_id: str | None = None,
    date_from: str = "-30d",
    date_to: str | None = None,
    math: str = "total",
    math_property: str | None = None,
    prop_type: str = "event",
    cycle_name: str | None = None,
    db: Session | None = None,
) -> TrendMetric:
    cache_key = _key(fn="trend", event=event, school_id=school_id, df=date_from, dt=date_to, math=math, mp=math_property, pt=prop_type, cyc=cycle_name)
    if (cached := _db_get(db, cache_key, school_id)) is not None:
        return TrendMetric.model_validate(cached) if isinstance(cached, dict) else cached

    series: dict = {"kind": "EventsNode", "event": event, "math": math}
    if math_property:
        series["math_property"] = math_property

    try:
        result = _query(api_key, project_id, {
            "kind": "TrendsQuery",
            "series": [series],
            "dateRange": _date_range(date_from, date_to),
            "properties": _school_filter(school_id, prop_type) + _cycle_filter(cycle_name),
            "interval": "day",
            "filterTestAccounts": False,
            "version": 2,
        })
    except (httpx.HTTPError, httpx.TimeoutException) as exc:
        logger.warning("PostHog error in get_trend(%s): %s — serving stale", event, exc)
        stale = _db_get_stale(db, cache_key)
        if stale is not None:
            return TrendMetric.model_validate(stale) if isinstance(stale, dict) else stale
        return TrendMetric(total=0, data=[], days=[])

    s = result.get("results", [{}])[0]
    metric = TrendMetric(total=s.get("count", 0), data=s.get("data", []), days=s.get("days", []))
    _db_set(db, cache_key, metric.model_dump())
    return metric


def get_funnel(
    api_key: str,
    project_id: str,
    step1: str,
    step2: str,
    *,
    school_id: str | None = None,
    date_from: str = "-30d",
    date_to: str | None = None,
    cycle_name: str | None = None,
    db: Session | None = None,
) -> list[FunnelStep]:
    cache_key = _key(fn="funnel", s1=step1, s2=step2, school_id=school_id, df=date_from, dt=date_to, cyc=cycle_name)
    if (cached := _db_get(db, cache_key, school_id)) is not None:
        if isinstance(cached, list) and cached and isinstance(cached[0], dict):
            return [FunnelStep.model_validate(s) for s in cached]
        return cached

    try:
        result = _query(api_key, project_id, {
            "kind": "FunnelsQuery",
            "series": [{"kind": "EventsNode", "event": step1}, {"kind": "EventsNode", "event": step2}],
            "dateRange": _date_range(date_from, date_to),
            "properties": _school_filter(school_id) + _cycle_filter(cycle_name),
            "funnelsFilter": {"funnelVizType": "steps", "funnelOrderType": "ordered", "funnelWindowInterval": 1, "funnelWindowIntervalUnit": "day"},
            "filterTestAccounts": False,
        })
    except (httpx.HTTPError, httpx.TimeoutException) as exc:
        logger.warning("PostHog error in get_funnel: %s — serving stale", exc)
        stale = _db_get_stale(db, cache_key)
        if stale is not None:
            return [FunnelStep.model_validate(s) for s in stale] if stale and isinstance(stale[0], dict) else stale
        return []

    raw = result.get("result") or result.get("results") or []
    if raw and isinstance(raw[0], list):
        raw_steps = raw[0]
    elif raw and isinstance(raw[0], dict):
        raw_steps = raw
    else:
        raw_steps = []
    steps = [FunnelStep(name=s.get("name", s.get("breakdown_value", "")), count=s.get("count", 0)) for s in raw_steps]
    _db_set(db, cache_key, [s.model_dump() for s in steps])
    return steps


def get_top_breakdown(
    api_key: str,
    project_id: str,
    event: str,
    breakdown_prop: str,
    *,
    school_id: str | None = None,
    date_from: str = "-30d",
    date_to: str | None = None,
    limit: int = 8,
    math: str = "total",
    math_property: str | None = None,
    cycle_name: str | None = None,
    db: Session | None = None,
) -> list[TopBreakdown]:
    cache_key = _key(fn="breakdown", event=event, bp=breakdown_prop, school_id=school_id, df=date_from, dt=date_to, math=math, mp=math_property, cyc=cycle_name)
    if (cached := _db_get(db, cache_key, school_id)) is not None:
        if isinstance(cached, list) and cached and isinstance(cached[0], dict):
            return [TopBreakdown.model_validate(r) for r in cached]
        return cached

    series: dict = {"kind": "EventsNode", "event": event, "math": math}
    if math_property:
        series["math_property"] = math_property

    try:
        result = _query(api_key, project_id, {
            "kind": "TrendsQuery",
            "series": [series],
            "dateRange": _date_range(date_from, date_to),
            "properties": _school_filter(school_id) + _cycle_filter(cycle_name),
            "breakdownFilter": {"breakdowns": [{"type": "event", "property": breakdown_prop}], "breakdown_type": "event"},
            "trendsFilter": {"display": "ActionsBarValue"},
            "filterTestAccounts": False,
            "version": 2,
        })
    except (httpx.HTTPError, httpx.TimeoutException) as exc:
        logger.warning("PostHog error in get_top_breakdown(%s): %s — serving stale", event, exc)
        stale = _db_get_stale(db, cache_key)
        if stale is not None:
            return [TopBreakdown.model_validate(r) for r in stale] if stale and isinstance(stale[0], dict) else stale
        return []

    def _extract_label(r: dict) -> str:
        bv = r.get("breakdown_value")
        if bv is None:
            bv = r.get("label", "")
        if isinstance(bv, list):
            bv = bv[0] if bv else ""
        return str(bv)

    def _valid(r: dict) -> bool:
        label = _extract_label(r)
        return bool(label) and label != "Other" and not label.startswith("$$_posthog")

    def _value(r: dict) -> float:
        val = r.get("aggregated_value")
        return val if val is not None else r.get("count", 0)

    rows = sorted(
        [{"label": _extract_label(r), "count": _value(r)} for r in result.get("results", []) if _valid(r)],
        key=lambda x: x["count"], reverse=True,
    )[:limit]
    breakdown = [TopBreakdown(label=r["label"], count=r["count"]) for r in rows]
    _db_set(db, cache_key, [b.model_dump() for b in breakdown])
    return breakdown
