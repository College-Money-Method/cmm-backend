"""School list-version fingerprint drives frontend cache invalidation.

The version must change when the list changes: a new school (count up), an edit
to an existing school (max updated_at moves), or a deletion (count down). If any
of these left the version unchanged, cached selectors would show stale data.
"""

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import src.content.models  # noqa: F401 — register GradeSet/Cohort for FK metadata
import src.schools.models  # noqa: F401
from src.auth.deps import get_current_user
from src.auth.schemas import CurrentUser
from src.db.base import Base
from src.db.client import get_supabase
from src.db.deps import get_db
from src.main import app
from src.schools.models import School

ADMIN = CurrentUser(user_id=uuid.uuid4(), role="super_admin")


@pytest.fixture
def ctx():
    """TestClient + a session factory sharing one in-memory DB, seeded with schools."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    tables = [t for n, t in Base.metadata.tables.items() if n in ("schools", "cohorts", "grade_sets")]
    Base.metadata.create_all(engine, tables=tables)
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)

    seed = SessionLocal()
    seed.add(School(id=uuid.uuid4(), name="Acme High", is_current_customer=True))
    seed.add(School(id=uuid.uuid4(), name="Beta Prep", is_current_customer=True))
    seed.commit()
    seed.close()

    def override_get_db():
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_supabase] = lambda: None
    app.dependency_overrides[get_current_user] = lambda: ADMIN
    yield TestClient(app), SessionLocal
    app.dependency_overrides.clear()


def _version(client: TestClient) -> str:
    resp = client.get("/api/v1/schools/list-version")
    assert resp.status_code == 200
    return resp.json()["version"]


def test_version_is_stable_without_changes(ctx):
    client, _ = ctx
    assert _version(client) == _version(client)


def test_version_changes_on_create(ctx):
    client, SessionLocal = ctx
    before = _version(client)
    db = SessionLocal()
    db.add(School(id=uuid.uuid4(), name="Gamma Academy", is_current_customer=True))
    db.commit()
    db.close()
    assert _version(client) != before


def test_version_changes_on_delete(ctx):
    client, SessionLocal = ctx
    before = _version(client)
    db = SessionLocal()
    victim = db.query(School).first()
    # Bulk delete avoids ORM relationship cascade (contacts table isn't created here).
    db.query(School).filter(School.id == victim.id).delete(synchronize_session=False)
    db.commit()
    db.close()
    assert _version(client) != before
