"""Who may delete a contact.

`DELETE /contacts/{id}` used to be super-admin only. Directors (hub_admin) can now
offboard their own school's contacts, so the guard rails matter:

- own school only — a director must not reach another school's roster;
- never yourself — the cascade deletes the caller's Supabase user, which would
  revoke the session mid-request and could lock out a school's last director;
- counselors and viewers still can't delete anyone.

See `_authorize_contact_delete` in src/auth/router.py.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import src.auth.models  # noqa: F401 — register UserRole for FK metadata
import src.content.models  # noqa: F401 — register GradeSet/Cohort for FK metadata
import src.schools.models  # noqa: F401
from src.auth.deps import get_current_user
from src.auth.models import Profile, UserRole
from src.auth.schemas import CurrentUser
from src.db.base import Base
from src.db.client import get_supabase
from src.db.deps import get_db
from src.main import app
from src.schools.models import Contact, School

# Letter-only hex: SQLite gives postgresql.UUID columns NUMERIC affinity and
# corrupts all-digit hex on round trip.
SCHOOL_ID = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
OTHER_SCHOOL_ID = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")

SUPER_ADMIN_USER_ID = uuid.UUID("ffffffff-ffff-ffff-ffff-ffffffffffff")

# The calling director and their own contact row.
DIRECTOR_USER_ID = uuid.UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")
DIRECTOR_CONTACT_ID = uuid.UUID("cccccccc-cccc-cccc-cccc-cccccccccccd")

# Admin/director-created teammate at the director's school — the deletable case.
TEAMMATE_USER_ID = uuid.UUID("dddddddd-dddd-dddd-dddd-dddddddddddd")
TEAMMATE_CONTACT_ID = uuid.UUID("dddddddd-dddd-dddd-dddd-ddddddddddde")

# Same school, but synced from Airtable.
AIRTABLE_CONTACT_ID = uuid.UUID("eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee")
# Another school's contact, and one that was never assigned a school.
OTHER_SCHOOL_CONTACT_ID = uuid.UUID("abababab-abab-abab-abab-abababababab")
NO_SCHOOL_CONTACT_ID = uuid.UUID("bcbcbcbc-bcbc-bcbc-bcbc-bcbcbcbcbcbc")


class FakeSupabaseAdmin:
    def __init__(self) -> None:
        self.deleted: list[str] = []

    def delete_user(self, user_id: str) -> None:
        self.deleted.append(user_id)


class FakeSupabase:
    def __init__(self) -> None:
        self.auth = type("_Auth", (), {"admin": FakeSupabaseAdmin()})()


@pytest.fixture
def client():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    tables = [
        t
        for n, t in Base.metadata.tables.items()
        if n
        in (
            "contacts",
            "schools",
            "cohorts",
            "grade_sets",
            "user_roles",
            "profiles",
            "email_suppression",
        )
    ]
    Base.metadata.create_all(engine, tables=tables)
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)

    seed = SessionLocal()
    seed.add(School(id=SCHOOL_ID, name="Acme High", slug="acme-high"))
    seed.add(School(id=OTHER_SCHOOL_ID, name="Rival High", slug="rival-high"))

    seed.add(
        Contact(
            id=DIRECTOR_CONTACT_ID, user_id=DIRECTOR_USER_ID, school_id=SCHOOL_ID,
            email="director@example.com", first_name="Dee", last_name="Rector",
        )
    )
    seed.add(UserRole(user_id=DIRECTOR_USER_ID, role="hub_admin", school_id=SCHOOL_ID))
    seed.add(Profile(user_id=DIRECTOR_USER_ID, email="director@example.com"))

    seed.add(
        Contact(
            id=TEAMMATE_CONTACT_ID, user_id=TEAMMATE_USER_ID, school_id=SCHOOL_ID,
            email="teammate@example.com", first_name="Tam", last_name="Mate",
        )
    )
    seed.add(UserRole(user_id=TEAMMATE_USER_ID, role="hub_user", school_id=SCHOOL_ID))
    seed.add(Profile(user_id=TEAMMATE_USER_ID, email="teammate@example.com"))

    seed.add(
        Contact(
            id=AIRTABLE_CONTACT_ID, school_id=SCHOOL_ID, airtable_id="recSynced1",
            email="synced@example.com", first_name="Sam", last_name="Synced",
        )
    )
    seed.add(
        Contact(
            id=OTHER_SCHOOL_CONTACT_ID, school_id=OTHER_SCHOOL_ID,
            email="rival@example.com", first_name="Riva", last_name="Else",
        )
    )
    seed.add(
        Contact(id=NO_SCHOOL_CONTACT_ID, email="floating@example.com", first_name="Flo")
    )
    seed.commit()
    seed.close()

    def override_get_db():
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    # Mutable so a test can switch who is calling (see `as_role`).
    caller: dict[str, CurrentUser] = {
        "user": CurrentUser(user_id=DIRECTOR_USER_ID, role="hub_admin", school_id=SCHOOL_ID)
    }
    fake_supabase = FakeSupabase()
    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_supabase] = lambda: fake_supabase
    app.dependency_overrides[get_current_user] = lambda: caller["user"]

    c = TestClient(app)
    c._session_local = SessionLocal
    c._supabase = fake_supabase
    c._caller = caller
    yield c
    app.dependency_overrides.clear()


def as_role(client, role: str, *, user_id=None, school_id=SCHOOL_ID) -> None:
    """Switch the calling identity for the rest of the test."""
    client._caller["user"] = CurrentUser(
        user_id=user_id or DIRECTOR_USER_ID, role=role, school_id=school_id
    )


def _contact(client, contact_id: uuid.UUID) -> Contact | None:
    db = client._session_local()
    try:
        return db.query(Contact).filter(Contact.id == contact_id).first()
    finally:
        db.close()


class TestDirectorCanDeleteOwnSchool:
    def test_director_deletes_a_teammate_at_their_school(self, client):
        resp = client.delete(f"/api/v1/contacts/{TEAMMATE_CONTACT_ID}")
        assert resp.status_code == 204

        contact = _contact(client, TEAMMATE_CONTACT_ID)
        assert contact.deleted_at is not None
        # Cascade still applies for a director-initiated delete.
        assert contact.user_id is None
        assert str(TEAMMATE_USER_ID) in client._supabase.auth.admin.deleted

        db = client._session_local()
        try:
            assert db.query(UserRole).filter(UserRole.user_id == TEAMMATE_USER_ID).first() is None
        finally:
            db.close()

    def test_director_still_cannot_delete_an_airtable_contact(self, client):
        resp = client.delete(f"/api/v1/contacts/{AIRTABLE_CONTACT_ID}")
        assert resp.status_code == 400
        assert "Airtable" in resp.json()["detail"]
        assert _contact(client, AIRTABLE_CONTACT_ID).deleted_at is None


class TestSelfDeleteBlocked:
    def test_director_cannot_delete_their_own_contact(self, client):
        resp = client.delete(f"/api/v1/contacts/{DIRECTOR_CONTACT_ID}")
        assert resp.status_code == 400
        assert "your own contact" in resp.json()["detail"]

        # Crucially the login survives — the caller keeps their session.
        contact = _contact(client, DIRECTOR_CONTACT_ID)
        assert contact.deleted_at is None
        assert contact.user_id == DIRECTOR_USER_ID
        assert client._supabase.auth.admin.deleted == []

    def test_super_admin_cannot_delete_their_own_contact_either(self, client):
        as_role(client, "super_admin", user_id=DIRECTOR_USER_ID, school_id=None)
        resp = client.delete(f"/api/v1/contacts/{DIRECTOR_CONTACT_ID}")
        assert resp.status_code == 400
        assert _contact(client, DIRECTOR_CONTACT_ID).deleted_at is None


class TestCrossSchoolBlocked:
    def test_director_cannot_delete_another_schools_contact(self, client):
        resp = client.delete(f"/api/v1/contacts/{OTHER_SCHOOL_CONTACT_ID}")
        assert resp.status_code == 403
        assert _contact(client, OTHER_SCHOOL_CONTACT_ID).deleted_at is None

    def test_director_cannot_delete_a_contact_with_no_school(self, client):
        """A school-less contact belongs to no director — super-admin territory."""
        resp = client.delete(f"/api/v1/contacts/{NO_SCHOOL_CONTACT_ID}")
        assert resp.status_code == 403
        assert _contact(client, NO_SCHOOL_CONTACT_ID).deleted_at is None


class TestLowerRolesBlocked:
    @pytest.mark.parametrize("role", ["hub_user", "viewer"])
    def test_counselors_and_viewers_cannot_delete(self, client, role):
        as_role(client, role, user_id=SUPER_ADMIN_USER_ID)
        resp = client.delete(f"/api/v1/contacts/{TEAMMATE_CONTACT_ID}")
        assert resp.status_code == 403
        assert _contact(client, TEAMMATE_CONTACT_ID).deleted_at is None

    def test_revoke_access_stays_super_admin_only(self, client):
        """Only DELETE was opened up; revoke-access is still an admin tool."""
        resp = client.post(f"/api/v1/contacts/{TEAMMATE_CONTACT_ID}/revoke-access")
        assert resp.status_code == 403
        assert _contact(client, TEAMMATE_CONTACT_ID).user_id == TEAMMATE_USER_ID


class TestSuperAdminUnaffected:
    def test_super_admin_deletes_across_schools(self, client):
        as_role(client, "super_admin", user_id=SUPER_ADMIN_USER_ID, school_id=None)
        resp = client.delete(f"/api/v1/contacts/{OTHER_SCHOOL_CONTACT_ID}")
        assert resp.status_code == 204
        assert _contact(client, OTHER_SCHOOL_CONTACT_ID).deleted_at is not None

    def test_super_admin_deletes_a_school_less_contact(self, client):
        as_role(client, "super_admin", user_id=SUPER_ADMIN_USER_ID, school_id=None)
        resp = client.delete(f"/api/v1/contacts/{NO_SCHOOL_CONTACT_ID}")
        assert resp.status_code == 204
        assert _contact(client, NO_SCHOOL_CONTACT_ID).deleted_at is not None


def test_deleted_at_is_timezone_aware_utc(client):
    """Sanity check on the stamp the whole soft-delete filter depends on."""
    assert client.delete(f"/api/v1/contacts/{TEAMMATE_CONTACT_ID}").status_code == 204
    stamp = _contact(client, TEAMMATE_CONTACT_ID).deleted_at
    assert stamp is not None
    # SQLite drops tzinfo on round trip; compare naively against "now".
    assert abs((stamp.replace(tzinfo=None) - datetime.now(timezone.utc).replace(tzinfo=None)).total_seconds()) < 60
