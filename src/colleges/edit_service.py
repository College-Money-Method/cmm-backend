"""Admin manual-correction logic: college identity edits and per-year record CRUD.

Every value goes through the same ``import_rules`` parsers as the CSV import, so
forms and files obey one rule set. Failures raise ``HTTPException`` (422 invalid,
404 missing, 409 clash); exceptions and SQL are never echoed.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.colleges import import_rules as rules
from src.colleges.admin_schemas import (
    CollegeCreate,
    CollegeDetail,
    CollegeListItem,
    CollegePatch,
    RecordCreate,
    RecordOut,
    RecordPatch,
)
from src.colleges.models import College, CollegeAidRecord
from src.colleges.name_key import normalize_name

logger = logging.getLogger(__name__)

_PARSERS: dict[str, Any] = dict(rules.FACT_PARSERS) | {"cds_year": rules.parse_year, "release_label": rules.parse_release}


def _invalid(errors: list[tuple[str, str]]) -> HTTPException:
    detail = [{"loc": ["body", field], "msg": msg, "type": "value_error"} for field, msg in errors]
    return HTTPException(status_code=422, detail=detail)


def _college(db: Session, college_id: uuid.UUID) -> College:
    college = db.get(College, college_id)
    if college is None:
        raise HTTPException(status_code=404, detail="College not found")
    return college


def _record(db: Session, record_id: uuid.UUID) -> CollegeAidRecord:
    record = db.get(CollegeAidRecord, record_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Record not found")
    return record


def _commit_or_conflict(db: Session, message: str) -> None:
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail=message) from None


def _item(college: College, record_count: int, latest: str | None) -> CollegeListItem:
    return CollegeListItem(
        college_id=college.id, name=college.name, state=college.state, control=college.control,
        hidden=college.hidden_at is not None, latest_cds_year=latest, record_count=record_count,
    )


def list_colleges(db: Session) -> list[CollegeListItem]:
    stats = {
        cid: (count, latest)
        for cid, count, latest in db.execute(
            select(CollegeAidRecord.college_id, func.count(), func.max(CollegeAidRecord.cds_year)).group_by(
                CollegeAidRecord.college_id
            )
        )
    }
    colleges = db.scalars(select(College).order_by(College.state, College.name)).all()
    return [_item(c, *stats.get(c.id, (0, None))) for c in colleges]


def get_detail(db: Session, college_id: uuid.UUID) -> CollegeDetail:
    college = _college(db, college_id)
    records = sorted(college.records, key=lambda r: r.cds_year or "", reverse=True)  # N/R sorts last
    years = [r.cds_year for r in records if r.cds_year]
    item = _item(college, len(records), years[0] if years else None)
    return CollegeDetail(**item.model_dump(), records=[RecordOut.model_validate(r) for r in records])


def _identity_changes(body: CollegeCreate | CollegePatch) -> dict[str, Any]:
    """Parse the identity fields the client sent, adding the derived name_key and region."""
    errors, changes = [], {}
    for column, parser in (("name", rules.parse_name), ("state", rules.parse_state), ("control", rules.parse_control)):
        if column in body.model_fields_set:
            try:
                changes[column] = parser(getattr(body, column))
            except rules.RuleError as exc:
                errors.append((column, str(exc)))
    if "hidden" in body.model_fields_set and body.hidden is None:
        errors.append(("hidden", "must be true or false"))
    if "name" in changes and not normalize_name(changes["name"]):
        errors.append(("name", "must contain letters or digits"))
    if errors:
        raise _invalid(errors)
    if "name" in changes:
        changes["name_key"] = normalize_name(changes["name"])
    if "state" in changes:
        changes["region"] = rules.REGION_BY_STATE[changes["state"]]
    return changes


def create_college(db: Session, body: CollegeCreate, admin_id: uuid.UUID) -> CollegeListItem:
    """A college with no records yet; it reaches the explorer once a record is added."""
    college = College(**_identity_changes(body))
    db.add(college)
    _commit_or_conflict(db, "Another college already has this name and state")
    logger.info("college aid college %s created by admin %s", college.id, admin_id)
    return _item(college, 0, None)


def patch_college(db: Session, college_id: uuid.UUID, body: CollegePatch, admin_id: uuid.UUID) -> CollegeListItem:
    college = _college(db, college_id)
    changes = _identity_changes(body)
    log = {k: f"{rules.echo(getattr(college, k))} -> {rules.echo(v)}" for k, v in changes.items() if getattr(college, k) != v}
    for column, value in changes.items():
        setattr(college, column, value)
    if "hidden" in body.model_fields_set:
        hidden = bool(body.hidden)
        if hidden != (college.hidden_at is not None):
            log["hidden"] = f"{college.hidden_at is not None} -> {hidden}"
        college.hidden_at = (college.hidden_at or datetime.now(UTC)) if hidden else None
    if log:
        college.updated_at = datetime.now(UTC)
    _commit_or_conflict(db, "Another college already has this name and state")
    logger.info("college aid college %s edited by admin %s: %s", college.id, admin_id, log)
    return _item(college, len(college.records), max((r.cds_year for r in college.records if r.cds_year), default=None))


def delete_college(db: Session, college_id: uuid.UUID, admin_id: uuid.UUID) -> None:
    college = _college(db, college_id)
    count = len(college.records)
    db.delete(college)
    db.commit()
    logger.info("college aid college %s deleted by admin %s (%d records)", college_id, admin_id, count)


def _parse_payload(body: RecordPatch) -> dict[str, Any]:
    """Parse only the fields the client sent; an explicit null clears the stored value."""
    values, errors = {}, []
    for column in body.model_fields_set:
        try:
            value = _PARSERS[column](getattr(body, column))
            values[column] = False if column == "coa_estimated" and value is None else value
        except rules.RuleError as exc:
            errors.append((column, str(exc)))
    if errors:
        raise _invalid(errors)
    return values


def _check_coa(merged: dict[str, Any]) -> None:
    try:
        rules.check_coa_year(merged)
    except rules.RuleError as exc:
        raise _invalid([("coa_year", str(exc))]) from None


def create_record(db: Session, college_id: uuid.UUID, body: RecordCreate, admin_id: uuid.UUID) -> RecordOut:
    college, values = _college(db, college_id), _parse_payload(body)
    values.setdefault("release_label", datetime.now(UTC).strftime("%Y-%m"))
    values.setdefault("cds_year", None)
    _check_coa(values)
    if any(r.cds_year == values["cds_year"] for r in college.records):
        raise HTTPException(status_code=409, detail="This college already has a record for that year")
    now = datetime.now(UTC)
    record = CollegeAidRecord(college=college, manually_edited_at=now, manually_edited_by=admin_id, **values)
    db.add(record)
    _commit_or_conflict(db, "This college already has a record for that year")
    logger.info("college aid record %s created by admin %s for college %s year %s", record.id, admin_id, college_id, values["cds_year"])
    return RecordOut.model_validate(record)


def patch_record(db: Session, record_id: uuid.UUID, body: RecordPatch, admin_id: uuid.UUID) -> RecordOut:
    record, values = _record(db, record_id), _parse_payload(body)
    _check_coa({c: values.get(c, getattr(record, c)) for c in ("coa_in_state", "coa_out_of_state", "coa_year")})
    changed = {c: v for c, v in values.items() if getattr(record, c) != v}
    if "cds_year" in changed and any(
        r.cds_year == changed["cds_year"] for r in record.college.records if r.id != record.id
    ):
        raise HTTPException(status_code=409, detail="This college already has a record for that year")
    if changed:
        log = {c: f"{rules.echo(getattr(record, c))} -> {rules.echo(v)}" for c, v in changed.items()}
        for column, value in changed.items():
            setattr(record, column, value)
        now = datetime.now(UTC)
        record.manually_edited_at, record.manually_edited_by, record.updated_at = now, admin_id, now
        _commit_or_conflict(db, "This college already has a record for that year")
        logger.info("college aid record %s edited by admin %s: %s", record_id, admin_id, log)
    return RecordOut.model_validate(record)


def delete_record(db: Session, record_id: uuid.UUID, admin_id: uuid.UUID) -> None:
    record = _record(db, record_id)
    db.delete(record)
    db.commit()
    logger.info("college aid record %s deleted by admin %s", record_id, admin_id)
