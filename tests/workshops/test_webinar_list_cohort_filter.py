"""The admin sessions list filters by cohort.

Sessions are scheduled per cohort, and the admin list is long enough that
"show me only Cohort B's sessions" is how an admin finds one. `cohort_id`
must narrow the list without excluding sessions that merely share a workshop.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from src.auth.deps import get_current_user
from src.auth.schemas import CurrentUser
from src.cycles.models import Cohort
from src.db.client import get_supabase
from src.db.deps import get_db
from src.main import app
from src.workshops.models import Webinar, Workshop

ADMIN_USER_ID = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
WORKSHOP_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
COHORT_A_ID = uuid.UUID("55555555-5555-5555-5555-555555555555")
COHORT_B_ID = uuid.UUID("66666666-6666-6666-6666-666666666666")
WEBINAR_A_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")
WEBINAR_B_ID = uuid.UUID("33333333-3333-3333-3333-333333333333")
WEBINAR_NO_COHORT_ID = uuid.UUID("44444444-4444-4444-4444-444444444444")

START = datetime(2026, 4, 3, 18, 0, tzinfo=timezone.utc)


@pytest.fixture
def client(webinar_sessionmaker):
    """One workshop, three sessions: cohort A, cohort B, and no cohort."""
    seed = webinar_sessionmaker()
    seed.add(Workshop(id=WORKSHOP_ID, name="FAFSA Basics"))
    seed.add(Cohort(id=COHORT_A_ID, name="Cohort A"))
    seed.add(Cohort(id=COHORT_B_ID, name="Cohort B"))
    seed.flush()
    for webinar_id, cohort_id, name in (
        (WEBINAR_A_ID, COHORT_A_ID, "Session for A"),
        (WEBINAR_B_ID, COHORT_B_ID, "Session for B"),
        (WEBINAR_NO_COHORT_ID, None, "Unassigned session"),
    ):
        seed.add(
            Webinar(
                id=webinar_id,
                workshop_id=WORKSHOP_ID,
                cohort_id=cohort_id,
                webinar_name=name,
                start_datetime=START,
                end_datetime=START + timedelta(hours=1),
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


def _ids(resp) -> set[str]:
    return {item["id"] for item in resp.json()}


class TestCohortFilter:
    def test_unfiltered_list_returns_every_session(self, client):
        resp = client.get("/api/v1/workshops/webinars")
        assert resp.status_code == 200
        assert _ids(resp) == {str(WEBINAR_A_ID), str(WEBINAR_B_ID), str(WEBINAR_NO_COHORT_ID)}

    def test_cohort_filter_returns_only_that_cohort(self, client):
        resp = client.get(f"/api/v1/workshops/webinars?cohort_id={COHORT_B_ID}")
        assert resp.status_code == 200
        assert _ids(resp) == {str(WEBINAR_B_ID)}
        assert resp.json()[0]["cohort_name"] == "Cohort B"

    def test_unknown_cohort_returns_nothing(self, client):
        resp = client.get(f"/api/v1/workshops/webinars?cohort_id={uuid.uuid4()}")
        assert resp.status_code == 200
        assert resp.json() == []
