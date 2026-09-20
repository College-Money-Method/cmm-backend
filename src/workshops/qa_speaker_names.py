"""Who a spoken answer gets attributed to.

Separate from the matching rules because naming a speaker and locating an answer
fail in completely different ways. Matching is wrong when it points at the wrong
minute of the recording; naming is wrong when it puts words in someone's mouth —
and that second one is the failure worth being careful about, because a
plausible name is indistinguishable from a correct one to anyone reading the
row later.

Two sources keep it honest. Zoom records the panelist's name against every
question they typed an answer to, which makes the typed answers a correctly
spelled register of who was on a session — written down, not heard. And a short
table of spellings a caption engine has actually produced for the host covers
what the register cannot, because the host answers aloud and so rarely appears
in it.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.workshops.qa_models import WebinarQaQuestion

# Who a spoken answer is attributed to when the transcript does not say.
# These sessions are hosted by one person, and a guest answering is the
# exception rather than the rule. A caption track carries no speaker labels at
# all, so the model is reading the name out of what is said — an introduction, a
# moderator handing over — and on a stretch where nobody is named it comes back
# empty. Empty is the wrong answer here: it renders as a spoken answer nobody
# said. A name the model does find is kept, which is how the genuine guest
# answers survive this.
DEFAULT_SPEAKER = "Paul Martin"

# Spellings a caption engine has actually produced for the host, keyed by
# `speaker_key`. Every entry is a name observed in stored extractions, not a
# pattern: "Paul Merlin" is what the captions make of "Paul Martin", and "Paul"
# is the transcript using his first name alone.
#
# Matching is exact on the normalised key and deliberately NOT fuzzy. Edit
# distance would fold a real person whose name sits one letter away — a Haul
# Martin, a Paula Martin — into the host, silently and with no way for anyone
# reading the row to tell. A misspelling left alone is a cosmetic wart; a
# misattributed answer is a false record of who said what. So a name that is not
# literally one of these is kept exactly as it came in.
SPEAKER_ALIASES = {
    "paul": DEFAULT_SPEAKER,
    "paul merlin": DEFAULT_SPEAKER,
    "paul martin college money method": DEFAULT_SPEAKER,
}


def speaker_key(name: str) -> str:
    """Case, punctuation and spacing dropped — what two spellings of one name share."""
    return re.sub(r"[^a-z0-9]+", " ", name.casefold()).strip()


def canonical_speaker(raw: object, roster: Sequence[str] = ()) -> str:
    """The name to store for a spoken answer.

    The roster is how the people on the webinar spell themselves, so a roster
    spelling wins over whatever the transcript sounded out. Past that, only the
    exact aliases above are rewritten, and an unrecognised name is stored as
    given: this corrects transcription, it does not guess at identity.
    """
    name = str(raw or "").strip()
    if not name:
        return DEFAULT_SPEAKER
    key = speaker_key(name)
    for known in roster:
        if speaker_key(known) == key:
            return known
    return SPEAKER_ALIASES.get(key, name)


def speaker_roster(db: Session, webinar_id: uuid.UUID) -> list[str]:
    """The people on this webinar, spelled the way they spell it.

    Handed to the model so it never has to spell a name from how the audio
    sounds — which is what turned "Paul Martin" into "Paul Merlin". The host
    leads every session but rarely types an answer, so he is added whether or
    not Zoom has him on this one.
    """
    names = db.scalars(
        select(WebinarQaQuestion.responder_name)
        .where(
            WebinarQaQuestion.webinar_id == webinar_id,
            WebinarQaQuestion.responder_name.is_not(None),
        )
        .distinct()
    ).all()
    roster = [DEFAULT_SPEAKER]
    roster.extend(sorted({n.strip() for n in names if n and n.strip()} - {DEFAULT_SPEAKER}))
    return roster
