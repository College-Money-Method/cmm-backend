"""Manual-correction endpoints: college edit, record add/edit/delete, and import interplay."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select

from src.colleges.models import College, CollegeAidRecord
from tests.colleges.admin_helpers import (
    ADMIN_ID,
    BASE_URL,
    commit,
    make_csv,
    row,
)

MISSING = uuid.uuid4()


def seed(client, *rows):
    """Import rows and return {name: college_id}."""
    assert commit(client, make_csv(list(rows))).status_code == 200
    return {c["name"]: c["college_id"] for c in client.get(BASE_URL).json()}


def records_of(client, college_id):
    return client.get(f"{BASE_URL}/{college_id}").json()["records"]


def test_detail_orders_newest_first_with_nr_last(client):
    ids = seed(client, row(cds_year="2023-24"), row(cds_year=""), row(cds_year="2025-26"))
    detail = client.get(f"{BASE_URL}/{ids['Alpha College']}").json()
    assert [r["cds_year"] for r in detail["records"]] == ["2025-26", "2023-24", None]
    assert detail["latest_cds_year"] == "2025-26" and detail["record_count"] == 3
    assert detail["records"][0]["manually_edited_at"] is None
    assert client.get(f"{BASE_URL}/{MISSING}").status_code == 404


def test_patch_college_rederives_region_and_hides(client, db):
    cid = seed(client, row())["Alpha College"]
    resp = client.patch(f"{BASE_URL}/{cid}", json={"state": "CA", "name": "Alpha U", "hidden": True})
    assert resp.status_code == 200
    body = resp.json()
    assert (body["state"], body["name"], body["hidden"]) == ("CA", "Alpha U", True)
    db.expire_all()
    college = db.get(College, uuid.UUID(cid))
    assert college.region == "West Coast" and college.name_key == "alpha u"
    assert client.patch(f"{BASE_URL}/{cid}", json={"hidden": False}).json()["hidden"] is False


def test_patch_college_validation_and_clash(client):
    ids = seed(client, row(), row(name="Beta U"))
    assert client.patch(f"{BASE_URL}/{ids['Beta U']}", json={"state": "ZZ"}).status_code == 422
    assert client.patch(f"{BASE_URL}/{ids['Beta U']}", json={"control": "x"}).status_code == 422
    assert client.patch(f"{BASE_URL}/{ids['Beta U']}", json={"name": "!!!"}).status_code == 422
    assert client.patch(f"{BASE_URL}/{ids['Beta U']}", json={"bogus": 1}).status_code == 422
    clash = client.patch(f"{BASE_URL}/{ids['Beta U']}", json={"name": "alpha college"})
    assert clash.status_code == 409
    assert client.patch(f"{BASE_URL}/{MISSING}", json={"name": "X"}).status_code == 404


def test_record_edit_validates_with_import_rules(client):
    cid = seed(client, row())["Alpha College"]
    rid = records_of(client, cid)[0]["id"]
    bad = client.patch(f"{BASE_URL}/records/{rid}", json={"pct_need_met": 1.5})
    assert bad.status_code == 422 and bad.json()["detail"][0]["loc"] == ["body", "pct_need_met"]
    assert client.patch(f"{BASE_URL}/records/{rid}", json={"cds_year": "2025-27"}).status_code == 422
    assert client.patch(f"{BASE_URL}/records/{rid}", json={"release_label": None}).status_code == 422
    assert client.patch(f"{BASE_URL}/records/{rid}", json={"coa_year": None}).status_code == 422  # COA still present


def test_record_edit_sets_manual_flags_and_null_clears(client):
    cid = seed(client, row())["Alpha College"]
    rid = records_of(client, cid)[0]["id"]
    resp = client.patch(f"{BASE_URL}/records/{rid}", json={"pct_need_met": "0.5", "avg_merit_award": None})
    body = resp.json()
    assert resp.status_code == 200
    assert float(body["pct_need_met"]) == 0.5 and body["avg_merit_award"] is None
    assert body["manually_edited_at"] and body["manually_edited_by"] == str(ADMIN_ID)
    assert float(body["coa_out_of_state"]) == 60000  # untouched fields stay


def test_empty_patch_does_not_mark_manual(client):
    rid = records_of(client, seed(client, row())["Alpha College"])[0]["id"]
    body = client.patch(f"{BASE_URL}/records/{rid}", json={}).json()
    assert body["manually_edited_at"] is None


def test_cds_year_clash_is_409_and_move_works(client):
    cid = seed(client, row(cds_year="2024-25"), row(cds_year="2025-26"))["Alpha College"]
    old, _new = (r["id"] for r in reversed(records_of(client, cid)))
    assert client.patch(f"{BASE_URL}/records/{old}", json={"cds_year": "2025-26"}).status_code == 409
    assert client.patch(f"{BASE_URL}/records/{old}", json={"cds_year": "2023-24"}).json()["cds_year"] == "2023-24"
    assert client.patch(f"{BASE_URL}/records/{uuid.uuid4()}", json={"source": "x"}).status_code == 404


def test_add_record_by_hand(client):
    cid = seed(client, row())["Alpha College"]
    resp = client.post(f"{BASE_URL}/{cid}/records", json={"cds_year": "2024-25", "pct_need_met": 0.9, "coa_out_of_state": 1000, "coa_year": "2024-25"})
    body = resp.json()
    assert resp.status_code == 201
    assert body["release_label"] == datetime.now(UTC).strftime("%Y-%m")
    assert body["manually_edited_by"] == str(ADMIN_ID) and body["coa_estimated"] is False
    assert client.post(f"{BASE_URL}/{cid}/records", json={"cds_year": "2024-25"}).status_code == 409
    assert client.post(f"{BASE_URL}/{cid}/records", json={"cds_year": "2023-24", "coa_in_state": 5}).status_code == 422
    assert client.post(f"{BASE_URL}/{MISSING}/records", json={}).status_code == 404


def test_later_import_warns_about_manual_edit(client):
    cid = seed(client, row())["Alpha College"]
    rid = records_of(client, cid)[0]["id"]
    client.patch(f"{BASE_URL}/records/{rid}", json={"pct_need_met": "0.5"})
    data = make_csv([row(release_label="2026-10")])
    body = commit(client, data).json()
    assert any(w["message"].startswith("overwrites manual edit: pct_need_met") for w in body["warnings"])


def test_delete_latest_record_makes_previous_year_latest(client):
    cid = seed(client, row(cds_year="2024-25"), row(cds_year="2025-26"))["Alpha College"]
    latest = records_of(client, cid)[0]["id"]
    assert client.delete(f"{BASE_URL}/records/{latest}").status_code == 204
    assert client.get(f"{BASE_URL}/{cid}").json()["latest_cds_year"] == "2024-25"
    assert client.delete(f"{BASE_URL}/records/{latest}").status_code == 404


def test_delete_college_removes_records(client, db):
    cid = seed(client, row(), row(name="Beta U"))["Alpha College"]
    assert client.delete(f"{BASE_URL}/{cid}").status_code == 204
    assert client.get(f"{BASE_URL}/{cid}").status_code == 404
    db.expire_all()
    assert len(db.scalars(select(College)).all()) == 1
    assert len(db.scalars(select(CollegeAidRecord)).all()) == 1
