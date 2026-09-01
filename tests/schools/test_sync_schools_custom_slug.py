"""The Airtable sync must never overwrite an admin-customized school slug.

`slug` is the authoritative public URL segment (migration 0108). An Airtable
slug change still propagates while `slug` tracks the Airtable value, but once an
admin has set a custom slug the sync leaves it alone — otherwise the next pull
would silently move a school's SRC URL back.
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
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
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


def test_custom_slug_survives_an_airtable_slug_change(db_session, monkeypatch):
    db_session.add(School(
        name="Dwight Global Online School",
        slug="dwight-global-online-school",   # admin-customized
        airtable_slug="dwight-global",        # what Airtable last said
        airtable_id="recDWIGHT",
    ))
    db_session.commit()

    _patch_airtable(monkeypatch, [
        _airtable_school("recDWIGHT", "Dwight Global Online School", "dwight-global-v2")
    ])
    sync_schools.sync_schools_from_airtable(db_session)

    school = db_session.query(School).filter(School.airtable_id == "recDWIGHT").one()
    assert school.slug == "dwight-global-online-school"
    # airtable_slug still tracks Airtable — it just no longer decides the URL
    assert school.airtable_slug == "dwight-global-v2"


def test_uncustomized_slug_follows_the_airtable_slug(db_session, monkeypatch):
    db_session.add(School(
        name="Polytechnic School",
        slug="polytechnic-school",
        airtable_slug="polytechnic-school",  # slug still tracks Airtable
        airtable_id="recPOLY",
    ))
    db_session.commit()

    _patch_airtable(monkeypatch, [
        _airtable_school("recPOLY", "Polytechnic School", "polytechnic-la")
    ])
    sync_schools.sync_schools_from_airtable(db_session)

    school = db_session.query(School).filter(School.airtable_id == "recPOLY").one()
    assert school.slug == "polytechnic-la"
    assert school.airtable_slug == "polytechnic-la"


def test_airtable_slug_change_is_not_propagated_onto_a_taken_slug(db_session, monkeypatch):
    """Unique constraint wins — the colliding school keeps its current slug."""
    db_session.add(School(
        name="Alpha Academy", slug="shared-slug", airtable_slug="shared-slug",
        airtable_id="recALPHA",
    ))
    db_session.add(School(
        name="Beta Academy", slug="beta-academy", airtable_slug="beta-academy",
        airtable_id="recBETA",
    ))
    db_session.commit()

    _patch_airtable(monkeypatch, [
        _airtable_school("recBETA", "Beta Academy", "shared-slug")
    ])
    sync_schools.sync_schools_from_airtable(db_session)

    beta = db_session.query(School).filter(School.airtable_id == "recBETA").one()
    assert beta.slug == "beta-academy"
    assert beta.airtable_slug == "shared-slug"
