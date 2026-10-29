"""POST /colleges/access and GET /colleges/public, including auth paths."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import jwt
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
from src.schools.src_session import optional_current_user, signing_secret

PASSWORD = "fake-pass-1234"
BASE = "/api/v1/colleges"


@pytest.fixture(autouse=True)
def _no_delay(monkeypatch):
    monkeypatch.setattr(public_router, "BAD_PASSWORD_DELAY_SECONDS", 0)


@pytest.fixture
def stored(db) -> str:
    value = hash_password(PASSWORD)
    db.add(AppConfig(college_aid_password_hash=value))
    db.commit()
    return value


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


def _admin(app, role="super_admin"):
    app.dependency_overrides[optional_current_user] = lambda: CurrentUser(user_id=uuid.uuid4(), role=role)


def _seed(db):
    c = College(name="Adelphi", name_key="adelphi", state="NY", region="Mid-Atlantic", control="private")
    nr = College(name="JTS", name_key="jts", state="NY", region="Mid-Atlantic", control="private")
    hidden = College(
        name="Hidden", name_key="hidden", state="NY", region="Mid-Atlantic", control="private",
        hidden_at=datetime.now(UTC),
    )
    db.add_all([c, nr, hidden])
    db.flush()
    db.add_all([
        CollegeAidRecord(
            college_id=c.id, cds_year="2025-26", release_label="2026-10", coa_out_of_state=Decimal(80000),
            avg_merit_award=Decimal(10000), pct_need_met=Decimal("0.8"),
            pct_non_need_receiving_merit=Decimal("0.3"), aid_application="CSS Profile",
        ),
        CollegeAidRecord(college_id=c.id, cds_year="2024-25", release_label="2025-09"),
        CollegeAidRecord(college_id=nr.id, cds_year=None, release_label="2026-01"),
        CollegeAidRecord(college_id=hidden.id, cds_year="2025-26", release_label="2026-10"),
    ])
    db.commit()


# ── access ───────────────────────────────────────────────────────────────────

def test_right_password_returns_token_and_expiry(client, stored):
    resp = client.post(f"{BASE}/access", json={"password": PASSWORD})
    assert resp.status_code == 200
    body = resp.json()
    assert body["token"]
    assert datetime.fromisoformat(body["expires_at"]) > datetime.now(UTC) + timedelta(days=6)


def test_wrong_password_401(client, stored):
    resp = client.post(f"{BASE}/access", json={"password": "wrong-pass-0000"})
    assert resp.status_code == 401
    assert resp.json()["detail"] == "college_aid_bad_password"


def test_no_password_set_is_403_disabled(client, db):
    resp = client.post(f"{BASE}/access", json={"password": PASSWORD})
    assert resp.status_code == 403
    assert resp.json()["detail"] == "college_aid_disabled"


def test_wrong_password_is_delayed(client, stored, monkeypatch):
    slept = []
    monkeypatch.setattr(public_router.time, "sleep", slept.append)
    monkeypatch.setattr(public_router, "BAD_PASSWORD_DELAY_SECONDS", 1.0)
    client.post(f"{BASE}/access", json={"password": "wrong-pass-0000"})
    assert slept == [1.0]


# ── read auth ────────────────────────────────────────────────────────────────

def test_no_auth_401_required(client, stored):
    resp = client.get(f"{BASE}/public")
    assert (resp.status_code, resp.json()["detail"]) == (401, "college_aid_token_required")


def test_bad_token_401_invalid(client, stored):
    resp = client.get(f"{BASE}/public", headers={TOKEN_HEADER: "garbage"})
    assert (resp.status_code, resp.json()["detail"]) == (401, "college_aid_token_invalid")


def test_token_from_access_works(client, stored):
    token = client.post(f"{BASE}/access", json={"password": PASSWORD}).json()["token"]
    assert client.get(f"{BASE}/public", headers={TOKEN_HEADER: token}).status_code == 200


def test_token_after_rotation_401(client, db, stored):
    token, _ = issue_access_token(stored)
    db.query(AppConfig).update({"college_aid_password_hash": hash_password("rotated-pass-9")})
    db.commit()
    resp = client.get(f"{BASE}/public", headers={TOKEN_HEADER: token})
    assert (resp.status_code, resp.json()["detail"]) == (401, "college_aid_token_invalid")


def test_school_style_token_without_audience_rejected(client, stored):
    forged = jwt.encode({"sub": "college-aid", "exp": datetime.now(UTC) + timedelta(hours=1)},
                        signing_secret(), algorithm="HS256")
    assert client.get(f"{BASE}/public", headers={TOKEN_HEADER: forged}).status_code == 401


def test_admin_bearer_without_token_200_even_when_closed(app, client, db):
    _admin(app)
    assert client.get(f"{BASE}/public").status_code == 200


def test_non_admin_bearer_still_needs_token(app, client, stored):
    _admin(app, role="viewer")
    assert client.get(f"{BASE}/public").status_code == 401


# ── read payload ─────────────────────────────────────────────────────────────

def test_public_payload_shape_and_rules(app, client, db):
    _admin(app)
    _seed(db)
    resp = client.get(f"{BASE}/public")
    assert resp.headers["cache-control"] == "private, no-store"
    body = resp.json()
    assert body["max_cds_year"] == "2025-26"
    assert body["latest_release_label"] == "2026-10"
    assert body["generated_at"]
    items = {i["name"]: i for i in body["items"]}
    assert set(items) == {"Adelphi", "JTS"}  # hidden excluded
    a = items["Adelphi"]
    assert a["cds_year"] == "2025-26"
    assert a["aid_form"] == "css"
    # Money and percents are JSON numbers, not Decimal strings (the UI sorts and does math on them).
    assert a["pct_need_unmet"] == 0.2
    assert a["pct_non_need_paying_full_coa"] == 0.7
    assert a["merit_pct_of_coa_out_of_state"] == 0.125
    assert a["coa_out_of_state"] == 80000
    assert a["merit_pct_of_coa_in_state"] is None
    assert items["JTS"]["cds_year"] is None
    assert items["JTS"]["pct_need_unmet"] is None
