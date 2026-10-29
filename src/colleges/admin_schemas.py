"""Pydantic schemas for the College Aid admin endpoints (import summary + edit forms)."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from src.colleges.json_decimal import JsonDecimal


class ImportIssue(BaseModel):
    row: int | None = None
    column: str | None = None
    message: str


class ImportWarning(BaseModel):
    row: int | None = None
    college: str | None = None
    message: str


class ImportSummary(BaseModel):
    file_sha256: str
    release_label: str | None = None
    colleges_created: int = 0
    colleges_updated: int = 0
    records_created: int = 0
    records_updated: int = 0
    records_unchanged: int = 0
    errors: list[ImportIssue] = []
    warnings: list[ImportWarning] = []
    truncated: bool = False
    dry_run: bool = True


class CollegeListItem(BaseModel):
    college_id: uuid.UUID
    name: str
    state: str
    control: str
    hidden: bool
    latest_cds_year: str | None = None  # newest real year; N/R-only colleges have none
    record_count: int


class RecordOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    cds_year: str | None = None
    release_label: str
    source: str | None = None
    coa_in_state: JsonDecimal | None = None
    coa_out_of_state: JsonDecimal | None = None
    coa_year: str | None = None
    coa_estimated: bool
    full_time_undergrads: int | None = None
    aid_application: str | None = None
    pct_need_met: JsonDecimal | None = None
    pct_non_need_receiving_merit: JsonDecimal | None = None
    avg_merit_award: JsonDecimal | None = None
    manually_edited_at: datetime | None = None
    manually_edited_by: uuid.UUID | None = None


class CollegeDetail(CollegeListItem):
    records: list[RecordOut]


class CollegePatch(BaseModel):
    """Identity edit; every field optional. Text/state/control go through import_rules."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    state: str | None = None
    control: str | None = None
    hidden: bool | None = None


class RecordPatch(BaseModel):
    """Partial record edit. Only fields actually sent are applied; an explicit null clears.

    Values stay loosely typed here on purpose: ``import_rules`` parsers are the single
    rule set and produce the 422 messages (a number or a string are both accepted).
    """

    model_config = ConfigDict(extra="forbid")

    cds_year: str | None = None
    release_label: str | None = None
    source: str | None = None
    coa_in_state: str | int | float | None = None
    coa_out_of_state: str | int | float | None = None
    coa_year: str | None = None
    coa_estimated: bool | None = None
    full_time_undergrads: str | int | float | None = None
    aid_application: str | None = None
    pct_need_met: str | int | float | None = None
    pct_non_need_receiving_merit: str | int | float | None = None
    avg_merit_award: str | int | float | None = None
    data_notes: str | None = None


class RecordCreate(RecordPatch):
    """New record by hand; release_label defaults to the current YYYY-MM in the service."""

