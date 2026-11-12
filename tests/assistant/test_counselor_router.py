"""Counselor chat: role gate, school resolution, scope, conversation ownership."""

from __future__ import annotations

import json
import uuid
from dataclasses import replace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.assistant import answer, chat_turn, counselor_router
from src.assistant.models import AssistantConversation, AssistantPromptSection
from src.assistant.retrieval_scope import RetrievalScope
from src.auth.deps import get_current_user
from src.auth.models import UserRole
from src.auth.schemas import CurrentUser
from src.db.deps import get_db
from src.schools.models import Contact, School
from src.video_pipeline import bedrock_usage
from tests.assistant.conftest import FakeBedrock, make_chunk
from tests.assistant.counselor_test_support import make_counselor_factory

BASE = "/api/v1/assistant/counselor"
SCHOOL_A, SCHOOL_B, SCHOOL_OFF = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()


@pytest.fixture
def factory():
    factory = make_counselor_factory()
    with factory() as db:
        db.add_all([
            School(id=SCHOOL_A, name="A High", slug="a-high", state="TX", counselor_assistant_enabled=True),
            School(id=SCHOOL_B, name="B High", slug="b-high", state="CA", counselor_assistant_enabled=True),
            School(id=SCHOOL_OFF, name="Off High", slug="off-high", state="NV"),
        ])
        db.commit()
    return factory


@pytest.fixture
def seen(monkeypatch, factory):
    calls = {"scopes": [], "embeds": 0, "prompts": []}
    fake = FakeBedrock()
    monkeypatch.setattr(answer, "get_client", lambda: fake)
    monkeypatch.setattr(answer, "get_session_factory", lambda: factory)
    monkeypatch.setattr(bedrock_usage, "record", lambda *a, **k: None)

    def embed(text):
        calls["embeds"] += 1
        return [0.0] * 1024

    def search(db, text, vector, scope):
        calls["scopes"].append(scope)
        return [make_chunk()]

    monkeypatch.setattr(chat_turn, "embed_query", embed)
    monkeypatch.setattr(chat_turn, "hybrid_search", search)
    calls["bedrock"] = fake
    return calls


class Harness:
    def __init__(self, factory):
        self.user: CurrentUser | None = None
        self.api = FastAPI()
        self.api.include_router(counselor_router.router)

        def override_get_db():
            session = factory()
            try:
                yield session
            finally:
                session.close()

        self.api.dependency_overrides[get_db] = override_get_db
        self.api.dependency_overrides[get_current_user] = lambda: self.user
        self.client = TestClient(self.api)

    def as_user(self, role, school_id=None, user_id=None) -> CurrentUser:
        self.user = CurrentUser(user_id=user_id or uuid.uuid4(), role=role, school_id=school_id)
        return self.user

    def chat(self, **body):
        return self.client.post(f"{BASE}/chat", json={"message": "Draft a workshop email", **body})


@pytest.fixture
def harness(factory, seen):
    return Harness(factory)


def test_hub_user_body_school_is_ignored(harness, seen):
    harness.as_user("hub_user", SCHOOL_A)
    assert harness.chat(school_id=str(SCHOOL_B)).status_code == 200
    scope = seen["scopes"][0]
    assert scope == RetrievalScope("counselor", SCHOOL_A, None, "TX")


def test_hub_admin_uses_own_school(harness, seen):
    harness.as_user("hub_admin", SCHOOL_B)
    assert harness.chat().status_code == 200
    assert seen["scopes"][0].school_id == SCHOOL_B


def test_super_admin_picks_school(harness, seen):
    harness.as_user("super_admin")
    assert harness.chat(school_id=str(SCHOOL_B)).status_code == 200
    assert seen["scopes"][0] == RetrievalScope("counselor", SCHOOL_B, None, "CA")


def test_workshop_section_and_school_reach_the_prompt(harness, seen):
    harness.as_user("hub_user", SCHOOL_A)
    assert harness.chat().status_code == 200
    system = seen["bedrock"].calls[0]["system"]
    system = system if isinstance(system, str) else str(system)
    assert "A High" in system and "Your school's workshops" in system


def _system(seen) -> str:
    system = seen["bedrock"].calls[0]["system"]
    return system if isinstance(system, str) else str(system)


def test_counselor_name_and_school_fill_the_templates(harness, seen, factory, monkeypatch):
    template = replace(make_chunk("Workshop invite"), source_type="template", text="From {{counselor_name}} at {{school_name}}")
    monkeypatch.setattr(chat_turn, "hybrid_search", lambda *a: [template])
    user_id = uuid.uuid4()
    with factory() as db:
        db.add(Contact(user_id=user_id, school_id=SCHOOL_A, first_name="Jane", last_name="Doe"))
        db.commit()
    harness.as_user("hub_user", SCHOOL_A, user_id=user_id)
    assert harness.chat().status_code == 200
    system = _system(seen)
    assert "Who is asking: Jane Doe, a high school counselor at A High (TX)." in system
    assert "- {{counselor_first_name}}: Jane" in system
    assert "- {{school_name}}: A High" in system
    assert "{{resource_center_password}}:" not in system
    assert "From Jane Doe at A High" in system


def test_super_admin_without_contact_drafts_as_the_school_counselor(harness, seen, factory):
    director = uuid.uuid4()
    with factory() as db:
        db.add(Contact(user_id=director, school_id=SCHOOL_B, first_name="Dana", last_name="Lee"))
        db.add(UserRole(user_id=director, role="hub_admin", school_id=SCHOOL_B))
        db.commit()
    harness.as_user("super_admin")
    assert harness.chat(school_id=str(SCHOOL_B)).status_code == 200
    system = _system(seen)
    assert "- {{counselor_name}}: Dana Lee" in system
    # The fallback fills drafts only; it does not say who is asking.
    assert "Who is asking: a high school counselor at B High (CA)." in system


def _source_urls(resp) -> list[str | None]:
    lines = resp.text.splitlines()
    meta = json.loads(lines[lines.index("event: meta") + 1].removeprefix("data: "))
    return [s["url"] for s in meta["sources"]]


def test_citations_link_into_the_school_resource_center(harness):
    harness.as_user("hub_user", SCHOOL_A)
    assert _source_urls(harness.chat()) == ["/school/a-high/topic/divorce"]


@pytest.mark.parametrize("role,school,status", [
    ("viewer", SCHOOL_A, 403), ("super_admin", None, 422), ("hub_user", None, 403),
])
def test_refused_before_embedding(harness, seen, role, school, status):
    harness.as_user(role, school)
    resp = harness.chat()
    assert resp.status_code == status
    assert seen["embeds"] == 0


@pytest.mark.parametrize("role", ["hub_user", "hub_admin"])
def test_school_without_the_assistant_is_refused(harness, seen, role):
    harness.as_user(role, SCHOOL_OFF)
    resp = harness.chat()
    assert resp.status_code == 403
    assert resp.json()["detail"] == "counselor_assistant_disabled"
    assert seen["embeds"] == 0


def test_super_admin_can_test_a_school_without_the_assistant(harness, seen):
    harness.as_user("super_admin")
    assert harness.chat(school_id=str(SCHOOL_OFF)).status_code == 200


def test_counselor_prompt_sections_reach_the_prompt(harness, seen, factory):
    with factory() as db:
        db.add_all([
            AssistantPromptSection(title="Greeting", body="Say hi to the counselor.", audience="counselor"),
            AssistantPromptSection(title="Family only", body="Talk to parents.", audience="family"),
        ])
        db.commit()
    harness.as_user("hub_user", SCHOOL_A)
    assert harness.chat().status_code == 200
    system = str(seen["bedrock"].calls[0]["system"])
    assert "### Greeting\nSay hi to the counselor." in system
    assert "Talk to parents." not in system


def test_unknown_school_for_super_admin_is_404(harness, seen):
    harness.as_user("super_admin")
    assert harness.chat(school_id=str(uuid.uuid4())).status_code == 404
    assert seen["embeds"] == 0


def test_conversation_ownership_and_audience(harness, factory):
    owner = harness.as_user("hub_user", SCHOOL_A)
    family_id, mine, theirs = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    with factory() as db:
        db.add_all([
            AssistantConversation(id=family_id, local_user_id=owner.user_id, audience="family", title="fam"),
            AssistantConversation(id=mine, local_user_id=owner.user_id, audience="counselor", title="mine"),
            AssistantConversation(id=theirs, local_user_id=uuid.uuid4(), audience="counselor", title="theirs"),
        ])
        db.commit()
    assert harness.client.get(f"{BASE}/conversations/{mine}").status_code == 200
    assert harness.client.get(f"{BASE}/conversations/{family_id}").status_code == 404
    assert harness.client.get(f"{BASE}/conversations/{theirs}").status_code == 404
    assert harness.client.delete(f"{BASE}/conversations/{theirs}").status_code == 404
    assert [c["title"] for c in harness.client.get(f"{BASE}/conversations").json()["items"]] == ["mine"]
    assert harness.chat(conversation_id=str(family_id)).status_code == 404
    assert harness.client.delete(f"{BASE}/conversations/{mine}").status_code == 204


def test_viewer_cannot_list_conversations(harness):
    harness.as_user("viewer", SCHOOL_A)
    assert harness.client.get(f"{BASE}/conversations").status_code == 403
