"""Shared helpers for the College Aid admin tests: CSV builder and upload shortcuts.

The admin ``client`` / ``acting_as`` fixtures live in ``conftest.py``.
"""

from __future__ import annotations

import csv
import io
import uuid

from src.colleges.import_rules import COLUMNS

ADMIN_ID = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
IMPORT_URL = "/api/v1/colleges/admin/import"
BASE_URL = "/api/v1/colleges/admin"


def row(**over: str) -> dict[str, str]:
    """One valid contract row; override columns by keyword."""
    base = {
        "college_id": "", "name": "Alpha College", "state": "NY", "region": "Mid-Atlantic", "control": "private",
        "coa_in_state": "", "coa_out_of_state": "60000", "coa_year": "2025-26", "coa_estimated": "true",
        "full_time_undergrads": "1000", "aid_application": "FAFSA", "pct_need_met": "0.8",
        "pct_non_need_receiving_merit": "0.3", "avg_merit_award": "5000", "cds_year": "2025-26",
        "source": "Test", "release_label": "2026-09", "data_notes": "",
    }
    return {**base, **over}


def make_csv(rows: list[dict[str, str]], columns: tuple[str, ...] = COLUMNS) -> bytes:
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=list(columns), lineterminator="\n", extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    return out.getvalue().encode("utf-8")


def upload(client, data: bytes, **params):
    return client.post(IMPORT_URL, params=params, files={"file": ("aid.csv", data, "text/csv")})


def commit(client, data: bytes, **params):
    """Dry-run then commit with the previewed hash; returns the commit response."""
    preview = upload(client, data, dry_run="true", **params)
    return upload(client, data, dry_run="false", expected_sha256=preview.json()["file_sha256"], **params)
