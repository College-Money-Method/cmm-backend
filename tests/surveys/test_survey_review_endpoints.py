"""Admin triage endpoints: review / undo, bulk, needs-review count and list filter."""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from src.auth.deps import get_current_user
from src.auth.schemas import CurrentUser
from src.db.deps import get_db
from src.main import app
from tests.surveys.conftest import add_response

ADMIN = CurrentUser(user_id=uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"), role="super_admin", email="ops@example.com")
COUNSELOR = CurrentUser(user_id=uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"), role="hub_user")


@pytest.fixture
def client(survey_sessionmaker):
    caller = {"user": ADMIN}

    def override_get_db():
        db = survey_sessionmaker()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = lambda: caller["user"]
    c = TestClient(app)
    c.session = survey_sessionmaker()
    c.caller = caller
    yield c
    app.dependency_overrides.clear()


def test_review_then_undo(client):
    row = add_response(client.session, rating_thumbs=False)

    resp = client.patch(f"/api/v1/surveys/{row.id}/review", json={"reviewed": True})
    assert resp.status_code == 200
    body = resp.json()
    assert body["reviewed_by"] == str(ADMIN.user_id)
    assert body["reviewed_by_email"] == "ops@example.com"
    assert body["reviewed_at"] is not None
    assert body["needs_review"] is False

    resp = client.patch(f"/api/v1/surveys/{row.id}/review", json={"reviewed": False})
    body = resp.json()
    assert body["reviewed_at"] is None and body["reviewed_by_email"] is None
    assert body["needs_review"] is True


def test_second_reviewer_does_not_overwrite_first(client):
    row = add_response(client.session, rating_thumbs=False)
    client.patch(f"/api/v1/surveys/{row.id}/review", json={"reviewed": True})

    client.caller["user"] = ADMIN.model_copy(update={"user_id": uuid.uuid4(), "email": "late@example.com"})
    body = client.patch(f"/api/v1/surveys/{row.id}/review", json={"reviewed": True}).json()
    assert body["reviewed_by_email"] == "ops@example.com"
    resp = client.post("/api/v1/surveys/review-bulk", json={"ids": [str(row.id)], "reviewed": True})
    assert resp.json() == {"updated": 0}


def test_review_unknown_id_is_404(client):
    resp = client.patch(f"/api/v1/surveys/{uuid.uuid4()}/review", json={"reviewed": True})
    assert resp.status_code == 404


def test_non_admin_is_forbidden(client):
    row = add_response(client.session, rating_thumbs=False)
    client.caller["user"] = COUNSELOR
    assert client.patch(f"/api/v1/surveys/{row.id}/review", json={"reviewed": True}).status_code == 403
    assert client.get("/api/v1/surveys/needs-review-count").status_code == 403
    assert client.post("/api/v1/surveys/review-bulk", json={"ids": [str(row.id)], "reviewed": True}).status_code == 403


def test_count_only_includes_actionable_unreviewed(client):
    add_response(client.session, rating_thumbs=True)                       # positive — never counted
    add_response(client.session, rating_thumbs=False)                      # counted
    add_response(client.session, rating_thumbs=True, comment="More please")  # counted
    reviewed = add_response(client.session, rating_thumbs=False)
    client.patch(f"/api/v1/surveys/{reviewed.id}/review", json={"reviewed": True})

    assert client.get("/api/v1/surveys/needs-review-count").json() == {"count": 2}


def test_bulk_review_and_status_filter(client):
    a = add_response(client.session, rating_thumbs=False)
    b = add_response(client.session, rating_stars=1, rating_thumbs=None, question_type="stars")
    add_response(client.session, rating_thumbs=False)  # left for triage

    resp = client.post("/api/v1/surveys/review-bulk", json={"ids": [str(a.id), str(b.id), str(uuid.uuid4())], "reviewed": True})
    assert resp.json() == {"updated": 2}

    needs = client.get("/api/v1/surveys", params={"status": "needs_review"}).json()
    reviewed = client.get("/api/v1/surveys", params={"status": "reviewed"}).json()
    assert needs["total"] == 1
    assert {i["id"] for i in reviewed["items"]} == {str(a.id), str(b.id)}


def test_bulk_rejects_empty_and_oversized(client):
    assert client.post("/api/v1/surveys/review-bulk", json={"ids": [], "reviewed": True}).status_code == 422
    too_many = [str(uuid.uuid4()) for _ in range(201)]
    assert client.post("/api/v1/surveys/review-bulk", json={"ids": too_many, "reviewed": True}).status_code == 422
