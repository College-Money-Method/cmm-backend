"""Admin conversation viewer: list filters, transcript, summary, topic counts and backfill."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.assistant import admin_conversations_router, conversation_summary, question_topics
from src.assistant.models import AssistantConversation, AssistantMessage, AssistantMessageFeedback
from src.auth.deps import get_current_user
from src.auth.models import Profile
from src.auth.schemas import CurrentUser
from src.db.deps import get_db
from src.schools.models import School
from src.video_pipeline.bedrock_client import BedrockCallError
from tests.assistant.counselor_test_support import make_counselor_factory

ADMIN = "/api/v1/admin/assistant"
SCHOOL_A, SCHOOL_B, OWNER = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
START = datetime(2026, 11, 1, tzinfo=UTC)


@pytest.fixture
def factory():
    return make_counselor_factory()


def _chat(db, *, school, title, turns, audience="counselor", deleted=False, at=0):
    """A conversation of (question, topic, answer, rating, flag) turns; returns its answer ids."""
    conv = AssistantConversation(
        local_user_id=OWNER, audience=audience, title=title, school_id=school,
        deleted_at=START if deleted else None, updated_at=START + timedelta(hours=at),
    )
    db.add(conv)
    db.flush()
    answers, t = [], START + timedelta(hours=at)
    for question, topic, answer, rating, flag in turns:
        db.add(AssistantMessage(conversation_id=conv.id, role="user", content=question, topic_tag=topic, created_at=t))
        reply = AssistantMessage(conversation_id=conv.id, role="assistant", content=answer,
                                 created_at=t + timedelta(seconds=1))
        db.add(reply)
        db.flush()
        if rating or flag:
            db.add(AssistantMessageFeedback(message_id=reply.id, local_user_id=OWNER, rating=rating, flag=flag,
                                            comment="Old deadline" if flag else None))
        answers.append(reply.id)
        t += timedelta(seconds=10)
    return conv.id


@pytest.fixture
def chats(factory):
    with factory() as db:
        db.add_all([School(id=SCHOOL_A, name="A High", slug="a-high", state="TX"),
                    School(id=SCHOOL_B, name="B High", slug="b-high", state="CA"),
                    Profile(user_id=OWNER, email="counselor@a-high.example")])
        db.commit()
        ids = {
            "fafsa": _chat(db, school=SCHOOL_A, title="FAFSA", at=3, turns=[
                ("When does FAFSA open?", "fafsa_filing", "October 1.", 1, None),
                ("And the CSS?", "css_profile", "Also October.", -1, "wrong_or_outdated"),
            ]),
            "email": _chat(db, school=SCHOOL_A, title="Email", at=2, deleted=True, turns=[
                ("Draft an email", "family_outreach_drafts", "Dear families…", None, None),
                ("hi", "retired_slug", "Hello!", None, None),
            ]),
            "b": _chat(db, school=SCHOOL_B, title="B chat", at=1, turns=[
                ("How many registered?", None, "42.", None, None),
            ]),
            "family": _chat(db, school=None, title="Family", audience="family", turns=[
                ("Do we qualify?", "aid_eligibility", "Maybe.", None, None),
            ]),
        }
        db.commit()
    return ids


class Harness:
    def __init__(self, factory):
        api = FastAPI()
        api.include_router(admin_conversations_router.router)

        def override_get_db():
            with factory() as session:
                yield session

        api.dependency_overrides[get_db] = override_get_db
        api.dependency_overrides[get_current_user] = lambda: CurrentUser(
            user_id=uuid.uuid4(), role=self.role, school_id=None)
        self.role = "super_admin"
        self.client = TestClient(api)

    def get(self, path, **params):
        return self.client.get(f"{ADMIN}{path}", params=params)


@pytest.fixture
def admin(factory):
    return Harness(factory)


def _titles(response):
    return [row["title"] for row in response.json()["items"]]


def test_list_defaults_to_counselor_chats_newest_first_with_counts(admin, chats):
    body = admin.get("/conversations").json()
    assert body["total"] == 3
    first = body["items"][0]
    assert (first["title"], first["school_name"], first["user_email"]) == ("FAFSA", "A High", "counselor@a-high.example")
    assert (first["questions"], first["helpful"], first["unhelpful"], first["flagged"]) == (2, 1, 1, 1)
    assert set(first["topics"]) == {"fafsa_filing", "css_profile"}
    assert body["items"][1]["deleted"] is True
    assert set(body["items"][1]["topics"]) == {"family_outreach_drafts", "other"}


def test_family_chats_hide_the_device_owner(admin, chats):
    [row] = admin.get("/conversations", audience="family").json()["items"]
    assert (row["title"], row["user_email"]) == ("Family", None)


@pytest.mark.parametrize("params,expected", [
    ({"school_id": str(SCHOOL_B)}, ["B chat"]),
    ({"feedback": "helpful"}, ["FAFSA"]),
    ({"feedback": "flagged"}, ["FAFSA"]),
    ({"feedback": "any"}, ["FAFSA"]),
    ({"topic": "family_outreach_drafts"}, ["Email"]),
    ({"topic": "other"}, ["Email"]),  # a retired slug counts as Other
    ({"topic": "css_profile", "school_id": str(SCHOOL_B)}, []),
])
def test_list_filters_combine(admin, chats, params, expected):
    assert _titles(admin.get("/conversations", **params)) == expected


def test_list_paginates(admin, chats):
    page = admin.get("/conversations", limit=1, offset=1).json()
    assert (page["total"], [row["title"] for row in page["items"]]) == (3, ["Email"])


def test_detail_shows_transcript_with_topics_and_feedback(admin, chats):
    body = admin.get(f"/conversations/{chats['fafsa']}").json()
    roles = [(m["role"], m["topic"]) for m in body["messages"]]
    assert roles == [("user", "fafsa_filing"), ("assistant", None), ("user", "css_profile"), ("assistant", None)]
    assert body["messages"][3]["feedback"] == {"rating": -1, "flag": "wrong_or_outdated", "comment": "Old deadline"}
    assert body["messages"][0]["feedback"] is None
    assert body["summary"] is None
    assert admin.get(f"/conversations/{uuid.uuid4()}").status_code == 404


def test_summary_is_stored_then_marked_stale_after_new_messages(admin, chats, factory, monkeypatch):
    seen = {}

    def fake_call(**kwargs):
        seen.update(kwargs)
        return {"summary": "Goal: FAFSA dates."}, 10, 5

    monkeypatch.setattr(conversation_summary, "call_json", fake_call)
    summary = admin.client.post(f"{ADMIN}/conversations/{chats['fafsa']}/summary").json()["summary"]
    assert (summary["text"], summary["messages"], summary["stale"]) == ("Goal: FAFSA dates.", 4, False)
    assert "Counselor: When does FAFSA open?" in seen["content"]
    assert "rated not helpful; flagged wrong or out of date" in seen["content"]

    with factory() as db:
        db.add(AssistantMessage(conversation_id=chats["fafsa"], role="user", content="More?",
                                created_at=START + timedelta(days=1)))
        db.commit()
    assert admin.get(f"/conversations/{chats['fafsa']}").json()["summary"]["stale"] is True


def test_summary_failure_is_a_readable_502_and_stores_nothing(admin, chats, monkeypatch):
    def boom(**_kwargs):
        raise BedrockCallError("throttled")

    monkeypatch.setattr(conversation_summary, "call_json", boom)
    response = admin.client.post(f"{ADMIN}/conversations/{chats['fafsa']}/summary")
    assert (response.status_code, response.json()["detail"]) == (502, "summary_failed")
    assert admin.get(f"/conversations/{chats['fafsa']}").json()["summary"] is None


def test_topic_counts_follow_audience_and_school(admin, chats):
    body = admin.get("/question-topics").json()
    counts = {t["slug"]: t["count"] for t in body["items"]}
    assert len(body["items"]) == 21 and body["items"][-1]["slug"] == "other"
    assert (counts["fafsa_filing"], counts["other"], counts["aid_eligibility"]) == (1, 1, 0)
    assert body["untagged"] == 1
    school_b = {t["slug"]: t["count"] for t in admin.get("/question-topics", school_id=str(SCHOOL_B)).json()["items"]}
    assert sum(school_b.values()) == 0
    family = {t["slug"]: t["count"] for t in admin.get("/question-topics", audience="family").json()["items"]}
    assert family["aid_eligibility"] == 1


def test_backfill_tags_untagged_questions(admin, chats, monkeypatch):
    monkeypatch.setattr(question_topics, "call_json", lambda **_k: ({"topic": "workshop_registrations"}, 1, 1))
    assert admin.client.post(f"{ADMIN}/question-topics/backfill").json() == {"tagged": 1, "failed": 0, "remaining": 0}
    assert admin.get("/conversations", topic="workshop_registrations").json()["items"][0]["title"] == "B chat"


def test_classifier_uses_previous_question_and_maps_unknown_to_other(monkeypatch):
    calls = []

    def fake_call(**kwargs):
        calls.append(kwargs["content"])
        return {"topic": "made_up"}, 1, 1

    monkeypatch.setattr(question_topics, "call_json", fake_call)
    assert question_topics.classify_question("yes, draft it", "Write an email to juniors") == "other"
    assert calls[0].startswith("Previous question: Write an email to juniors\nQuestion: yes, draft it")

    monkeypatch.setattr(question_topics, "call_json", lambda **_k: (_ for _ in ()).throw(BedrockCallError("x")))
    assert question_topics.classify_question("anything") is None


@pytest.mark.parametrize("method,path", [
    ("get", "/conversations"), ("get", "/question-topics"), ("post", "/question-topics/backfill"),
])
def test_admin_conversation_views_are_super_admin_only(admin, method, path):
    admin.role = "hub_admin"
    assert getattr(admin.client, method)(f"{ADMIN}{path}").status_code == 403
