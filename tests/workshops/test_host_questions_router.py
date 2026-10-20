"""Public host-questions read and admin host-link endpoints."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from src.auth.deps import get_current_user
from src.auth.schemas import CurrentUser
from src.db.client import get_supabase
from src.db.deps import get_db
from src.db.enums import RegistrationStatus
from src.main import app
from src.schools.models import School
from src.workshops.models import Webinar, Workshop, WorkshopRegistration

WORKSHOP_ID = uuid.UUID(int=1)
SCHOOL_ID = uuid.UUID(int=2)
LIVE_ID, OLD_ID, NOSTART_ID, PLAIN_ID = (uuid.UUID(int=i) for i in (10, 11, 12, 13))
LIVE_TOKEN, OLD_TOKEN, NOSTART_TOKEN = ("L" * 43, "O" * 43, "N" * 43)
NOW = datetime.now(timezone.utc)
URL = "/api/v1/workshops/public/webinar-hosts/"
INVALID = {"detail": "This link is invalid or has been replaced."}


def _reg(webinar_id, email, status, minutes, **kw):
    return WorkshopRegistration(
        webinar_id=webinar_id,
        school_id=SCHOOL_ID,
        email=email,
        status=status,
        created_at=NOW - timedelta(minutes=minutes),
        **kw,
    )


@pytest.fixture
def sm(webinar_sessionmaker):
    s = webinar_sessionmaker()
    s.add(Workshop(id=WORKSHOP_ID, name="FAFSA Basics"))
    s.add(School(id=SCHOOL_ID, name="Annie Wright", slug="annie-wright"))
    s.flush()
    start = NOW + timedelta(hours=3)
    s.add(Webinar(id=LIVE_ID, workshop_id=WORKSHOP_ID, webinar_name="Live · Annie Wright, East High",
                  host_access_token=LIVE_TOKEN,
                  start_datetime=start, end_datetime=start + timedelta(hours=1)))
    s.add(Webinar(id=OLD_ID, workshop_id=WORKSHOP_ID, host_access_token=OLD_TOKEN,
                  start_datetime=NOW - timedelta(days=20), end_datetime=NOW - timedelta(days=20) + timedelta(hours=1)))
    s.add(Webinar(id=NOSTART_ID, workshop_id=WORKSHOP_ID, host_access_token=NOSTART_TOKEN))
    s.add(Webinar(id=PLAIN_ID, workshop_id=WORKSHOP_ID, start_datetime=start))
    s.flush()
    s.add_all([
        _reg(LIVE_ID, "second@x.org", RegistrationStatus.APPROVED, 1, first_name="Sec", questions=" Q2? ", grade="12"),
        _reg(LIVE_ID, "first@x.org", RegistrationStatus.APPROVED, 5, first_name="Fir", questions="Q1?", grade="11"),
        _reg(LIVE_ID, "quiet@x.org", RegistrationStatus.APPROVED, 2, first_name="Qui", questions="   "),
        _reg(LIVE_ID, "pending@x.org", RegistrationStatus.PENDING, 3, questions="Pending Q?"),
        _reg(LIVE_ID, "denied@x.org", RegistrationStatus.DENIED, 4, questions="Denied Q?"),
    ])
    s.commit()
    s.close()
    return webinar_sessionmaker


def _client(sm, role: str | None):
    def override_get_db():
        db = sm()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_supabase] = lambda: None
    if role:
        app.dependency_overrides[get_current_user] = lambda: CurrentUser(
            user_id=uuid.UUID(int=99), email="a@collegemoneymethod.com", role=role
        )
    return TestClient(app)


@pytest.fixture
def public(sm):
    yield _client(sm, None)
    app.dependency_overrides.clear()


@pytest.fixture
def admin(sm):
    yield _client(sm, "super_admin")
    app.dependency_overrides.clear()


def test_valid_token_returns_only_approved_questions_newest_first(public):
    resp = public.get(URL + LIVE_TOKEN)
    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "no-store"
    assert resp.headers["referrer-policy"] == "no-referrer"
    body = resp.json()
    assert set(body) == {"webinar", "ended", "questions"}
    assert [(q["question"], q["grade"]) for q in body["questions"]] == [("Q2?", "12"), ("Q1?", "11")]
    assert body["webinar"]["workshop_title"] == "FAFSA Basics"
    assert body["ended"] is False


def test_payload_carries_no_registrant_identity_or_session_name(public):
    body = public.get(URL + LIVE_TOKEN).json()
    # Hosts span every school in a cohort: only a registrant's own school may know who registered.
    assert set(body["questions"][0]) == {"question", "grade", "submitted_at"}
    assert set(body["webinar"]) == {"workshop_title", "start_datetime", "end_datetime", "display_timezone"}
    raw = public.get(URL + LIVE_TOKEN).text
    for leak in ("first@x.org", "Fir", "Annie Wright", "East High", "Live"):
        assert leak not in raw


@pytest.mark.parametrize("token", ["U" * 43, "short", "bad chars!" + "x" * 33, "A" * 44])
def test_bad_tokens_share_one_404(public, token):
    resp = public.get(URL + token)
    assert resp.status_code == 404
    assert resp.json() == INVALID
    assert resp.headers["cache-control"] == "no-store"
    assert resp.headers["referrer-policy"] == "no-referrer"


def test_link_never_expires(public):
    old = public.get(URL + OLD_TOKEN)  # session ended 20 days ago
    assert old.status_code == 200
    assert old.json()["ended"] is True
    no_start = public.get(URL + NOSTART_TOKEN)
    assert no_start.status_code == 200
    assert no_start.json()["ended"] is False


def test_malformed_token_does_not_query_db(sm, public):
    queries = []
    from sqlalchemy import event

    engine = sm.kw["bind"]
    event.listen(engine, "before_cursor_execute", lambda *a: queries.append(a[2]))
    assert public.get(URL + "nope").status_code == 404
    assert queries == []


def test_ended_flag(sm, public):
    db = sm()
    w = db.get(Webinar, LIVE_ID)
    w.start_datetime = NOW - timedelta(hours=3)
    w.end_datetime = NOW - timedelta(hours=2)
    db.commit()
    db.close()
    assert public.get(URL + LIVE_TOKEN).json()["ended"] is True


def test_host_link_is_stable_and_rotate_revokes(admin):
    first = admin.post(f"/api/v1/workshops/webinars/{PLAIN_ID}/host-link")
    assert first.status_code == 200
    again = admin.post(f"/api/v1/workshops/webinars/{PLAIN_ID}/host-link").json()
    assert again["url"] == first.json()["url"]
    assert first.json()["url"].rsplit("/workshop-hosts/", 1)[1].__len__() == 43
    old_token = first.json()["url"].rsplit("/", 1)[1]
    assert admin.get(URL + old_token).status_code == 200

    rotated = admin.post(f"/api/v1/workshops/webinars/{PLAIN_ID}/host-link/rotate").json()
    assert rotated["url"] != first.json()["url"]
    assert admin.get(URL + old_token).status_code == 404
    assert admin.get(URL + rotated["url"].rsplit("/", 1)[1]).status_code == 200


def test_unknown_webinar_404(admin):
    assert admin.post(f"/api/v1/workshops/webinars/{uuid.uuid4()}/host-link").status_code == 404


def test_admin_routes_refuse_non_admins(sm):
    for role in (None, "hub_admin"):
        c = _client(sm, role)
        for path in ("host-link", "host-link/rotate"):
            assert c.post(f"/api/v1/workshops/webinars/{PLAIN_ID}/{path}").status_code in (401, 403)
        app.dependency_overrides.clear()


def test_host_link_without_public_origin_is_503_not_relative(admin, monkeypatch):
    from src.workshops import host_access

    monkeypatch.setattr(host_access, "email_origin", lambda: "")
    resp = admin.post(f"/api/v1/workshops/webinars/{PLAIN_ID}/host-link")
    assert resp.status_code == 503
