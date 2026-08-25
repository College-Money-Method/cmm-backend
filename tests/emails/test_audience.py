"""Tests for `resolve_audience` — the broadcast filter resolver.

Covers: school/cohort targeting restricted to current-customer schools (even
when a caller asks for a prospect explicitly, and even for a prospect whose
School Resource Center an admin activated), the school+cohort union, role
filter, the `broadcast_emails` opt-in filter, and the exclusion of
deactivated/emailless contacts.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.auth.models import UserRole
from src.cycles.models import Cohort
from src.db.base import Base
from src.emails.audience import resolve_audience
from src.main import app  # noqa: F401 — importing registers every model for FK/mapper metadata
from src.schools.models import Contact, School

# Letter-only hex UUIDs — see tests/auth/test_contact_auto_emails_self_edit.py
# for the documented SQLite NUMERIC-affinity coercion bug this avoids.
CUSTOMER_SCHOOL_ID = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
PROSPECT_SCHOOL_ID = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
ACTIVATED_PROSPECT_SCHOOL_ID = uuid.UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")
COHORT_CUSTOMER_SCHOOL_ID = uuid.UUID("dadadada-dada-dada-dada-dadadadadada")
COHORT_ID = uuid.UUID("bcbcbcbc-bcbc-bcbc-bcbc-bcbcbcbcbcbc")

# Auth user ids for the hub_admin/hub_user contacts (letter-only, same reason).
USER_CUSTOMER_ADMIN = uuid.UUID("dddddddd-dddd-dddd-dddd-dddddddddddd")
USER_CUSTOMER_USER = uuid.UUID("eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee")
USER_ACTIVATED_ADMIN = uuid.UUID("ffffffff-ffff-ffff-ffff-ffffffffffff")
USER_PROSPECT_ADMIN = uuid.UUID("abababab-abab-abab-abab-abababababab")
USER_COHORT_ADMIN = uuid.UUID("acacacac-acac-acac-acac-acacacacacac")


@pytest.fixture
def db_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    tables = [
        t
        for n, t in Base.metadata.tables.items()
        if n in ("contacts", "schools", "cohorts", "grade_sets", "user_roles")
    ]
    Base.metadata.create_all(engine, tables=tables)
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
    db = SessionLocal()
    yield db
    db.close()


def _seed_standard_fixture(db) -> None:
    """Seed 2 customer-school contacts (1 hub_admin opted-in, 1 hub_user not
    opted-in), 1 customer contact in the seeded cohort, 1 prospect contact whose
    SRC an admin activated (also in that cohort — still unreachable), and 1 plain
    prospect contact. Only the customer-school contacts are ever addressable."""
    db.add(Cohort(id=COHORT_ID, name="Fall 2026"))
    db.add(School(id=CUSTOMER_SCHOOL_ID, name="Customer High", is_current_customer=True))
    db.add(
        School(
            id=COHORT_CUSTOMER_SCHOOL_ID,
            name="Cohort Customer High",
            is_current_customer=True,
            cohort_id=COHORT_ID,
        )
    )
    db.add(
        School(
            id=ACTIVATED_PROSPECT_SCHOOL_ID,
            name="Activated Prospect High",
            is_current_customer=False,
            is_cmm_website_activated=True,
            cohort_id=COHORT_ID,
        )
    )
    db.add(School(id=PROSPECT_SCHOOL_ID, name="Prospect High", is_current_customer=False))

    # Contact.role holds the Airtable job title (display only). The hub_admin
    # *app role* that the role filter keys on lives in user_roles, linked by
    # user_id — seeded alongside each contact below.
    db.add(
        Contact(
            school_id=CUSTOMER_SCHOOL_ID,
            user_id=USER_CUSTOMER_ADMIN,
            email="admin@customer.example",
            role="Director",
            broadcast_emails=True,
        )
    )
    db.add(
        Contact(
            school_id=CUSTOMER_SCHOOL_ID,
            user_id=USER_CUSTOMER_USER,
            email="user@customer.example",
            role="Counselor",
            broadcast_emails=False,
        )
    )
    db.add(
        Contact(
            school_id=COHORT_CUSTOMER_SCHOOL_ID,
            user_id=USER_COHORT_ADMIN,
            email="admin@cohort.example",
            role="Director",
            broadcast_emails=True,
        )
    )
    db.add(
        Contact(
            school_id=ACTIVATED_PROSPECT_SCHOOL_ID,
            user_id=USER_ACTIVATED_ADMIN,
            email="admin@activated.example",
            role="Director",
            broadcast_emails=True,
        )
    )
    db.add(
        Contact(
            school_id=PROSPECT_SCHOOL_ID,
            user_id=USER_PROSPECT_ADMIN,
            email="admin@prospect.example",
            role="Director",
            broadcast_emails=True,
        )
    )
    db.add(UserRole(user_id=USER_CUSTOMER_ADMIN, school_id=CUSTOMER_SCHOOL_ID, role="hub_admin"))
    db.add(UserRole(user_id=USER_CUSTOMER_USER, school_id=CUSTOMER_SCHOOL_ID, role="hub_user"))
    db.add(UserRole(user_id=USER_COHORT_ADMIN, school_id=COHORT_CUSTOMER_SCHOOL_ID, role="hub_admin"))
    db.add(UserRole(user_id=USER_ACTIVATED_ADMIN, school_id=ACTIVATED_PROSPECT_SCHOOL_ID, role="hub_admin"))
    db.add(UserRole(user_id=USER_PROSPECT_ADMIN, school_id=PROSPECT_SCHOOL_ID, role="hub_admin"))
    db.commit()


def test_all_customers_opted_in_excludes_non_opted_in_and_prospect_schools(db_session):
    _seed_standard_fixture(db_session)

    contacts = resolve_audience(db_session, [], [], "all", "opted_in")

    emails = {c.email for c in contacts}
    assert emails == {"admin@customer.example", "admin@cohort.example"}


def test_opt_in_filter_all_reaches_non_opted_in_contact(db_session):
    _seed_standard_fixture(db_session)

    contacts = resolve_audience(db_session, [], [], "all", "all")

    emails = {c.email for c in contacts}
    assert "user@customer.example" in emails


def test_opt_in_reads_broadcast_emails_not_auto_emails(db_session):
    """The two opt-ins are independent: a contact who accepted workshop
    automations but declined broadcasts must not receive a broadcast."""
    db_session.add(School(id=CUSTOMER_SCHOOL_ID, name="Customer High", is_current_customer=True))
    db_session.add(
        Contact(
            school_id=CUSTOMER_SCHOOL_ID,
            email="autos-only@customer.example",
            role="Director",
            auto_emails=True,
            broadcast_emails=False,
        )
    )
    db_session.commit()

    assert resolve_audience(db_session, [], [], "all", "opted_in") == []


def test_school_scope_restricted_to_customer_schools_even_for_forged_prospect_id(db_session):
    """Passing a real, existing prospect school_id must still resolve to an
    empty audience — the customer restriction applies unconditionally,
    regardless of caller input."""
    _seed_standard_fixture(db_session)

    contacts = resolve_audience(db_session, [str(PROSPECT_SCHOOL_ID)], [], "all", "all")

    assert contacts == []


def test_src_activated_prospect_is_still_not_emailable(db_session):
    """Activating a prospect's School Resource Center gives them a preview of
    the site, NOT a place on any mailing list — targeting them explicitly (the
    strongest possible request) still resolves to nobody."""
    _seed_standard_fixture(db_session)

    contacts = resolve_audience(
        db_session, [str(ACTIVATED_PROSPECT_SCHOOL_ID)], [], "all", "all"
    )

    assert contacts == []


def test_school_scope_restricts_to_one_school(db_session):
    _seed_standard_fixture(db_session)

    contacts = resolve_audience(db_session, [str(CUSTOMER_SCHOOL_ID)], [], "all", "all")

    emails = {c.email for c in contacts}
    assert emails == {"admin@customer.example", "user@customer.example"}


def test_cohort_scope_expands_to_member_schools(db_session):
    """The cohort holds a customer AND an SRC-activated prospect; expanding it
    reaches only the customer."""
    _seed_standard_fixture(db_session)

    contacts = resolve_audience(db_session, [], [str(COHORT_ID)], "all", "all")

    emails = {c.email for c in contacts}
    assert emails == {"admin@cohort.example"}


def test_school_and_cohort_scopes_union(db_session):
    """A cohort plus an extra school reaches both — the admin adds schools on
    top of a cohort rather than intersecting with it."""
    _seed_standard_fixture(db_session)

    contacts = resolve_audience(
        db_session, [str(CUSTOMER_SCHOOL_ID)], [str(COHORT_ID)], "all", "all"
    )

    emails = {c.email for c in contacts}
    assert emails == {
        "admin@customer.example",
        "user@customer.example",
        "admin@cohort.example",
    }


def test_role_filter_hub_admin_only(db_session):
    _seed_standard_fixture(db_session)

    contacts = resolve_audience(db_session, [], [], "hub_admin", "all")

    emails = {c.email for c in contacts}
    assert emails == {"admin@customer.example", "admin@cohort.example"}


def test_deactivated_and_emailless_contacts_excluded(db_session):
    db_session.add(School(id=CUSTOMER_SCHOOL_ID, name="Customer High", is_current_customer=True))
    db_session.add(
        Contact(
            school_id=CUSTOMER_SCHOOL_ID,
            email="gone@customer.example",
            role="hub_admin",
            broadcast_emails=True,
            deleted_at=datetime.now(UTC),
        )
    )
    db_session.add(
        Contact(school_id=CUSTOMER_SCHOOL_ID, email=None, role="hub_admin", broadcast_emails=True)
    )
    db_session.commit()

    contacts = resolve_audience(db_session, [], [], "all", "all")

    assert contacts == []


def test_malformed_school_id_returns_empty_audience_not_an_error(db_session):
    """A restriction was asked for but nothing parsed — match nothing rather
    than silently widening to every customer school."""
    _seed_standard_fixture(db_session)

    contacts = resolve_audience(db_session, ["not-a-uuid"], [], "all", "all")

    assert contacts == []
