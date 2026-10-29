"""CSV import orchestration: bounded parse -> validate -> plan -> (dry run | commit)."""

from __future__ import annotations

import csv
import hashlib
import io
import logging
import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.colleges.admin_schemas import ImportSummary
from src.colleges.import_diff import Flags, plan_import
from src.colleges.import_issues import IssueLog
from src.colleges.import_row import ParsedRow, parse_row
from src.colleges.import_rules import COLUMNS, echo
from src.colleges.models import College, CollegeAidRecord

logger = logging.getLogger(__name__)

MAX_BYTES = 2 * 1024 * 1024
MAX_ROWS = 5000
MAX_COLUMNS = 30
FIELD_SIZE_LIMIT = 10_000


class ImportConflict(Exception):
    """A concurrent write broke a unique constraint; nothing was saved."""


def _read_rows(data: bytes, log: IssueLog) -> list[tuple[int, ParsedRow]]:
    """Decode and parse the CSV under the size/shape caps; problems go to ``log``."""
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        log.error(None, None, "file is not valid UTF-8")
        return []
    csv.field_size_limit(FIELD_SIZE_LIMIT)
    reader = csv.DictReader(io.StringIO(text, newline=""))
    try:
        header = reader.fieldnames or []
    except csv.Error:
        log.error(None, None, "header row is not valid CSV")
        return []
    if len(header) > MAX_COLUMNS:
        log.error(1, None, f"too many columns (max {MAX_COLUMNS})")
        return []
    if len(set(header)) != len(header) or set(header) != set(COLUMNS):
        missing = [c for c in COLUMNS if c not in header]
        extra = [echo(c) for c in dict.fromkeys(header) if c not in COLUMNS]
        log.error(1, None, f"header must match the contract exactly; missing: {missing or 'none'}; "
                  f"unexpected or duplicated: {extra[:5] or 'none'}")
        return []
    rows: list[tuple[int, ParsedRow]] = []
    try:
        for raw in reader:
            n = reader.line_num
            if len(rows) >= MAX_ROWS:
                log.error(n, None, f"too many rows (max {MAX_ROWS})")
                return rows
            if None in raw:
                log.error(n, None, "row has more cells than the header")
                continue
            if not any((v or "").strip() for v in raw.values()):
                continue  # blank line
            parsed = parse_row(raw)
            for column, message in parsed.errors:
                log.error(n, column, message)
            rows.append((n, parsed))
    except csv.Error:
        log.error(reader.line_num, None, "malformed CSV row (field too large or bad quoting)")
    return rows


def _apply(db: Session, plan, now: datetime) -> None:
    for cp in plan.colleges.values():
        if cp.college is None:
            cp.college = College(**cp.values)
            db.add(cp.college)
        elif cp.values:
            for column, value in cp.values.items():
                setattr(cp.college, column, value)
            cp.college.updated_at = now
    for rp in plan.records:
        if rp.record is None:
            db.add(CollegeAidRecord(college=rp.college.college, **rp.values))
        else:
            for column, value in rp.values.items():
                setattr(rp.record, column, value)
            rp.record.updated_at = now


def run_import(
    db: Session, data: bytes, flags: Flags, dry_run: bool, expected_sha256: str | None, admin_id: uuid.UUID,
) -> ImportSummary:
    """Validate (and, unless ``dry_run``, apply) one CSV. Any error means nothing is written."""
    sha = hashlib.sha256(data).hexdigest()
    log = IssueLog()
    rows = _read_rows(data, log)
    labels = {r.values["release_label"] for _, r in rows if "release_label" in r.values}
    if len(labels) > 1:
        log.error(None, "release_label", "every row must carry the same release_label")
    valid = [(n, r) for n, r in rows if not r.errors]
    plan = plan_import(valid, db.scalars(select(College)).all(), db.scalars(select(CollegeAidRecord)).all(), flags, log)
    if not rows and not log.errors:
        log.error(None, None, "file has no data rows")
    if not dry_run and not log.has_errors and (expected_sha256 or "").lower() != sha:
        log.error(None, "expected_sha256", "commit needs expected_sha256 equal to the previewed file's sha256")
    summary = ImportSummary(
        file_sha256=sha, release_label=next(iter(labels)) if len(labels) == 1 else None, dry_run=dry_run,
        colleges_created=plan.colleges_created, colleges_updated=plan.colleges_updated,
        records_created=plan.records_created, records_updated=plan.records_updated,
        records_unchanged=plan.unchanged,
    )
    summary.errors, summary.warnings = log.errors, log.warnings()
    summary.truncated = log.truncated
    if dry_run or log.has_errors:
        return summary
    try:
        _apply(db, plan, datetime.now(UTC))
        db.commit()
    except IntegrityError:
        db.rollback()
        raise ImportConflict from None
    logger.info(
        "college aid import committed by admin %s: sha256=%s release=%s colleges_created=%d colleges_updated=%d "
        "records_created=%d records_updated=%d", admin_id, sha, summary.release_label, summary.colleges_created,
        summary.colleges_updated, summary.records_created, summary.records_updated,
    )
    return summary
