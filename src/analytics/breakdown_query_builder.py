"""HogQL builder for the batched breakdown query (and the identifier guards
shared with the other batched helpers).

A dashboard tab asks for several rankings at once — top videos, avg % watched,
top resources, top topics. Expressed as UNION ALL (one SELECT per ranking)
ClickHouse reads the events table once PER BRANCH: the Content tab's four
rankings meant four full scans of exactly the same rows, ~11s of query time.

When the specs are event-disjoint — no event name appears in two specs — a row
can belong to at most one ranking, so the whole set collapses into a SINGLE
scan: `multiIf` stamps each row with its ranking (`kind`) and that ranking's
breakdown property (`label`), and one `GROUP BY kind, label` yields every
ranking at once. The result shape is identical, so the caller is unchanged.

Specs that SHARE an event fall back to UNION ALL: `multiIf` takes the first
matching arm only, which would silently drop that row from every later ranking.
"""

from __future__ import annotations

import re

# Property/event/key names are code-supplied constants, but validate before
# interpolation — nothing here may come from a request.
PROP_RE = re.compile(r"^[A-Za-z_$][A-Za-z0-9_$]*$")
EVENT_RE = re.compile(r"^[A-Za-z0-9_$]+$")
KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def ident(value: str, pattern: re.Pattern, what: str) -> str:
    if not pattern.match(value):
        raise ValueError(f"Invalid {what} for HogQL interpolation: {value!r}")
    return value


def event_names(spec: dict) -> list[str]:
    """Validated event name(s) for a series/breakdown spec.

    `event` accepts a single name or a list — a list means "count these events
    together as one series" (e.g. site search = search_query +
    global_search_performed, which fire from two different search surfaces)."""
    raw = spec["event"]
    events = raw if isinstance(raw, list) else [raw]
    return [ident(e, EVENT_RE, "event") for e in events]


def event_match(events: list[str]) -> str:
    if len(events) == 1:
        return f"event = '{events[0]}'"
    return "event IN (" + ", ".join(f"'{e}'" for e in events) + ")"


def _spec_match(spec: dict) -> str:
    """Full row predicate for one spec: its event(s) plus its extra_filter.

    `extra_filter` already carries its leading " AND " (code-supplied fragment,
    see get_batched_breakdowns' docstring)."""
    return event_match(event_names(spec)) + (spec.get("extra_filter") or "")


def _events_are_disjoint(specs: list[dict]) -> bool:
    """True when no event name is claimed by more than one spec."""
    seen: set[str] = set()
    for sp in specs:
        events = set(event_names(sp))
        if events & seen:
            return False
        seen |= events
    return True


def build_breakdowns_hogql(specs: list[dict], where: str) -> str:
    """HogQL yielding (kind, label, val) rows for every spec in `specs`.

    `where` is the shared, pre-validated scope clause (date + school + cycle),
    written without a leading "AND".
    """
    if _events_are_disjoint(specs):
        return _single_scan_hogql(specs, where)
    return _union_all_hogql(specs, where)


def _single_scan_hogql(specs: list[dict], where: str) -> str:
    """One pass over events; multiIf routes each row to its ranking."""
    all_events: list[str] = []
    kind_arms: list[str] = []
    label_arms: list[str] = []
    avg_arms: list[str] = []
    avg_keys: list[str] = []

    for sp in specs:
        all_events.extend(event_names(sp))
        key = ident(sp["key"], KEY_RE, "breakdown key")
        prop = ident(sp["prop"], PROP_RE, "property")
        cond = _spec_match(sp)
        kind_arms.append(f"{cond}, '{key}'")
        label_arms.append(f"{cond}, toString(properties.{prop})")
        if sp.get("math") == "avg":
            mp = ident(sp["math_prop"], PROP_RE, "math property")
            avg_arms.append(f"{cond}, toFloat(ifNull(properties.{mp}, '0'))")
            avg_keys.append(key)

    kind_expr = "multiIf(" + ", ".join(kind_arms) + ", '')"
    label_expr = "multiIf(" + ", ".join(label_arms) + ", NULL)"
    events_in = ", ".join(f"'{e}'" for e in dict.fromkeys(all_events))

    if avg_keys:
        # `kind` is a GROUP BY key, so it is constant inside each group — the
        # avg branch is only ever read for the specs that asked for it.
        inner_cols = (
            f"{kind_expr} AS kind, {label_expr} AS label, "
            "multiIf(" + ", ".join(avg_arms) + ", 0.0) AS mval"
        )
        keys_in = ", ".join(f"'{k}'" for k in avg_keys)
        val_expr = f"if(kind IN ({keys_in}), avg(mval), toFloat(count()))"
    else:
        inner_cols = f"{kind_expr} AS kind, {label_expr} AS label"
        val_expr = "toFloat(count())"

    return (
        f"SELECT kind, label, {val_expr} AS val FROM ("
        f"SELECT {inner_cols} FROM events "
        f"WHERE {where} AND event IN ({events_in})"
        ") WHERE kind != '' AND label IS NOT NULL AND label != '' "
        "GROUP BY kind, label"
    )


def _union_all_hogql(specs: list[dict], where: str) -> str:
    """One scan per spec — correct for any spec set, used when events overlap."""
    branches: list[str] = []
    for sp in specs:
        key = ident(sp["key"], KEY_RE, "breakdown key")
        prop = ident(sp["prop"], PROP_RE, "property")
        if sp.get("math") == "avg":
            mp = ident(sp["math_prop"], PROP_RE, "math property")
            val = f"avg(toFloat(ifNull(properties.{mp}, '0')))"
        else:
            val = "toFloat(count())"
        match = event_match(event_names(sp))
        extra = sp.get("extra_filter") or ""
        branches.append(
            f"SELECT '{key}' AS kind, toString(properties.{prop}) AS label, {val} AS val "
            f"FROM events WHERE {match} AND {where}{extra} "
            f"AND isNotNull(properties.{prop}) GROUP BY label"
        )
    return " UNION ALL ".join(branches)
