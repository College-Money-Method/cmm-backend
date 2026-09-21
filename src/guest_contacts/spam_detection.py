"""Heuristics that recognise bot submissions of the public contact form.

The form is open to the internet with no CAPTCHA, and the inbox filled up with
two distinct kinds of junk:

1. Form-stuffing bots. Every text field holds a random alphanumeric string
   ("ussppyXbAPxyhvUi"), the message is a bare phone number, and the address is
   a dot-stuffed Gmail alias ("s.a.c.e.w.e.l.o.p73.3@gmail.com").
2. Cold-outreach pitches — SEO services, form-spam tooling — which read like
   prose but carry a URL and marketing vocabulary a parent never uses.

Nothing here rejects a submission. A match sets a flag so the row lands in the
admin Spam tab instead of the inbox, which keeps the cost of a false positive at
"the admin has to look in the other tab" rather than a lost enquiry. Thresholds
are therefore tuned to be quiet on real names in the existing table — "McGuire",
"Dee-Dee", "Baranidharan", "Krzysztof" all pass clean.
"""

from __future__ import annotations

import re

VOWELS = set("aeiouyAEIOUY")

# Reasons a human wrote, not a rule. Both must survive a re-run of the backfill:
# "restored_by_admin" is what distinguishes a row an admin rescued from the spam
# tab from one the guard has simply never looked at, so the two cannot be
# confused and a rescued row can never be re-quarantined behind the admin's back.
ADMIN_MARKED = "marked_by_admin"
ADMIN_RESTORED = "restored_by_admin"
ADMIN_DECISIONS = (ADMIN_MARKED, ADMIN_RESTORED)

# A bare domain counts: the pitches often write "collegemoneymethod.com" with no
# scheme to slip past naive link filters.
_URL_RE = re.compile(r"(https?://|www\.|\b[a-z0-9-]+\.(?:com|net|org|io|co|biz|ru|xyz)\b)", re.I)

# Vocabulary of unsolicited B2B outreach. Absent from a parent asking about
# financial aid, dense in every pitch the inbox has received.
_PITCH_TERMS = (
    "seo",
    "backlink",
    "web design",
    "website design",
    "digital marketing",
    "increase your traffic",
    "rank on google",
    "search engine",
    "guest post",
    "free trial",
    "our platform",
    "our team of experts",
    "crypto",
    "bitcoin",
    "form submissions",
    "lead generation",
    "i recently visited your website",
    "visited your website",
    "loan offer",
)

# How far each signal goes on its own.
_MIN_NAME_LEN_FOR_GIBBERISH = 8
_MAX_INTERNAL_CAPS = 2  # "McGuire" has 1; random strings have many
_MAX_CONSONANT_RUN = 4  # "Krzysztof" peaks at 3 with y as a vowel
_MIN_DOTTED_SEGMENTS = 4  # single-character pieces in a Gmail dot alias


def _longest_consonant_run(word: str) -> int:
    longest = run = 0
    for ch in word:
        if ch.isalpha() and ch not in VOWELS:
            run += 1
            longest = max(longest, run)
        else:
            run = 0
    return longest


def _internal_capitals(word: str) -> int:
    """Capitals that do not open a word part, i.e. not after a space or hyphen."""
    count = 0
    for i, ch in enumerate(word):
        if i == 0 or not ch.isupper():
            continue
        if word[i - 1] in " -'":
            continue
        count += 1
    return count


def looks_like_gibberish(value: str | None) -> bool:
    """True when a value reads as keyboard noise rather than a name or sentence."""
    if not value:
        return False
    text = value.strip()
    if len(text) < _MIN_NAME_LEN_FOR_GIBBERISH or not re.fullmatch(r"[A-Za-z\s\-']+", text):
        return False
    return (
        _internal_capitals(text) > _MAX_INTERNAL_CAPS
        or _longest_consonant_run(text) > _MAX_CONSONANT_RUN
    )


def _is_dot_stuffed_email(email: str) -> bool:
    """Gmail ignores dots, so bots mint unlimited aliases by sprinkling them."""
    local = email.split("@", 1)[0]
    single_char_segments = sum(1 for part in local.split(".") if len(part) == 1)
    return single_char_segments >= _MIN_DOTTED_SEGMENTS


def _is_contentless_message(message: str) -> bool:
    """A message carrying no words — digits only, or one unbroken noise token."""
    stripped = re.sub(r"[^A-Za-z0-9]", "", message)
    if not stripped:
        return True
    if stripped.isdigit():
        return True
    return " " not in message.strip() and looks_like_gibberish(message)


def _is_solicitation(message: str) -> bool:
    """Cold outreach: a link plus sales vocabulary, or sales vocabulary alone."""
    lowered = message.lower()
    hits = sum(1 for term in _PITCH_TERMS if term in lowered)
    if hits >= 2:
        return True
    return hits >= 1 and bool(_URL_RE.search(message))


def detect_spam(
    *,
    first_name: str,
    last_name: str | None,
    email: str,
    school_name: str | None,
    message: str,
    honeypot: str | None,
) -> str | None:
    """Return a short reason when the submission looks automated, else None.

    The reason is stored alongside the row so an admin reviewing the Spam tab can
    see which rule caught it, and so a rule that starts misfiring is easy to spot.
    """
    if honeypot and honeypot.strip():
        # A field hidden from human eyes; only a form-filling script types here.
        return "honeypot"
    if looks_like_gibberish(first_name) or looks_like_gibberish(last_name):
        return "gibberish_name"
    if looks_like_gibberish(school_name):
        return "gibberish_school"
    if _is_contentless_message(message):
        return "contentless_message"
    if _is_dot_stuffed_email(email):
        return "dot_stuffed_email"
    if _is_solicitation(message):
        return "solicitation"
    return None
