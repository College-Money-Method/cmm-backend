"""POST /contacts emails the new hub user their login.

Same in-memory SQLite + TestClient + dependency_overrides setup as
tests/auth/test_create_contact_opts_into_emails.py. ``send_email`` is patched
where the welcome module imports it, so nothing reaches SES.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import src.auth.models  # noqa: F401 — register UserRole for FK metadata
from src.auth import hub_welcome_email
from src.auth.deps import get_current_user
from src.auth.schemas import CurrentUser
from src.config import settings
from src.db.base import Base
from src.db.client import get_supabase
from src.db.deps import get_db
from src.emails.models import EmailSendLog
from src.main import app
from src.schools.models import Contact, School

# Letter-only hex — SQLite gives postgresql.UUID columns NUMERIC affinity and
# corrupts all-digit hex on round trip (see test_contact_auto_emails_self_edit).
SCHOOL_ID = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
ADMIN_USER_ID = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
NEW_USER_ID = uuid.UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")
NEW_EMAIL = "jane.doe@hampton.edu"
ORIGIN = "https://collegemoneymethod.com"


class _FakeUser:
    def __init__(self) -> None:
        self.id = str(NEW_USER_ID)
        self.email = NEW_EMAIL
        self.user_metadata = {"first_name": "Jane", "last_name": "Doe"}


class _FakeAdmin:
    def __init__(self, already_exists: bool) -> None:
        self.already_exists = already_exists

    def create_user(self, params: dict):
        if self.already_exists:
            raise RuntimeError("A user with this email address has already been registered")
        return type("_Resp", (), {"user": _FakeUser()})()

    def list_users(self):
        return [_FakeUser()]

    def update_user_by_id(self, user_id, attrs):
        return None


class _FakeSupabase:
    def __init__(self, already_exists: bool = False) -> None:
        self.auth = type("_Auth", (), {"admin": _FakeAdmin(already_exists)})()


@pytest.fixture
def sent(monkeypatch):
    calls: list[dict] = []

    def fake_send(db, to, subject, html, text, source, **kwargs):
        calls.append({"to": to, "subject": subject, "html": html, "text": text, "source": source, **kwargs})

    monkeypatch.setattr(hub_welcome_email, "send_email", fake_send)
    monkeypatch.setattr(settings, "app_public_url", ORIGIN)
    return calls


@pytest.fixture
def make_client():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    wanted = ("contacts", "schools", "user_roles", "profiles", "cohorts", "grade_sets", "email_suppression")
    Base.metadata.create_all(engine, tables=[t for n, t in Base.metadata.tables.items() if n in wanted])
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)

    seed = SessionLocal()
    seed.add(School(id=SCHOOL_ID, name="Hampton <High>", cmm_website_password="hscmm"))
    seed.add(Contact(email="maria@hampton.edu", first_name="Maria", last_name="Lopez",
                     user_id=ADMIN_USER_ID, school_id=SCHOOL_ID))
    seed.commit()
    seed.close()

    def override_get_db():
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    def _make(role: str = "super_admin", already_exists: bool = False) -> TestClient:
        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_supabase] = lambda: _FakeSupabase(already_exists)
        app.dependency_overrides[get_current_user] = lambda: CurrentUser(
            user_id=ADMIN_USER_ID, role=role,
            school_id=SCHOOL_ID if role == "hub_admin" else None, email="maria@hampton.edu",
        )
        return TestClient(app)

    yield _make
    app.dependency_overrides.clear()


def _post(client: TestClient, **body) -> None:
    payload = {"email": NEW_EMAIL, "role": "hub_user", "school_id": str(SCHOOL_ID), **body}
    resp = client.post("/api/v1/contacts", json=payload)
    assert resp.status_code in (200, 201), resp.text


def test_super_admin_adding_a_user_emails_the_default_login(make_client, sent):
    _post(make_client())

    assert len(sent) == 1
    mail = sent[0]
    assert mail["to"] == NEW_EMAIL  # only the new user, no CC to the adder
    assert mail["source"] == "hub_welcome"
    assert mail["school_id"] == SCHOOL_ID
    for part in (f"{ORIGIN}/hub/login", f"{ORIGIN}/forgot-password", f"Email: {NEW_EMAIL}",
                 "Password: jane.doehscmm", "as a User.", "Maria Lopez added you"):
        assert part in mail["text"]


def test_director_adding_an_admin_gets_the_admin_role_line(make_client, sent):
    _post(make_client(role="hub_admin"), role="hub_admin", school_id=None)

    assert len(sent) == 1
    assert "as an Admin. Admins can also add other counselors to your school." in sent[0]["text"]


def test_viewer_gets_the_email_too(make_client, sent):
    _post(make_client(), role="viewer")

    assert len(sent) == 1
    assert "as a Viewer." in sent[0]["text"]


def test_a_custom_password_is_never_emailed(make_client, sent):
    _post(make_client(), password="S3cret-custom!")

    mail = sent[0]
    for body in (mail["text"], mail["html"]):
        assert "S3cret-custom!" not in body
        assert "jane.doehscmm" not in body
    assert "the one Maria Lopez set for you" in mail["text"]


def test_an_existing_auth_user_also_gets_the_email(make_client, sent):
    _post(make_client(already_exists=True))

    assert len(sent) == 1
    assert "Password: jane.doehscmm" in sent[0]["text"]


def test_no_access_contacts_get_no_email(make_client, sent):
    _post(make_client(), role="no_access")

    assert sent == []


def test_a_failed_send_does_not_fail_the_create(make_client, monkeypatch):
    monkeypatch.setattr(settings, "app_public_url", ORIGIN)

    def boom(db, to, subject, html, text, source, **kwargs):
        # Fail on a send-log flush (the table isn't created here), which leaves
        # the session needing a rollback before the endpoint can build its response.
        db.add(EmailSendLog(recipient_email=to, subject=subject, status="sent", source=source))
        db.flush()

    monkeypatch.setattr(hub_welcome_email, "send_email", boom)
    _post(make_client())


def test_html_escapes_the_school_name(make_client, sent):
    _post(make_client())

    assert "Hampton &lt;High&gt;" in sent[0]["html"]
    assert "Hampton <High>" not in sent[0]["html"]
