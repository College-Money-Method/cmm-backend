"""Admin (super_admin) College Aid endpoints: CSV import and the college list / id export."""

from __future__ import annotations

import csv
import io
import re

from fastapi import APIRouter, File, HTTPException, Query, Response, UploadFile
from fastapi.responses import JSONResponse

from src.auth.deps import AdminDep
from src.colleges import edit_service
from src.colleges.admin_schemas import CollegeListItem, ImportSummary
from src.colleges.import_diff import Flags
from src.colleges.import_service import MAX_BYTES, ImportConflict, run_import
from src.db.deps import DbDep

router = APIRouter(prefix="/api/v1/colleges/admin", tags=["college-aid-admin"])
_SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")


@router.post("/import", response_model=ImportSummary)
def import_csv(
    db: DbDep,
    admin: AdminDep,
    file: UploadFile = File(...),
    dry_run: bool = Query(True),
    expected_sha256: str | None = Query(None),
    allow_identity_change: bool = Query(False),
    allow_older_release: bool = Query(False),
):
    """Validate a contract CSV; ``dry_run=false`` writes it, but only if it hashes to ``expected_sha256``."""
    data = file.file.read(MAX_BYTES + 1)  # bounded read: never buffer more than the cap
    if len(data) > MAX_BYTES:
        raise HTTPException(status_code=413, detail=f"File too large (max {MAX_BYTES // (1024 * 1024)} MB)")
    if expected_sha256 is not None and not _SHA256.match(expected_sha256):
        expected_sha256 = ""  # reported as a mismatch by the service on commit
    try:
        summary = run_import(
            db, data, Flags(allow_identity_change, allow_older_release), dry_run, expected_sha256, admin.user_id
        )
    except ImportConflict:
        raise HTTPException(status_code=409, detail="The import conflicted with a concurrent change; nothing was saved")
    if summary.errors:
        return JSONResponse(status_code=422, content=summary.model_dump(mode="json"))
    return summary


@router.get("", response_model=list[CollegeListItem])
def list_colleges(db: DbDep, _admin: AdminDep, fmt: str | None = Query(None, alias="format")):
    """Every college (hidden too). ``?format=csv`` is the ``college_id,name,state`` export for the converter."""
    items = edit_service.list_colleges(db)
    if fmt is None:
        return items
    if fmt != "csv":
        raise HTTPException(status_code=422, detail="format must be csv")
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(["college_id", "name", "state"])
    writer.writerows([str(i.college_id), i.name, i.state] for i in items)
    return Response(content=out.getvalue(), media_type="text/csv")
