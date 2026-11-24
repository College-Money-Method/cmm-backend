"""Counselor answer feedback (owner-only upsert) and the admin feedback/usage views."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.assistant import admin_feedback_router, counselor_feedback_router, counselor_router
from src.assistant.models import AssistantConversation, AssistantMessage, AssistantMessageFeedback
from src.auth.deps import get_current_user
from src.auth.models import Profile
from src.auth.schemas import CurrentUser
from src.db.deps import get_db
from src.schools.models import School
from tests.assistant.counselor_test_support import make_counselor_factory

BASE = "/api/v1/assistant/counselor"
ADMIN = "/api/v1/admin/assistant"
SCHOOL = uuid.uuid4()
OWNER = uuid.uuid4()


@pytest.fixture
def factory():
    return make_counselor_factory()


@pytest.fixture
def chat(factory):
    """One counselor conversation with a question and an answer; returns their ids."""
    ids = {"conversation": uuid.uuid4(), "question": uuid.uuid4(), "answer": uuid.uuid4()}
    now = datetime.now(UTC)
    with factory() as db:
        db.add(School(id=SCHOOL, name="A High", slug="a-high", state="TX"))
        db.add(Profile(user_id=OWNER, email="counselor@a-high.example"))
        db.commit()  # the conversation's school must exist first (no ORM relationship orders them)
        db.add(AssistantConversation(
            id=ids["conversation"], local_user_id=OWNER, audience="counselor", title="FAFSA",
            school_id=SCHOOL, card_type="grade_checklist",
        ))
        db.add_all([
            AssistantMessage(id=ids["question"], conversation_id=ids["conversation"], role="user",
                             content="When does FAFSA open?", created_at=now),
            AssistantMessage(id=ids["answer"], conversation_id=ids["conversation"], role="assistant",
                             content="October 1.", created_at=now + timedelta(seconds=1)),
        ])
        db.commit()
    return ids


class Harness:
    def __init__(self, factory):
        self.user: CurrentUser | None = None
        api = FastAPI()
        for module in (counselor_router, counselor_feedback_router, admin_feedback_router):
            api.include_router(module.router)

        def override_get_db():
            session = factory()
            try:
                yield session
            finally:
                session.close()

        api.dependency_overrides[get_db] = override_get_db
        api.dependency_overrides[get_current_user] = lambda: self.user
        self.client = TestClient(api)

    def as_user(self, role, user_id=None):
        self.user = CurrentUser(user_id=user_id or uuid.uuid4(), role=role, school_id=SCHOOL)

    def rate(self, message_id, **body):
        return self.client.put(f"{BASE}/messages/{message_id}/feedback", json=body)


@pytest.fixture
def harness(factory):
    return Harness(factory)


def test_owner_rates_an_answer_and_a_second_put_replaces_it(harness, chat, factory):
    harness.as_user("hub_user", OWNER)
    assert harness.rate(chat["answer"], rating=-1, comment="  Wrong date  ").json() == {
        "rating": -1, "flag": None, "comment": "Wrong date",
    }
    assert harness.rate(chat["answer"], rating=-1, flag="wrong_or_outdated", comment="").status_code == 200
    with factory() as db:
        row = db.query(AssistantMessageFeedback).one()
        assert (row.rating, row.flag, row.comment) == (-1, "wrong_or_outdated", None)
    messages = harness.client.get(f"{BASE}/conversations/{chat['conversation']}").json()["messages"]
    assert messages[0]["feedback"] is None
    assert messages[1]["feedback"] == {"rating": -1, "flag": "wrong_or_outdated", "comment": None}


@pytest.mark.parametrize("who", ["stranger", "question", "deleted", "missing"])
def test_feedback_is_404_unless_it_is_your_visible_answer(harness, chat, factory, who):
    harness.as_user("hub_user", uuid.uuid4() if who == "stranger" else OWNER)
    target = {"question": chat["question"], "missing": uuid.uuid4()}.get(who, chat["answer"])
    if who == "deleted":
        with factory() as db:
            db.get(AssistantConversation, chat["conversation"]).deleted_at = datetime.now(UTC)
            db.commit()
    assert harness.rate(target, rating=1).status_code == 404


@pytest.mark.parametrize("body", [{"rating": 0}, {"flag": "rude"}, {"comment": "x" * 2001}])
def test_invalid_feedback_is_422(harness, chat, body):
    harness.as_user("hub_user", OWNER)
    assert harness.rate(chat["answer"], **body).status_code == 422


def test_viewer_cannot_give_feedback(harness, chat):
    harness.as_user("viewer", OWNER)
    assert harness.rate(chat["answer"], rating=1).status_code == 403


def test_admin_feedback_lists_question_answer_school_and_email(harness, chat):
    harness.as_user("hub_user", OWNER)
    harness.rate(chat["answer"], rating=-1, flag="wrong_or_outdated", comment="Outdated")
    harness.as_user("super_admin")
    items = harness.client.get(f"{ADMIN}/feedback").json()["items"]
    assert len(items) == 1
    row = items[0]
    assert (row["question"], row["answer"], row["school_name"]) == ("When does FAFSA open?", "October 1.", "A High")
    assert row["conversation_id"] == str(chat["conversation"])
    assert (row["counselor_email"], row["card_type"], row["comment"]) == (
        "counselor@a-high.example", "grade_checklist", "Outdated",
    )
    assert harness.client.get(f"{ADMIN}/feedback", params={"rating": 1}).json()["items"] == []
    assert len(harness.client.get(f"{ADMIN}/feedback", params={"flag": "wrong_or_outdated"}).json()["items"]) == 1


def test_usage_counts_deleted_chats(harness, chat):
    harness.as_user("hub_user", OWNER)
    assert harness.client.delete(f"{BASE}/conversations/{chat['conversation']}").status_code == 204
    harness.as_user("super_admin")
    [row] = harness.client.get(f"{ADMIN}/usage").json()["items"]
    assert row["school_name"] == "A High"
    assert (row["counselors"], row["conversations"], row["deleted_conversations"], row["questions"]) == (1, 1, 1, 1)


@pytest.mark.parametrize("path", ["/feedback", "/usage"])
def test_admin_views_are_super_admin_only(harness, path):
    harness.as_user("hub_admin")
    assert harness.client.get(f"{ADMIN}{path}").status_code == 403
