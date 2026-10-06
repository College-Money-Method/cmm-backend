"""The Airtable sync must not wipe a self-serve preview's generated password."""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import src.db.models  # noqa: F401
from src.db.base import Base
from src.schools import sync_schools
from src.schools.models import School


@pytest.fixture
def db_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    tables = [t for n, t in Base.metadata.tables.items() if n in ("schools", "cohorts", "grade_sets")]
    Base.metadata.create_all(engine, tables=tables)
    db = sessionmaker(bind=engine, expire_on_commit=False)()
    yield db
    db.close()


def _run(db, monkeypatch, *, customer: bool):
    record = {"id": "recX", "fields": {"School": "Sunrise Academy", "slug": "sunrise-academy",
                                       "Current Customer": customer}}
    monkeypatch.setattr(sync_schools, "get_schools_records", lambda: [record])
    monkeypatch.setattr(sync_schools, "get_cohorts_records", lambda: [])
    sync_schools.sync_schools_from_airtable(db)
    db.expire_all()
    return db.query(School).filter_by(slug="sunrise-academy").one()


def _preview(db):
    db.add(School(name="Sunrise Academy", slug="sunrise-academy", airtable_id="recX",
                  is_src_preview=True, is_cmm_website_activated=True, cmm_website_password="sacmm"))
    db.commit()


def test_password_kept_while_preview(db_session, monkeypatch):
    _preview(db_session)
    school = _run(db_session, monkeypatch, customer=False)
    assert school.cmm_website_password == "sacmm" and school.is_src_preview


def test_conversion_clears_preview_and_takes_airtable_password(db_session, monkeypatch):
    _preview(db_session)
    school = _run(db_session, monkeypatch, customer=True)
    assert school.is_current_customer and not school.is_src_preview
    assert school.cmm_website_password != "sacmm"
