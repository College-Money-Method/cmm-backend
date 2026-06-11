"""PostHog query client with in-memory TTL cache (30 min)."""

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from src.analytics.schemas import FunnelStep, TrendMetric, TopBreakdown

POSTHOG_API = "https://us.posthog.com"
CACHE_TTL = timedelta(minutes=30)

_cache: dict[str, tuple[Any, datetime]] = {}


# ── Cache helpers ─────────────────────────────────────────────────────────────

def _key(**kwargs: Any) -> str:
    # default=str handles UUID and other non-serializable types (e.g. school_id from SQLAlchemy)
    return hashlib.md5(json.dumps(kwargs, sort_keys=True, default=str).encode()).hexdigest()


def _get(key: str) -> Any | None:
    if key in _cache:
        data, ts = _cache[key]
        if datetime.now(timezone.utc) - ts < CACHE_TTL:
            return data
        del _cache[key]
    return None


def _set(key: str, data: Any) -> None:
    _cache[key] = (data, datetime.now(timezone.utc))


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


def _date_range(date_from: str, date_to: str | None) -> dict:
    dr: dict = {"date_from": date_from}
    if date_to:
        dr["date_to"] = date_to
    return dr


# ── Public helpers ────────────────────────────────────────────────────────────

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
) -> TrendMetric:
    cache_key = _key(fn="trend", event=event, school_id=school_id, df=date_from, dt=date_to, math=math, mp=math_property, pt=prop_type)
    if (cached := _get(cache_key)) is not None:
        return cached

    series: dict = {"kind": "EventsNode", "event": event, "math": math}
    if math_property:
        series["math_property"] = math_property

    result = _query(api_key, project_id, {
        "kind": "TrendsQuery",
        "series": [series],
        "dateRange": _date_range(date_from, date_to),
        "properties": _school_filter(school_id, prop_type),
        "interval": "day",
        "filterTestAccounts": False,
        "version": 2,
    })
    s = result.get("results", [{}])[0]
    metric = TrendMetric(total=s.get("count", 0), data=s.get("data", []), days=s.get("days", []))
    _set(cache_key, metric)
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
) -> list[FunnelStep]:
    cache_key = _key(fn="funnel", s1=step1, s2=step2, school_id=school_id, df=date_from, dt=date_to)
    if (cached := _get(cache_key)) is not None:
        return cached

    result = _query(api_key, project_id, {
        "kind": "FunnelsQuery",
        "series": [{"kind": "EventsNode", "event": step1}, {"kind": "EventsNode", "event": step2}],
        "dateRange": _date_range(date_from, date_to),
        "properties": _school_filter(school_id),
        "funnelsFilter": {"funnelVizType": "steps", "funnelOrderType": "ordered", "funnelWindowInterval": 1, "funnelWindowIntervalUnit": "day"},
        "filterTestAccounts": False,
    })
    # PostHog may return flat [step1, step2] or nested [[step1, step2]]
    raw = result.get("result") or result.get("results") or []
    if raw and isinstance(raw[0], list):
        raw_steps = raw[0]   # nested format: [[{name,count}, ...]]
    elif raw and isinstance(raw[0], dict):
        raw_steps = raw      # flat format:   [{name,count}, ...]
    else:
        raw_steps = []
    steps = [FunnelStep(name=s.get("name", s.get("breakdown_value", "")), count=s.get("count", 0)) for s in raw_steps]
    _set(cache_key, steps)
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
) -> list[TopBreakdown]:
    cache_key = _key(fn="breakdown", event=event, bp=breakdown_prop, school_id=school_id, df=date_from, dt=date_to, math=math, mp=math_property)
    if (cached := _get(cache_key)) is not None:
        return cached

    series: dict = {"kind": "EventsNode", "event": event, "math": math}
    if math_property:
        series["math_property"] = math_property

    result = _query(api_key, project_id, {
        "kind": "TrendsQuery",
        "series": [series],
        "dateRange": _date_range(date_from, date_to),
        "properties": _school_filter(school_id),
        "breakdownFilter": {"breakdowns": [{"type": "event", "property": breakdown_prop}], "breakdown_type": "event"},
        "trendsFilter": {"display": "ActionsBarValue"},
        "filterTestAccounts": False,
        "version": 2,
    })

    def _extract_label(r: dict) -> str:
        """breakdown_value may be a list (multi-breakdown) or a plain string."""
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
        # ActionsBarValue stores the aggregated metric in aggregated_value, not count.
        val = r.get("aggregated_value")
        return val if val is not None else r.get("count", 0)

    rows = sorted(
        [{"label": _extract_label(r), "count": _value(r)}
         for r in result.get("results", []) if _valid(r)],
        key=lambda x: x["count"], reverse=True,
    )[:limit]
    breakdown = [TopBreakdown(label=r["label"], count=r["count"]) for r in rows]
    _set(cache_key, breakdown)
    return breakdown
