"""A counselor's hub is read from UserRole.school_id, so provisioning must keep
that column following the contact when Airtable moves the person to a different
school. Regression: mmiller@dwight.global kept landing in The Dwight School New
York's hub after her contact had already moved to Dwight Global Online School.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

# src.db.models imports every model module — needed so the FKs on `schools`
# (cohorts, grade_sets) resolve when SQLite DDL is generated.
import src.db.models  # noqa: F401
from src.auth.models import UserRole
from src.db.base import Base
from src.schools import sync_provisioning
from src.schools.models import Contact, School


@pytest.fixture
def db_session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    # schools carries FKs to cohorts/grade_sets, so those must exist for
    # SQLite DDL generation even though these tests never populate them.
    wanted = ("contacts", "schools", "user_roles", "profiles", "cohorts", "grade_sets")
    tables = [t for n, t in Base.metadata.tables.items() if n in wanted]
    Base.metadata.create_all(engine, tables=tables)
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
    db = SessionLocal()
    yield db
    db.close()


class _FakeAdmin:
    def list_users(self, page: int, per_page: int) -> list:
        return []


class _FakeSupabase:
    """Provisioning only reads the auth directory here — the contact in these
    tests is already linked to a user_id, so create_user is never reached."""

    def __init__(self) -> None:
        self.auth = type("_Auth", (), {"admin": _FakeAdmin()})()


def _seed(db, *, role_school, contact_school):
    old = School(id=uuid.uuid4(), name="The Dwight School New York")
    new = School(id=uuid.uuid4(), name="Dwight Global Online School")
    db.add_all([old, new])

    user_id = uuid.uuid4()
    schools = {"old": old, "new": new}
    db.add(Contact(
        id=uuid.uuid4(),
        email="mmiller@dwight.global",
        first_name="Megan",
        last_name="Miller",
        role="Director",
        user_id=user_id,
        school_id=schools[contact_school].id,
    ))
    db.add(UserRole(
        id=uuid.uuid4(),
        user_id=user_id,
        role="hub_admin",
        school_id=schools[role_school].id,
        school_role="Director",
    ))
    db.commit()
    return user_id, old, new


def test_existing_role_follows_contact_to_new_school(db_session, monkeypatch):
    monkeypatch.setattr(sync_provisioning, "upsert_profile", lambda *a, **k: None)
    user_id, _old, new = _seed(db_session, role_school="old", contact_school="new")

    result = sync_provisioning.provision_counselors_from_contacts(
        db_session, _FakeSupabase()
    )

    role = db_session.query(UserRole).filter_by(user_id=user_id).one()
    assert role.school_id == new.id
    assert result["roles_reassigned"] == 1


def test_role_already_matching_is_left_alone(db_session, monkeypatch):
    monkeypatch.setattr(sync_provisioning, "upsert_profile", lambda *a, **k: None)
    user_id, _old, new = _seed(db_session, role_school="new", contact_school="new")

    result = sync_provisioning.provision_counselors_from_contacts(
        db_session, _FakeSupabase()
    )

    role = db_session.query(UserRole).filter_by(user_id=user_id).one()
    assert role.school_id == new.id
    assert result["roles_reassigned"] == 0
