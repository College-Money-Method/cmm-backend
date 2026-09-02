"""A hard delete must not silently destroy a real session's history.

`DELETE /webinars/{id}` cascades: every `workshop_registrations` row (families
and their attendance) and every school `portal_mapping` goes with the webinar,
and there is no soft-cancel state to fall back on. It exists for a webinar
created by mistake, so it stays open for an empty one and needs `force=true`
once the session has registrations or has already generated email.

The counts the endpoints report are what the admin confirmation prompt reads,
so they are covered here too — a prompt with the wrong number is worse than no
prompt.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from src.auth.deps import get_current_user
from src.auth.schemas import CurrentUser
from src.db.client import get_supabase
from src.db.deps import get_db
from src.emails.models import EmailSendLog
from src.main import app
from src.schools.models import School
from src.workshops.models import PortalMapping, Webinar, Workshop, WorkshopRegistration

ADMIN_USER_ID = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
WORKSHOP_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
EMPTY_WEBINAR_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")
BUSY_WEBINAR_ID = uuid.UUID("33333333-3333-3333-3333-333333333333")
SCHOOL_ID = uuid.UUID("44444444-4444-4444-4444-444444444444")

START = datetime(2026, 4, 3, 18, 0, tzinfo=timezone.utc)


@pytest.fixture
def client(webinar_sessionmaker):
    """Admin client over two webinars: one untouched, one with a registration,
    a mapped school and a sent email."""
    seed = webinar_sessionmaker()
    seed.add(Workshop(id=WORKSHOP_ID, name="FAFSA Basics"))
    seed.add(School(id=SCHOOL_ID, name="Annie Wright Schools", slug="annie-wright-schools"))
    seed.flush()
    for webinar_id, name in ((EMPTY_WEBINAR_ID, "Created by mistake"), (BUSY_WEBINAR_ID, "Real session")):
        seed.add(
            Webinar(
                id=webinar_id,
                workshop_id=WORKSHOP_ID,
                webinar_name=name,
                start_datetime=START,
                end_datetime=START + timedelta(hours=1),
            )
        )
    seed.flush()
    seed.add(
        WorkshopRegistration(
            webinar_id=BUSY_WEBINAR_ID, school_id=SCHOOL_ID, email="family@example.com", attended=True
        )
    )
    seed.add(PortalMapping(school_id=SCHOOL_ID, webinar_id=BUSY_WEBINAR_ID))
    seed.add(
        EmailSendLog(
            recipient_email="counselor@example.com",
            subject="Your workshop is coming up",
            status="sent",
            source="pre_workshop",
            webinar_id=BUSY_WEBINAR_ID,
            school_id=SCHOOL_ID,
        )
    )
    seed.commit()
    seed.close()

    def override_get_db():
        db = webinar_sessionmaker()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_supabase] = lambda: None
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(
        user_id=ADMIN_USER_ID, email="admin@collegemoneymethod.com", role="super_admin"
    )
    yield TestClient(app)
    app.dependency_overrides.clear()


def _webinar_exists(sessionmaker_, webinar_id: uuid.UUID) -> bool:
    db = sessionmaker_()
    try:
        return db.get(Webinar, webinar_id) is not None
    finally:
        db.close()


class TestDeleteGuard:
    def test_empty_webinar_deletes(self, client, webinar_sessionmaker):
        """The reported need: remove a webinar created by mistake."""
        resp = client.delete(f"/api/v1/workshops/webinars/{EMPTY_WEBINAR_ID}")
        assert resp.status_code == 204
        assert not _webinar_exists(webinar_sessionmaker, EMPTY_WEBINAR_ID)

    def test_webinar_with_history_is_refused(self, client, webinar_sessionmaker):
        resp = client.delete(f"/api/v1/workshops/webinars/{BUSY_WEBINAR_ID}")
        assert resp.status_code == 409
        detail = resp.json()["detail"]
        assert "1 registration(s)" in detail
        assert "1 email(s)" in detail
        assert _webinar_exists(webinar_sessionmaker, BUSY_WEBINAR_ID), "refusal must not delete"

    def test_force_deletes_a_webinar_with_history(self, client, webinar_sessionmaker):
        """The admin has seen the counts and chosen to proceed.

        Also pins the cascade: SQLAlchemy must let the DB remove the children
        rather than trying to NULL their (NOT NULL) `webinar_id`, which is what
        made this endpoint fail outright for any webinar with history.
        """
        resp = client.delete(f"/api/v1/workshops/webinars/{BUSY_WEBINAR_ID}?force=true")
        assert resp.status_code == 204
        assert not _webinar_exists(webinar_sessionmaker, BUSY_WEBINAR_ID)

        db = webinar_sessionmaker()
        try:
            assert db.query(WorkshopRegistration).count() == 0, "registrations cascade with the webinar"
            assert db.query(PortalMapping).count() == 0, "school mappings cascade with the webinar"
            # ON DELETE SET NULL: the audit row survives, unlinked.
            log = db.query(EmailSendLog).one()
            assert log.webinar_id is None
        finally:
            db.close()

    def test_missing_webinar_is_404_not_409(self, client):
        resp = client.delete(f"/api/v1/workshops/webinars/{uuid.uuid4()}")
        assert resp.status_code == 404


class TestDeleteImpactCounts:
    """The numbers the confirmation prompt shows, on every surface that offers
    the button: sessions list, workshop sub-table, webinar detail."""

    def _by_id(self, rows: list[dict]) -> dict[str, dict]:
        return {r["id"]: r for r in rows}

    def test_sessions_list_reports_counts(self, client):
        rows = self._by_id(client.get("/api/v1/workshops/webinars").json())
        busy = rows[str(BUSY_WEBINAR_ID)]
        assert (busy["registration_count"], busy["school_count"], busy["email_send_count"]) == (1, 1, 1)
        empty = rows[str(EMPTY_WEBINAR_ID)]
        assert (empty["registration_count"], empty["school_count"], empty["email_send_count"]) == (0, 0, 0)

    def test_workshop_subtable_reports_counts(self, client):
        rows = self._by_id(client.get(f"/api/v1/workshops/{WORKSHOP_ID}/webinars").json())
        busy = rows[str(BUSY_WEBINAR_ID)]
        assert (busy["registration_count"], busy["school_count"], busy["email_send_count"]) == (1, 1, 1)

    def test_webinar_detail_reports_counts(self, client):
        body = client.get(f"/api/v1/workshops/webinars/{BUSY_WEBINAR_ID}").json()
        assert (body["registration_count"], body["school_count"], body["email_send_count"]) == (1, 1, 1)

    def test_counts_do_not_leak_between_webinars(self, client):
        body = client.get(f"/api/v1/workshops/webinars/{EMPTY_WEBINAR_ID}").json()
        assert (body["registration_count"], body["school_count"], body["email_send_count"]) == (0, 0, 0)
