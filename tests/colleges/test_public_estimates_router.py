"""POST /colleges/public/estimates: auth, validation, payload."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.app_config.models import AppConfig
from src.auth.schemas import CurrentUser
from src.colleges import public_router
from src.colleges.access_password import hash_password
from src.colleges.access_token import TOKEN_HEADER, issue_access_token
from src.colleges.models import College, CollegeAidRecord
from src.db.deps import get_db
from src.schools.src_session import optional_current_user

URL = "/api/v1/colleges/public/estimates"


@pytest.fixture
def app(db):
    api = FastAPI()
    api.include_router(public_router.router)
    api.dependency_overrides[get_db] = lambda: db
    api.dependency_overrides[optional_current_user] = lambda: None
    return api


@pytest.fixture
def client(app):
    return TestClient(app)


@pytest.fixture
def admin(app):
    app.dependency_overrides[optional_current_user] = lambda: CurrentUser(user_id=uuid.uuid4(), role="super_admin")


@pytest.fixture
def seeded(db):
    priv = College(name="Priv", name_key="priv", state="NY", region="Mid-Atlantic", control="private")
    pub = College(name="Pub", name_key="pub", state="FL", region="Southeast", control="public")
    hidden = College(name="Hidden", name_key="hidden", state="NY", region="Mid-Atlantic", control="private",
                     hidden_at=datetime.now(UTC))
    db.add_all([priv, pub, hidden])
    db.flush()
    db.add_all([
        CollegeAidRecord(college_id=priv.id, cds_year="2025-26", release_label="2026-10", coa_out_of_state=Decimal(80000),
                         coa_year="2025-26", pct_need_met=Decimal("0.8"), avg_merit_award=Decimal(10000)),
        CollegeAidRecord(college_id=pub.id, cds_year="2025-26", release_label="2026-10", coa_in_state=Decimal(20000),
                         coa_out_of_state=Decimal(40000), pct_need_met=Decimal("0.9")),
        CollegeAidRecord(college_id=hidden.id, cds_year="2025-26", release_label="2026-10", coa_out_of_state=Decimal(1)),
    ])
    db.commit()
    return {"priv": priv, "pub": pub}


def test_no_token_401(client, db):
    db.add(AppConfig(college_aid_password_hash=hash_password("fake-pass-1234")))
    db.commit()
    resp = client.post(URL, json={"sai": 0})
    assert (resp.status_code, resp.json()["detail"]) == (401, "college_aid_token_required")


def test_bad_token_401(client, db):
    resp = client.post(URL, json={"sai": 0}, headers={TOKEN_HEADER: "garbage"})
    assert resp.status_code == 401


def test_valid_token_200(client, db):
    stored = hash_password("fake-pass-1234")
    db.add(AppConfig(college_aid_password_hash=stored))
    db.commit()
    token, _ = issue_access_token(stored)
    assert client.post(URL, json={"sai": 0}, headers={TOKEN_HEADER: token}).status_code == 200


@pytest.mark.parametrize("params", [
    {},  # sai required
    {"sai": -1501}, {"sai": 1000000}, {"sai": "abc"},
    {"sai": 0, "home_state": "N"}, {"sai": 0, "home_state": "NYC"}, {"sai": 0, "home_state": "1A"},
    {"sai": 0, "merit": "-5"}, {"sai": 0, "merit": "1000000"}, {"sai": 0, "merit": "lots"}, {"sai": 0, "merit": "1.5"},
])
def test_invalid_params_422(client, admin, params):
    assert client.post(URL, json=params).status_code == 422


@pytest.mark.parametrize("params", [{"sai": -1500}, {"sai": 999999, "merit": "999999"}, {"sai": 0, "merit": "0"}])
def test_boundary_params_ok(client, admin, seeded, params):
    assert client.post(URL, json=params).status_code == 200


def test_payload_shape_numbers_and_hidden_excluded(client, admin, seeded):
    resp = client.post(URL, json={"sai": 20000, "home_state": "fl", "merit": "none"})
    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "private, no-store"
    body = resp.json()
    assert body["inputs"] == {"sai": 20000, "home_state": "FL", "merit_mode": "none", "merit_amount": None}
    items = {i["college_id"]: i for i in body["items"]}
    assert set(items) == {str(seeded["priv"].id), str(seeded["pub"].id)}  # hidden excluded
    priv = items[str(seeded["priv"].id)]
    assert set(priv) == {
        "college_id", "coa_used", "coa_basis", "coa_year", "need", "need_package", "merit_used", "net_low",
        "net_high", "self_help_low", "self_help_high", "family_low", "family_high", "missing",
    }
    assert priv["coa_used"] == 80000 and isinstance(priv["coa_used"], (int, float))
    assert priv["coa_basis"] == "out_of_state" and priv["coa_year"] == "2025-26"
    assert priv["need"] == 60000 and priv["need_package"] == 48000 and priv["merit_used"] == 0
    assert isinstance(priv["net_low"], (int, float)) and priv["net_low"] <= priv["net_high"]
    assert priv["missing"] == []
    pub = items[str(seeded["pub"].id)]
    assert (pub["coa_basis"], pub["coa_used"]) == ("in_state", 20000)  # home FL, public in FL


@pytest.mark.parametrize("params", [
    {"sai": 0, "home_state": ""}, {"sai": 0, "home_state": "XYZ"}, {"sai": 0, "merit": ""},
    {"sai": 0, "merit": "5000\n"}, {"sai": 1.5}, {"sai": 0, "merit": 5000}, {"sai": 0, "extra": 1},
])
def test_more_invalid_params_422(client, admin, params):
    assert client.post(URL, json=params).status_code == 422


MONEY_FIELDS = ("coa_used", "need", "need_package", "merit_used", "net_low", "net_high",
                "self_help_low", "self_help_high", "family_low", "family_high")


def test_money_fields_are_json_numbers_not_strings(client, admin, seeded):
    body = client.post(URL, json={"sai": 0, "merit": "5000"}).json()
    assert body["inputs"]["merit_amount"] == 5000 and not isinstance(body["inputs"]["merit_amount"], str)
    for item in body["items"]:
        for field in MONEY_FIELDS:
            value = item[field]
            assert value is None or (isinstance(value, (int, float)) and not isinstance(value, bool)), field


def test_merit_above_coa_returns_zero_not_negative(client, admin, seeded):
    body = client.post(URL, json={"sai": 0, "merit": "999999"}).json()
    assert body["items"]
    for item in body["items"]:
        for field in ("net_low", "net_high", "family_low", "family_high"):
            assert item[field] == 0, field


def test_missing_pct_need_met_with_need_reports_missing(client, admin, db):
    college = College(name="No Pct", name_key="no-pct", state="NY", region="Mid-Atlantic", control="private")
    db.add(college)
    db.flush()
    db.add(CollegeAidRecord(college_id=college.id, cds_year="2025-26", release_label="2026-10",
                            coa_out_of_state=Decimal(60000), coa_year="2025-26", avg_merit_award=Decimal(5000)))
    db.commit()
    body = client.post(URL, json={"sai": 0, "merit": "none"}).json()
    item = next(i for i in body["items"] if i["college_id"] == str(college.id))
    assert item["missing"] == ["pct_need_met"]
    assert item["coa_used"] == 60000 and item["need"] == 60000
    assert item["net_low"] is None and item["family_high"] is None


def test_merit_amount_and_missing_data(client, admin, seeded):
    body = client.post(URL, json={"sai": 0, "merit": "5000"}).json()
    assert body["inputs"]["merit_mode"] == "amount" and body["inputs"]["merit_amount"] == 5000
    pub = next(i for i in body["items"] if i["college_id"] == str(seeded["pub"].id))
    assert pub["merit_used"] == 5000 and pub["coa_basis"] == "out_of_state"  # no home state
    avg = client.post(URL, json={"sai": 0}).json()
    pub_avg = next(i for i in avg["items"] if i["college_id"] == str(seeded["pub"].id))
    assert pub_avg["net_low"] is None and pub_avg["merit_used"] is None
    assert pub_avg["missing"] == ["avg_merit_award"]
    assert avg["inputs"]["merit_mode"] == "avg"
