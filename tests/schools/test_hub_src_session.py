"""A hub login trades for a Resource Center session on its own school only."""

from __future__ import annotations

import pytest

from src.auth.deps import get_current_user
from src.auth.schemas import CurrentUser
from src.config import settings
from src.main import app
from src.schools.models import School
from src.schools.src_session import _check_token
from tests.schools.src_test_support import install, make_session_factory, uid

OWN, OTHER, PROSPECT = uid(1), uid(2), uid(3)
URL = "/api/v1/schools/me/src-session"


@pytest.fixture(autouse=True)
def _secret(monkeypatch):
    monkeypatch.setattr(settings, "src_session_secret", "unit-test-secret-not-real")


@pytest.fixture
def env():
    factory = make_session_factory()
    with factory() as db:
        db.add_all([
            School(id=OWN, name="Own High", slug="own-high", is_current_customer=True, cmm_website_password="pw1"),
            School(id=OTHER, name="Other High", slug="other-high", is_current_customer=True),
            School(id=PROSPECT, name="Prospect High", slug="prospect-high"),
        ])
        db.commit()
    client = install(factory)
    yield client, factory
    app.dependency_overrides.clear()


def _as(role, school_id=None):
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(user_id=uid(99), role=role, school_id=school_id)


def test_requires_a_hub_login(env):
    client, _ = env
    assert client.post(URL).status_code in (401, 403)


def test_counselor_gets_a_token_for_their_own_school_even_when_it_has_a_password(env):
    client, factory = env
    _as("hub_user", OWN)
    # A school id sent by a counselor is ignored: always their own school.
    body = client.post(URL, params={"school_id": str(OTHER)}).json()
    assert body["slug"] == "own-high" and body["mode"] == "full"
    with factory() as db:
        _check_token(body["session_token"], db.get(School, OWN))


def test_super_admin_names_the_school(env):
    client, _ = env
    _as("super_admin")
    assert client.post(URL).status_code == 422
    assert client.post(URL, params={"school_id": str(OTHER)}).json()["slug"] == "other-high"


def test_school_without_a_resource_center_gets_none(env):
    client, _ = env
    _as("hub_user", PROSPECT)
    assert client.post(URL).status_code == 404
    _as("hub_user", None)
    assert client.post(URL).status_code == 403
