"""Import planning: match rows to colleges/records and work out what would change.

Pure with respect to the database: it reads already-loaded ORM objects and returns a
``Plan``; nothing is mutated, so a dry run and a commit share exactly this code.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from src.colleges.import_issues import IssueLog
from src.colleges.import_row import ParsedRow
from src.colleges.import_rules import FACT_FIELDS, REGION_BY_STATE, echo
from src.colleges.models import College, CollegeAidRecord
from src.colleges.name_key import normalize_name

STALE_YEARS = 3


@dataclass
class Flags:
    allow_identity_change: bool = False
    allow_older_release: bool = False


@dataclass
class CollegePlan:
    """A college to create (``college`` None) or an existing one with pending changes."""

    college: College | None
    values: dict[str, Any]  # create: full values; existing: only changed columns
    first_row: int
    signature: tuple


@dataclass
class RecordPlan:
    college: CollegePlan
    record: CollegeAidRecord | None  # None = create
    values: dict[str, Any]


@dataclass
class Plan:
    colleges: dict[Any, CollegePlan] = field(default_factory=dict)  # identity -> plan
    records: list[RecordPlan] = field(default_factory=list)
    unchanged: int = 0

    @property
    def colleges_created(self) -> int:
        return sum(1 for c in self.colleges.values() if c.college is None)

    @property
    def colleges_updated(self) -> int:
        return sum(1 for c in self.colleges.values() if c.college is not None and c.values)

    @property
    def records_created(self) -> int:
        return sum(1 for r in self.records if r.record is None)

    @property
    def records_updated(self) -> int:
        return sum(1 for r in self.records if r.record is not None)


def _same(old: Any, new: Any) -> bool:
    if isinstance(new, Decimal) and old is not None:
        return Decimal(str(old)) == new
    return old == new


def _show(value: Any) -> str:
    if value is None:
        return "empty"
    return echo(value) if isinstance(value, str) else str(value)


def _year_start(year: str | None) -> int | None:
    return int(year[:4]) if year else None


def plan_import(
    rows: Iterable[tuple[int, ParsedRow]], colleges: Iterable[College], records: Iterable[CollegeAidRecord],
    flags: Flags, issues: IssueLog,
) -> Plan:
    parsed = [(n, r.values, r.warnings) for n, r in rows]
    by_id = {c.id: c for c in colleges}
    by_key = {(c.name_key, c.state): c for c in by_id.values()}
    by_unitid = {c.unitid: c for c in by_id.values() if c.unitid is not None}
    stored: dict[Any, dict[str | None, CollegeAidRecord]] = defaultdict(dict)
    for rec in records:
        stored[rec.college_id][rec.cds_year] = rec
    starts = [s for _, v, _w in parsed if (s := _year_start(v.get("cds_year")))]
    file_max = max(starts, default=None)
    plan, claimed, seen = Plan(), {}, set()

    for n, v, row_warnings in parsed:
        for message in row_warnings:
            issues.warn(n, v["name"], message)
        key = (normalize_name(v["name"]), v["state"])
        cplan = _resolve_college(n, v, key, (by_id, by_unitid, by_key), claimed, flags, plan, issues)
        if cplan is None:
            continue
        identity = cplan.college.id if cplan.college else ("new", key)
        if (identity, v["cds_year"]) in seen:
            issues.error(n, "cds_year", "duplicate row for the same college and year")
            continue
        seen.add((identity, v["cds_year"]))
        existing = stored[cplan.college.id].get(v["cds_year"]) if cplan.college else None
        _plan_record(n, v, cplan, existing, file_max, flags, plan, issues)
    return plan


def _resolve_college(n, v, key, lookups, claimed, flags, plan, issues) -> CollegePlan | None:
    """Match order: college_id, then UNITID, then name + state."""
    by_id, by_unitid, by_key = lookups
    uid = v.get("unitid")
    if v["college_id"]:
        college = by_id.get(v["college_id"])
    else:
        college = by_unitid.get(uid) if uid in by_unitid else by_key.get(key)
    if v["college_id"] and college is None:
        issues.error(n, "college_id", f"college_id not found: {echo(v['college_id'])}")
        return None
    identity = college.id if college else ("new", key)
    owner = by_key.get(key)
    if claimed.setdefault(key, identity) != identity or (owner is not None and owner is not college):
        issues.error(n, "name", "name and state already belong to a different college")
        return None
    uid_owner = by_unitid.get(uid)
    if uid is not None and (claimed.setdefault(("unitid", uid), identity) != identity
                            or (uid_owner is not None and uid_owner is not college)):
        issues.error(n, "unitid", f"UNITID {uid} already belongs to a different college")
        return None
    signature = (key, v["control"], uid)
    if college is None:
        if identity not in plan.colleges:
            values = {k: v[k] for k in ("name", "state", "region", "control")} | {"name_key": key[0]}
            if uid is not None:
                values["unitid"] = uid
            plan.colleges[identity] = CollegePlan(None, values, n, signature)
            issues.warn(n, v["name"], "new college", important=False)
        elif plan.colleges[identity].signature != signature:
            issues.error(n, "control", "rows disagree on the college's control or UNITID")
            return None
        return plan.colleges[identity]
    if identity in plan.colleges:
        if plan.colleges[identity].signature != signature:
            issues.error(n, "name", "rows disagree on the college's name, state, control or UNITID")
            return None
        return plan.colleges[identity]
    changes: dict[str, Any] = {}
    for column in ("state", "control"):
        if getattr(college, column) != v[column]:
            if not flags.allow_identity_change:
                issues.error(n, column, f"{column} change {_show(getattr(college, column))} -> {_show(v[column])} "
                          "needs allow_identity_change")
                return None
            changes[column] = v[column]
    if "state" in changes:
        changes["region"] = REGION_BY_STATE[v["state"]]
    if uid is not None and college.unitid != uid:  # a blank cell keeps the stored UNITID
        if college.unitid is not None and not flags.allow_identity_change:
            issues.error(n, "unitid", f"unitid change {college.unitid} -> {uid} needs allow_identity_change")
            return None
        issues.warn(n, v["name"], f"unitid: {_show(college.unitid)} -> {uid}", important=college.unitid is not None)
        changes["unitid"] = uid
    if college.name != v["name"]:
        issues.warn(n, v["name"], f"name: {_show(college.name)} -> {_show(v['name'])}", important=True)
        changes |= {"name": v["name"], "name_key": key[0]}
    plan.colleges[identity] = CollegePlan(college, changes, n, signature)
    return plan.colleges[identity]


def _plan_record(n, v, cplan, existing, file_max, flags, plan, issues) -> None:
    name = v["name"]
    start = _year_start(v["cds_year"])
    if v["cds_year"] is None:
        issues.warn(n, name, "cds_year not reported")
    elif file_max and start <= file_max - STALE_YEARS:
        issues.warn(n, name, f"latest year {v['cds_year']} is {file_max - start} years behind the file")
    if existing is None:
        values = {"cds_year": v["cds_year"], "release_label": v["release_label"]}
        values |= {f: v[f] for f in FACT_FIELDS if v[f] is not None}
        plan.records.append(RecordPlan(cplan, None, values))
        return
    if existing.release_label > v["release_label"]:
        if not flags.allow_older_release:
            issues.error(n, "release_label", f"release {v['release_label']} is older than stored "
                      f"{existing.release_label} (needs allow_older_release)")
            return
        issues.warn(n, name, f"older release {v['release_label']} replaces {existing.release_label}", important=True)
    changes: dict[str, Any] = {}
    manual = existing.manually_edited_at is not None
    for column in FACT_FIELDS:
        new, old = v[column], getattr(existing, column)
        if new is None:
            if old is not None and column != "coa_estimated":
                issues.warn(n, name, f"kept existing {column}")
        elif not _same(old, new):
            changes[column] = new
            text = f"{column}: {_show(old)} -> {_show(new)}"
            issues.warn(n, name, f"overwrites manual edit: {text}" if manual else text, important=manual)
    if changes:
        plan.records.append(RecordPlan(cplan, existing, changes | {"release_label": v["release_label"]}))
    else:
        plan.unchanged += 1
