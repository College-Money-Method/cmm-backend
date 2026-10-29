"""Import and list endpoints, mounted on a fresh app with overridden auth/db."""

from __future__ import annotations

import csv
import io
import os
from pathlib import Path

import pytest
from sqlalchemy import select

from src.auth.deps import get_current_user
from src.auth.schemas import CurrentUser
from src.colleges.models import College, CollegeAidRecord
from tests.colleges.admin_helpers import (
    ADMIN_ID,
    BASE_URL,
    commit,
    make_csv,
    row,
    upload,
)

REAL_CSV = Path(os.environ.get("COLLEGE_AID_REAL_CSV", "/nonexistent"))


def test_dry_run_writes_nothing_and_reports_counts(client, db):
    resp = upload(client, make_csv([row(), row(name="Beta U")]), dry_run="true")
    body = resp.json()
    assert resp.status_code == 200
    assert (body["colleges_created"], body["records_created"], body["dry_run"]) == (2, 2, True)
    assert body["release_label"] == "2026-09" and len(body["file_sha256"]) == 64
    assert db.scalars(select(College)).all() == []


def test_commit_requires_matching_sha(client, db):
    data = make_csv([row()])
    for params in ({}, {"expected_sha256": "0" * 64}, {"expected_sha256": "bogus"}):
        resp = upload(client, data, dry_run="false", **params)
        assert resp.status_code == 422
        assert resp.json()["errors"][0]["column"] == "expected_sha256"
    assert db.scalars(select(College)).all() == []
    ok = commit(client, data)
    assert ok.status_code == 200 and ok.json()["dry_run"] is False
    assert len(db.scalars(select(College)).all()) == 1


def test_any_error_means_422_and_nothing_written(client, db):
    resp = commit(client, make_csv([row(), row(name="Bad", pct_need_met="75")]))
    assert resp.status_code == 422
    body = resp.json()
    assert body["errors"][0]["row"] == 3 and body["errors"][0]["column"] == "pct_need_met"
    assert db.scalars(select(College)).all() == []


def test_header_must_be_exact(client):
    extra = make_csv([row()])  # exact header set
    assert upload(client, extra, dry_run="true").status_code == 200
    lines = extra.decode().splitlines()
    missing = "\n".join([lines[0].replace("data_notes", "extra_col")] + lines[1:]).encode()
    resp = upload(client, missing, dry_run="true")
    assert resp.status_code == 422 and "header" in resp.json()["errors"][0]["message"]
    wide = ",".join(f"c{i}" for i in range(31)).encode()
    assert "too many columns" in upload(client, wide, dry_run="true").json()["errors"][0]["message"]


def test_size_row_and_encoding_limits(client):
    assert upload(client, b"x" * (2 * 1024 * 1024 + 1)).status_code == 413
    rows = [row(name=f"College {i}") for i in range(5001)]
    resp = upload(client, make_csv(rows), dry_run="true")
    assert resp.status_code == 422 and "too many rows" in resp.json()["errors"][-1]["message"]
    assert upload(client, b"\xff\xfe\x00bad", dry_run="true").status_code == 422
    huge = make_csv([row(data_notes="x" * 20_000)])
    assert upload(client, huge, dry_run="true").status_code == 422  # field size limit, no 500


def test_bom_and_blank_lines_accepted(client):
    data = b"\xef\xbb\xbf" + make_csv([row()]) + b"\n\n"
    assert upload(client, data, dry_run="true").status_code == 200


def test_warning_cap_truncates(client, db):
    upload_rows = [row(name=f"College {i}") for i in range(150)]
    body = upload(client, make_csv(upload_rows), dry_run="true").json()
    assert body["truncated"] is True and len(body["warnings"]) == 100


def test_non_admin_and_anonymous_rejected(client, acting_as):
    acting_as.user = CurrentUser(user_id=ADMIN_ID, role="viewer", school_id=None)
    assert upload(client, make_csv([row()])).status_code == 403
    assert client.get(BASE_URL).status_code == 403
    client.app.dependency_overrides.pop(get_current_user)
    assert client.get(BASE_URL).status_code == 401


def test_list_and_csv_export(client, db):
    commit(client, make_csv([row(), row(name="Beta U", cds_year="2024-25"), row(name="Gamma", cds_year="")]))
    items = {i["name"]: i for i in client.get(BASE_URL).json()}
    assert items["Alpha College"]["latest_cds_year"] == "2025-26" and items["Alpha College"]["record_count"] == 1
    assert items["Gamma"]["latest_cds_year"] is None and items["Gamma"]["hidden"] is False
    resp = client.get(BASE_URL, params={"format": "csv"})
    assert resp.headers["content-type"].startswith("text/csv")
    parsed = list(csv.reader(io.StringIO(resp.text)))
    assert parsed[0] == ["college_id", "name", "state"] and len(parsed) == 4
    assert client.get(BASE_URL, params={"format": "xml"}).status_code == 422


@pytest.mark.skipif(not REAL_CSV.exists(), reason="real Sept-2026 CSV not available")
def test_real_csv_dry_run_commit_and_reimport(client, db):
    data = REAL_CSV.read_bytes()
    preview = upload(client, data, dry_run="true").json()
    assert preview["errors"] == [] and preview["colleges_created"] == 463 and preview["truncated"]
    assert len(preview["warnings"]) == 100 and db.scalars(select(College)).all() == []
    assert commit(client, data).status_code == 200
    again = upload(client, data, dry_run="true").json()
    assert (again["colleges_created"], again["records_unchanged"], again["records_updated"]) == (0, 463, 0)
    assert len(db.scalars(select(CollegeAidRecord)).all()) == 463
