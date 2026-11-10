"""Family chat: audience is fixed by the endpoint, school context needs proof."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.app_config.models import AppConfig
from src.assistant import chat_turn, router
from src.assistant.access_token import TOKEN_HEADER, USER_ID_HEADER, issue_access_token
from src.assistant.family_school_scope import scope_for_school
from src.assistant.models import AssistantConversation
from src.assistant.retrieval_scope import RetrievalScope
from src.auth.schemas import CurrentUser
from src.colleges.access_password import hash_password
from src.config import settings
from src.db.deps import get_db
from src.schools.models import School
from src.schools.src_session import issue_token, optional_current_user
from tests.assistant.conftest import make_chunk
from tests.schools.src_test_support import make_session_factory, uid

BASE = "/api/v1/assistant"
SRC_HEADER = "X-SRC-Session"
SCHOOL_A, SCHOOL_B, PREVIEW = uid(1), uid(2), uid(3)
COHORT = uid(50)


@pytest.fixture(autouse=True)
def _secret(monkeypatch):
    monkeypatch.setattr(settings, "src_session_secret", "unit-test-secret-not-real")
    monkeypatch.setattr(settings, "src_session_enforced", False)


@pytest.fixture
def factory():
    """All real tables (schools included), replacing the chat-only harness."""
    factory = make_session_factory()
    with factory() as db:
        db.add(AppConfig(assistant_password_hash=hash_password("fake-pass-1234")))
        future = datetime.now(timezone.utc) + timedelta(days=10)
        db.add_all([
            School(id=SCHOOL_A, name="A High", slug="a-high", state="TX", is_current_customer=True,
                   cmm_website_password="pw-a"),
            School(id=SCHOOL_B, name="B High", slug="b-high", is_current_customer=True),
            School(id=PREVIEW, name="Preview High", slug="preview-high", is_cmm_website_activated=True,
                   is_src_preview=True, src_preview_expires_at=future, cmm_website_password="pw-p"),
        ])
        db.commit()
    return factory


@pytest.fixture
def seen(monkeypatch, bedrock):
    """Scopes handed to retrieval, and how many queries were embedded."""
    calls = {"scopes": [], "embeds": 0}

    def embed(text):
        calls["embeds"] += 1
        return [0.0] * 1024

    def search(db, text, vector, scope):
        calls["scopes"].append(scope)
        return [make_chunk()]

    monkeypatch.setattr(chat_turn, "embed_query", embed)
    monkeypatch.setattr(chat_turn, "hybrid_search", search)
    return calls


class Harness:
    def __init__(self, factory):
        self.user: CurrentUser | None = None
        self.api = FastAPI()
        self.api.include_router(router.router)

        def override_get_db():
            session = factory()
            try:
                yield session
            finally:
                session.close()

        self.api.dependency_overrides[get_db] = override_get_db
        self.api.dependency_overrides[optional_current_user] = lambda: self.user
        self.client = TestClient(self.api)
        with factory() as db:
            stored = db.query(AppConfig).one().assistant_password_hash
        token, _ = issue_access_token(stored)
        self.headers = {TOKEN_HEADER: token, USER_ID_HEADER: str(uuid.uuid4())}

    def chat(self, school_id=None, src_token=None, profile=None, **extra):
        body = {"message": "Who files?", "profile": profile or {}, "school_id": str(school_id) if school_id else None}
        headers = dict(self.headers)
        if src_token:
            headers[SRC_HEADER] = src_token
        return self.client.post(f"{BASE}/chat", json={**body, **extra}, headers=headers)


@pytest.fixture
def harness(factory, bedrock, seen):
    return Harness(factory)


def _src_token(factory, school_id) -> str:
    with factory() as db:
        return issue_token(db.get(School, school_id))[0]


def _source_urls(resp) -> list[str | None]:
    lines = resp.text.splitlines()
    meta = json.loads(lines[lines.index("event: meta") + 1].removeprefix("data: "))
    return [s["url"] for s in meta["sources"]]


def test_school_chat_links_into_that_school_resource_center(harness, factory):
    resp = harness.chat(school_id=SCHOOL_A, src_token=_src_token(factory, SCHOOL_A))
    assert resp.status_code == 200
    assert _source_urls(resp) == ["/school/a-high/topic/divorce"]


def test_public_chat_keeps_generic_links(harness):
    assert _source_urls(harness.chat()) == ["/topic/divorce"]


def test_old_client_role_cannot_change_audience(harness, seen):
    resp = harness.chat(profile={"role": "counselor", "grade": 11})
    assert resp.status_code == 200
    assert seen["scopes"] == [RetrievalScope.public("family")]


def test_no_school_means_public_family_scope(harness, seen):
    assert harness.chat().status_code == 200
    assert seen["scopes"][0].school_id is None and seen["scopes"][0].audience == "family"


def test_profile_state_never_widens_retrieval(harness, seen):
    assert harness.chat(profile={"state": "tx"}).status_code == 200
    assert seen["scopes"][0].state is None


def test_school_without_credentials_is_forbidden_before_embedding(harness, seen):
    resp = harness.chat(school_id=SCHOOL_A)
    assert resp.status_code == 403 and resp.json()["detail"] == "assistant_school_forbidden"
    assert seen["embeds"] == 0


def test_staff_of_another_school_is_forbidden(harness, seen):
    harness.user = CurrentUser(user_id=uuid.uuid4(), role="hub_user", school_id=SCHOOL_B)
    assert harness.chat(school_id=SCHOOL_A).status_code == 403
    assert seen["embeds"] == 0


def test_staff_of_the_school_gets_its_scope(harness, seen):
    harness.user = CurrentUser(user_id=uuid.uuid4(), role="hub_admin", school_id=SCHOOL_A)
    assert harness.chat(school_id=SCHOOL_A).status_code == 200
    assert seen["scopes"] == [RetrievalScope("family", SCHOOL_A, None, "TX")]


def test_super_admin_gets_school_cohort_and_state(harness, factory, seen):
    with factory() as db:
        db.get(School, SCHOOL_A).cohort_id = COHORT
        db.commit()
    harness.user = CurrentUser(user_id=uuid.uuid4(), role="super_admin")
    assert harness.chat(school_id=SCHOOL_A).status_code == 200
    scope = seen["scopes"][0]
    assert scope == RetrievalScope("family", SCHOOL_A, COHORT, "TX")


def test_unknown_school_is_404(harness):
    harness.user = CurrentUser(user_id=uuid.uuid4(), role="super_admin")
    assert harness.chat(school_id=uid(99)).status_code == 404


def test_valid_src_token_sets_the_school_scope(harness, factory, seen):
    resp = harness.chat(school_id=SCHOOL_A, src_token=_src_token(factory, SCHOOL_A))
    assert resp.status_code == 200
    assert seen["scopes"] == [RetrievalScope("family", SCHOOL_A, None, "TX")]


def test_src_token_for_another_school_is_rejected(harness, factory, seen):
    resp = harness.chat(school_id=SCHOOL_A, src_token=_src_token(factory, SCHOOL_B))
    assert resp.status_code == 403
    assert seen["embeds"] == 0


def test_bad_src_token_is_401(harness, seen):
    resp = harness.chat(school_id=SCHOOL_A, src_token="not-a-token")
    assert resp.status_code == 401 and seen["embeds"] == 0


def test_preview_school_gets_the_public_scope(harness, factory, seen):
    resp = harness.chat(school_id=PREVIEW, src_token=_src_token(factory, PREVIEW))
    assert resp.status_code == 200
    assert seen["scopes"] == [RetrievalScope.public("family")]


def test_scope_for_school_is_pure():
    school = School(id=SCHOOL_A, name="A", state="TX", cohort_id=COHORT)
    assert scope_for_school(school, "counselor", "full") == RetrievalScope("counselor", SCHOOL_A, COHORT, "TX")
    assert scope_for_school(school, "counselor", "preview") == RetrievalScope.public("counselor")


def test_family_never_sees_counselor_conversations(harness, factory):
    owner = uuid.UUID(harness.headers[USER_ID_HEADER])
    with factory() as db:
        row = AssistantConversation(local_user_id=owner, audience="counselor", title="private")
        db.add(row)
        db.commit()
        conversation_id = row.id
    assert harness.client.get(f"{BASE}/conversations", headers=harness.headers).json()["items"] == []
    assert harness.client.get(f"{BASE}/conversations/{conversation_id}", headers=harness.headers).status_code == 404
    assert harness.client.delete(f"{BASE}/conversations/{conversation_id}", headers=harness.headers).status_code == 404
    assert harness.chat(conversation_id=str(conversation_id)).status_code == 404
