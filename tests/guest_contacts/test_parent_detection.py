"""Telling a parent from a counsellor, measured against real inbox traffic.

Every message below is a lightly redacted row from the production
``guest_contacts`` table. The parent cases are what motivated the split; the
professional cases are the ones the rules must never touch, and they are why the
rules key on the possessive rather than on family vocabulary — a counsellor
writes "our families" and "students and their families" all day long, and only a
parent writes "my son".

**If you loosen a rule, add the row that motivated it to the "must not move"
cases below first.** Those are the contract; the misses are not.
"""

from __future__ import annotations

import pytest

from src.guest_contacts.parent_detection import detect_parent


def classify(message, role=None):
    return detect_parent(role=role, message=message)


# ── Families, who belong in the Parents tab ──────────────────────────

@pytest.mark.parametrize(
    "message",
    [
        "Hi, I was on your webinar today. My son is a senior at SSA. Thanks.",
        "Hi, our son just started at WashU and we did not get the aid we expected.",
        "My son currently in 9th grade and I want to know what you do with the school.",
        "I have a unique situation and my son's college counselor suggested I speak with you.",
        "My son Avery - HS Senior. Daughter Tory - HS Junior. What is realistic all-in?",
        "Our daughter is a rising senior at Campbell Hall and we would love a consult.",
        "We need support for the financial aid application for our daughter who is a senior.",
        "Our kid is class of 2028 and we cannot remember how to get back into the portal.",
        "My daughter's college counselor recommended you.",
        "I have a question relating to my step-son, who is entering his senior year.",
    ],
)
def test_a_parent_writing_about_their_own_child(message):
    assert classify(message) == "mentions_own_child"


def test_a_curly_apostrophe_is_still_a_possessive():
    """Mail clients rewrite ' as ’; the rules have to read both."""
    assert classify("my son’s counselor suggested I call") == "mentions_own_child"


@pytest.mark.parametrize(
    "message",
    [
        "I am the father of a rising senior and my wife and I need aid guidance.",
        "I'm a single mom and I need help affording his college list.",
        "As a parent I found your webinar useful — can we talk about our options?",
    ],
)
def test_a_parent_saying_so_outright(message):
    assert classify(message) == "self_described_parent"


@pytest.mark.parametrize(
    "message",
    [
        "Hello - we are an individual family with a senior attending Berkeley High.",
        "Our family met with Noelle at Fieldston and she suggested we reach out.",
        "My husband's work situation changed and we need help with the forms.",
    ],
)
def test_a_household_rather_than_an_institution(message):
    assert classify(message) == "writes_as_a_family"


def test_the_spouse_phrasing_is_the_weakest_rule_and_can_overreach():
    """A documented limit, not an endorsement.

    "my husband"/"my wife" earns its place on one live row and fires last, only
    for messages carrying no other signal. A business co-founder writing "my
    wife and I run this together" would be misfiled. Tighten this rule if that
    turns up in real traffic — and add the row here when it does.
    """
    assert classify("I am a consultant and my wife and I run this together.") == (
        "writes_as_a_family"
    )


def test_a_stated_role_settles_it_without_reading_the_message():
    """Some of the form's history carried a role field; when it is filled in it
    is better evidence than anything the prose can offer."""
    assert classify("can you help me figure out how to pay for college?", role="parent") == (
        "role_says_parent"
    )
    assert classify("khgkjh", role="parent") == "role_says_parent"


# ── Schools and counsellors, who must stay in the inbox ──────────────

@pytest.mark.parametrize(
    "message",
    [
        # The plural is the tell: a professional serves families, a parent is one.
        "I would like to learn more about how you might work with our families.",
        "I'm interested in this for students and their families in our school community.",
        "I would love to partner with you to support our families with financial aid.",
        "I am now the director of college counseling at Brewster Academy in New Hampshire.",
        "I am reaching out to see if it is too late to work with you for the fall.",
        "I enjoyed your presentation at the ACCIS conference and would love to connect.",
        "I learned about your company through the Archer School and think you could help "
        "my community at Country Day.",
        "I'm the founder of a future-readiness platform for high school student-athletes.",
        "I'm exploring our financial aid programming and would love to learn about yours.",
    ],
)
def test_a_professional_enquiry_is_left_in_the_inbox(message):
    assert classify(message) is None


@pytest.mark.parametrize(
    "message",
    [
        # Someone else's child, described by the professional who serves them.
        # The second possessive is the tell, and reading past it would misfile
        # exactly the senders this form exists for.
        "My student's son is applying to college this year.",
        "Our school's children would benefit from this program.",
        "On behalf of my client's kids, I am reaching out.",
        "My colleague's daughter needs advice on financial aid.",
        # A vendor's client list, not a household.
        "We would love to welcome your school into our family of partner institutions.",
    ],
)
def test_someone_elses_child_does_not_make_the_sender_a_parent(message):
    assert classify(message) is None


def test_a_school_job_named_after_families_is_still_a_school_job():
    assert classify("I would like to discuss your sessions.", role="Parent Liaison") is None


def test_a_parent_who_counts_their_household_still_reads_as_one():
    """The same two words as the vendor phrasing above, and the opposite sender."""
    assert classify("We are our family of four applying this year.") == "writes_as_a_family"


def test_a_counsellor_who_mentions_their_own_child_is_still_a_counsellor():
    """A stated professional role outranks anything inferred from the prose."""
    assert classify("My daughter is a senior, and separately I run counselling here.",
                    role="college counselor") is None


def test_an_empty_or_neutral_message_moves_nothing():
    assert classify("Attending your webinar") is None
    assert classify("I'm curious as to whether you work with individual families.") is None
