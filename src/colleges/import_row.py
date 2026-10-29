"""Row-level CSV parsing: raw dict -> ParsedRow (clean values, errors, warnings)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from src.colleges.import_rules import (
    FACT_FIELDS,
    FACT_PARSERS,
    REGION_BY_STATE,
    RuleError,
    check_coa_year,
    clean,
    echo,
    parse_control,
    parse_name,
    parse_release,
    parse_state,
    parse_year,
)

_TEXT_COLUMNS = ("name", "aid_application", "source", "data_notes")


@dataclass
class ParsedRow:
    """One validated CSV row: clean values, per-column errors, row-level warnings."""

    values: dict[str, Any] = field(default_factory=dict)
    errors: list[tuple[str | None, str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _outlier_warnings(v: dict[str, Any]) -> list[str]:
    """Plausibility checks: odd combinations are flagged, never rejected."""
    out: list[str] = []
    k, m, need = v.get("pct_non_need_receiving_merit"), v.get("avg_merit_award"), v.get("pct_need_met")
    if k == 1 and m == 0:
        out.append("outlier: all non-need students get merit but the average award is 0")
    if k == 0 and m:
        out.append("outlier: no non-need students get merit but an average award is given")
    if need is not None and need < Decimal("0.10"):
        out.append(f"outlier: pct_need_met is very low ({need})")
    coa = v.get("coa_out_of_state") or v.get("coa_in_state")
    if m and coa and m / coa > Decimal("0.6"):
        out.append("outlier: average merit award is over 60% of the cost of attendance")
    if v.get("coa_year") and v.get("cds_year") and v["coa_year"] != v["cds_year"]:
        out.append(f"COA year {v['coa_year']} differs from CDS year {v['cds_year']}")
    return out


def parse_row(raw: dict[str, Any]) -> ParsedRow:
    row = ParsedRow()

    def take(column: str, parser) -> None:
        try:
            row.values[column] = parser(raw.get(column))
        except RuleError as exc:
            row.errors.append((column, str(exc)))

    raw_id = clean(raw.get("college_id"))
    if raw_id is None:
        row.values["college_id"] = None
    else:
        try:
            row.values["college_id"] = uuid.UUID(str(raw_id))
        except ValueError:
            row.errors.append(("college_id", f"not a valid id: {echo(raw_id)}"))
    take("name", parse_name)
    take("state", parse_state)
    take("control", parse_control)
    take("cds_year", parse_year)
    take("release_label", parse_release)
    for column in FACT_FIELDS:
        take(column, FACT_PARSERS[column])
    state, region = row.values.get("state"), clean(raw.get("region"))
    if state and region is not None and REGION_BY_STATE[state] != region:
        row.errors.append(("region", f"region {echo(region)} does not match state {state}"))
    if state:
        row.values["region"] = REGION_BY_STATE[state]
    if not row.errors:
        try:
            check_coa_year(row.values)
        except RuleError as exc:
            row.errors.append(("coa_year", str(exc)))
    for column in _TEXT_COLUMNS:  # spreadsheet formula injection guard
        text = row.values.get(column)
        if isinstance(text, str) and text.startswith(("=", "+", "-", "@")):
            row.warnings.append(f"{column} starts with '{text[0]}' (spreadsheet formula risk)")
    if not row.errors:
        row.warnings.extend(_outlier_warnings(row.values))
    return row
