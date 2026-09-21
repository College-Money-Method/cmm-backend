"""The contact-form spam heuristics, measured against real inbox traffic.

Every sample below is a lightly redacted row from the production
``guest_contacts`` table. The spam cases are what motivated the guard; the
legitimate cases are the ones it must never touch, and they are the reason the
thresholds sit where they do — "McGuire" has an internal capital, "Krzysztof"
has a long consonant run, and a parent may well name a college's website.
"""

from __future__ import annotations

import pytest

from src.guest_contacts.spam_detection import detect_spam


def classify(
    first_name="Jane",
    last_name="Doe",
    email="jane@example.com",
    school_name="Lincoln High School",
    message="Hello Paul, I would like help with financial aid for my senior.",
    honeypot=None,
):
    return detect_spam(
        first_name=first_name,
        last_name=last_name,
        email=email,
        school_name=school_name,
        message=message,
        honeypot=honeypot,
    )


# ── Bot traffic ──────────────────────────────────────────────────────

def test_honeypot_beats_every_other_signal():
    """A filled hidden field settles it before any content heuristic runs."""
    assert classify(honeypot="https://example.com") == "honeypot"


def test_blank_honeypot_is_what_a_real_browser_sends():
    assert classify(honeypot="") is None
    assert classify(honeypot="   ") is None


@pytest.mark.parametrize(
    "first,last",
    [
        ("ussppyXbAPxyhvUi", "BDJZjHHdCzIsbpyEIDpPMZsn"),
        ("QaqPSOfqViTTdcbrlpbg", "nmEgxjGDIfLabyfpFBOvqv"),
        ("DaMWbvFFqnwCeYspjNkI", "XyRTvLfrEdNXyroKU"),
        ("hGNLfqHBCYlQzaxRbULY", "CPDIVagjfDoQfxaidsNYmcVr"),
    ],
)
def test_random_string_names_are_caught(first, last):
    assert classify(first_name=first, last_name=last) == "gibberish_name"


def test_message_that_is_only_a_phone_number():
    assert classify(message="7452959171") == "contentless_message"


def test_message_that_is_one_unbroken_noise_token():
    assert classify(message="MtIskmTrgXREcsmJCkrQEBNd") == "contentless_message"


def test_dot_stuffed_gmail_alias():
    """Gmail ignores dots, so bots mint endless addresses from one mailbox."""
    assert classify(email="s.a.c.e.w.e.l.o.p73.3@gmail.com") == "dot_stuffed_email"


def test_seo_pitch_with_a_link():
    assert (
        classify(
            first_name="majid",
            last_name="jamil",
            school_name="zoulex",
            message=(
                "Hi, I hope you're doing well. I recently visited your website and really "
                "liked your content collegemoneymethod.com. It could rank on Google with "
                "some SEO work."
            ),
        )
        == "solicitation"
    )


def test_form_spam_tooling_pitch():
    assert (
        classify(
            first_name="Julia",
            last_name="Miller",
            school_name="TurboJot",
            message=(
                "Have you tried TurboJot yet? It's a new platform that automates website "
                "form submissions across thousands of sites. Start your free trial today."
            ),
        )
        == "solicitation"
    )


# ── Real enquiries that must reach the inbox ─────────────────────────

@pytest.mark.parametrize(
    "first,last",
    [
        ("Susan", "McGuire"),          # internal capital
        ("Dee-Dee", "Sanders"),        # capital after a hyphen
        ("Baranidharan", "Chinnasamy"),  # long, unfamiliar, perfectly real
        ("Krzysztof", "Wojciechowski"),  # long consonant runs
        ("RENZO", "MAZZINI"),          # typed with caps lock on
        ("Rivka", "Geoghegan"),
        ("Nihal", "O'Brien-Smith"),
    ],
)
def test_real_names_are_left_alone(first, last):
    assert classify(first_name=first, last_name=last) is None


def test_ordinary_enquiry_passes():
    assert (
        classify(
            first_name="Patricia",
            last_name="Davico",
            school_name="St. Ignatius College Prep/CCSF",
            message=(
                "We have a student at CCSF and a junior. One is applying to 5 year "
                "B.Architecture programs to start next fall and we need guidance on aid."
            ),
        )
        is None
    )


def test_short_but_human_message_passes():
    assert classify(message="hey paul - can you help me figure out how to pay for college?") is None


def test_parent_naming_a_college_site_is_not_a_solicitation():
    """A bare domain alone is not a pitch — sales vocabulary has to come with it."""
    assert (
        classify(message="I read the aid page at washu.edu and I am confused by the CSS Profile.")
        is None
    )


def test_one_dot_in_the_address_is_just_a_name():
    assert classify(email="leslie.sanderfur@mybga.org") is None
    assert classify(email="alex.catalan@7hills.org") is None
