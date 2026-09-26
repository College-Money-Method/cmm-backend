"""Heuristics that recognise a parent writing in through the partner-school form.

The form is meant for counsellors, schools and businesses, but families find it
anyway and most of what arrives is a parent asking about aid for their own
child. Those enquiries are welcome — they are simply a different conversation
from "can you run a session for our school", and mixing the two makes the inbox
hard to work through.

So this is routing, not rejection, and it is independent of ``spam_detection``:
a flagged row moves to the Parents tab, keeps every control the inbox has, and
an admin can move it back. Nothing is hidden and nothing is dropped.

Precision matters more than recall here. A counsellor says "our families" and
"my students"; a parent says "my son". The rules below only fire on the second
kind of phrase, so a missed parent sits in the inbox looking exactly as it does
today, which is the failure everyone already knows how to handle.
"""

from __future__ import annotations

import re

# The same two sentinels the spam guard uses. A human decision reads the same
# whichever tab it was made in, and both classifications need it for the same
# reason: a backfill must never overturn a call an admin already made.
from src.guest_contacts.spam_detection import ADMIN_DECISIONS, ADMIN_MARKED, ADMIN_RESTORED

__all__ = ["ADMIN_DECISIONS", "ADMIN_MARKED", "ADMIN_RESTORED", "detect_parent"]

# The form has carried an optional role field at times; when it is filled in it
# settles the question outright, in both directions.
_PARENT_ROLES = ("parent", "mother", "father", "mom", "dad", "guardian", "family")
_PROFESSIONAL_ROLES = (
    "counselor",
    "counsellor",
    "advisor",
    "adviser",
    "director",
    "teacher",
    "educator",
    "administrator",
    "principal",
    "school",
    "staff",
    "consultant",
    "business",
    "partner",
    # A school job can be named after the people it serves — "Parent Liaison",
    # "Director of Parent Engagement" — so these have to be read before the
    # parent words below, or the title files the staffer with the families.
    "liaison",
    "coordinator",
)

# "my son", "our daughter", "my youngest kid" — a possessive followed closely by
# a word for one's own child. The possessive is what does the work: a counsellor
# writes "our families" and "students and their families", never "my daughter".
_CHILD = (
    r"(?:son|daughter|child|children|kid|kids|twins|"
    r"step[\s-]?son|step[\s-]?daughter|grand[\s-]?son|grand[\s-]?daughter)"
)
# The words allowed between the possessive and the child let "my youngest son"
# through while keeping the child the sender's own. They carry no apostrophe on
# purpose: "my student's son" and "our school's children" are someone else's
# child described by a professional, and reading the chain as far as a second
# possessive is what would misfile the counsellors this form exists for.
_OWN_CHILD_RE = re.compile(rf"\b(?:my|our)\s+(?:[\w-]+\s+){{0,2}}?{_CHILD}\b", re.I)

# The other way families introduce themselves: by saying so. "I'm a single mom",
# "I am the father of a rising senior", "as a parent".
_SELF_DESCRIBED_RE = re.compile(
    r"\b(?:i am|i'm|i’m)\s+(?:an?\s+|the\s+)?(?:single\s+)?"
    r"(?:mom|mother|dad|father|parent|grandparent|guardian)\b"
    r"|\bas a parent\b"
    r"|\b(?:the|a)\s+(?:father|mother|parent)s?\s+of\s+(?:a|an|two|three|our|my)\b",
    re.I,
)

# Weaker, and deliberately kept apart so a misfire is traceable to it: writing as
# a household rather than an institution. Singular "family" is the whole trick —
# a counsellor offers to help "our families", a parent says "our family". The
# spouse phrasing shows up in the same enquiries, always about household money.
# "our family of partner schools" is a vendor describing its client list, not a
# household; a parent who counts says "our family of four".
_FAMILY_VOICE_RE = re.compile(
    r"\bour family\b(?!\s+of\s+(?!\d|two|three|four|five|six)\w)"
    r"|\b(?:we are|we're|i am|i'm|i’m)\s+(?:an?\s+)?(?:individual\s+)?family\b"
    r"|\bmy (?:husband|wife)\b",
    re.I,
)


def _role_contains(role: str | None, words: tuple[str, ...]) -> bool:
    lowered = (role or "").lower()
    return any(word in lowered for word in words)


def detect_parent(*, role: str | None, message: str) -> str | None:
    """Return a short reason when the sender is writing as a parent, else None.

    The reason is stored on the row so the Parents tab can say why it put a
    submission there, which is what makes a misfiring rule easy to spot.
    """
    # A stated role beats anything inferred from prose. A counsellor who happens
    # to mention their own child is still writing to us as a counsellor.
    if _role_contains(role, _PROFESSIONAL_ROLES):
        return None
    if _role_contains(role, _PARENT_ROLES):
        return "role_says_parent"
    if _OWN_CHILD_RE.search(message):
        return "mentions_own_child"
    if _SELF_DESCRIBED_RE.search(message):
        return "self_described_parent"
    if _FAMILY_VOICE_RE.search(message):
        return "writes_as_a_family"
    return None
