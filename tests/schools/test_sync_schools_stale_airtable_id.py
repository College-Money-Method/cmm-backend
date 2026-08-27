"""When an Airtable school record is deleted and recreated, the DB row keeps the
dead record ID. Every airtable_id-keyed sync then silently skips that school —
webinar sync stops creating its portal_mapping rows, contact sync stops linking
its counselors. The schools sync must refresh a dead ID onto the row it matched
by slug/name, while never reassigning an ID that another live record owns.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import src.db.models  # noqa: F401 — registers every mapper so schools' FKs resolve
from src.db.base import Base
from src.schools import sync_schools
from src.schools.models import School


def _airtable_school(rec_id: str, name: str, slug: str) -> dict:
    return {"id": rec_id, "fields": {"School": name, "slug": slug, "Current Customer": True}}


@pytest.fixture
def db_session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    tables = [
        t for n, t in Base.metadata.tables.items()
        if n in ("schools", "cohorts", "grade_sets")
    ]
    Base.metadata.create_all(engine, tables=tables)
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
    db = SessionLocal()
    yield db
    db.close()


def _patch_airtable(monkeypatch, school_records: list[dict]) -> None:
    monkeypatch.setattr(sync_schools, "get_schools_records", lambda: school_records)
    monkeypatch.setattr(sync_schools, "get_cohorts_records", lambda: [])


def test_dead_airtable_id_is_refreshed_from_slug_match(db_session, monkeypatch):
    db_session.add(School(name="Polytechnic School", slug="polytechnic-school", airtable_id="recDEAD"))
    db_session.commit()

    _patch_airtable(monkeypatch, [_airtable_school("recLIVE", "Polytechnic School", "polytechnic-school")])
    result = sync_schools.sync_schools_from_airtable(db_session)

    school = db_session.query(School).filter_by(slug="polytechnic-school").one()
    assert school.airtable_id == "recLIVE"
    assert result["airtable_ids_refreshed"] == 1
    assert result["schools_created"] == 0


def test_live_airtable_id_is_never_reassigned(db_session, monkeypatch):
    """Two DB rows sharing a name: the ID belonging to a record still present in
    the pull must stay put, so a name collision can't move it onto another row.
    """
    db_session.add(School(name="Riverdale Country School", slug="riverdale-a", airtable_id="recA"))
    db_session.add(School(name="Riverdale Country School", slug="riverdale-b", airtable_id="recB"))
    db_session.commit()

    # recB pulled under slug riverdale-a: slug matches row A, whose recA is still live.
    _patch_airtable(monkeypatch, [_airtable_school("recB", "Riverdale Country School", "riverdale-a")])
    result = sync_schools.sync_schools_from_airtable(db_session)

    assert db_session.query(School).filter_by(slug="riverdale-a").one().airtable_id == "recA"
    assert db_session.query(School).filter_by(slug="riverdale-b").one().airtable_id == "recB"
    assert result["airtable_ids_refreshed"] == 0


def test_claimed_new_id_is_not_stolen(db_session, monkeypatch):
    """The dead-ID row must not take an ID another DB row already holds — that
    would violate the airtable_id unique constraint.
    """
    db_session.add(School(name="Stale School", slug="stale-school", airtable_id="recDEAD"))
    db_session.add(School(name="Owner School", slug="owner-school", airtable_id="recOWNED"))
    db_session.commit()

    _patch_airtable(monkeypatch, [_airtable_school("recOWNED", "Stale School", "stale-school")])
    result = sync_schools.sync_schools_from_airtable(db_session)

    assert db_session.query(School).filter_by(slug="stale-school").one().airtable_id == "recDEAD"
    assert db_session.query(School).filter_by(slug="owner-school").one().airtable_id == "recOWNED"
    assert result["airtable_ids_refreshed"] == 0


def test_matching_airtable_id_is_untouched(db_session, monkeypatch):
    db_session.add(School(name="Steady School", slug="steady-school", airtable_id="recSAME"))
    db_session.commit()

    _patch_airtable(monkeypatch, [_airtable_school("recSAME", "Steady School", "steady-school")])
    result = sync_schools.sync_schools_from_airtable(db_session)

    assert db_session.query(School).filter_by(slug="steady-school").one().airtable_id == "recSAME"
    assert result["airtable_ids_refreshed"] == 0
    assert result["schools_updated"] == 1
