"""Tell genuine questions apart from the chatter that arrives in the same panel.

A webinar's Q&A box is not only questions. It collects "Thank you!", jokes,
and warm multi-paragraph notes from counsellors at other schools. None of those
need an answer, and none of them should be dropped either — the raw stream says
something about the audience, and an admin may disagree with any given verdict.
So everything is labelled and nothing is deleted; hiding is a query concern.

**Noise is long-form**, which is the one counter-intuitive thing here. On the
webinar this was measured against, the two noise rows were a Marvin the Martian
joke and a signed greeting from a colleague — and the cheap regex pass caught
*neither*, classifying 0 of 25. The model did all of the work. The rule pass is
kept because it is free and does short-circuit a bare "Thanks!", but it must not
be mistaken for the mechanism.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from src.config import settings
from src.video_pipeline import bedrock_usage
from src.video_pipeline.bedrock_client import BedrockCallError, call_json
from src.workshops.qa_models import CLASSIFICATIONS, WebinarQaQuestion

logger = logging.getLogger(__name__)

# Whole-string matches only. Anything with a question mark, or any content past
# the pleasantry, falls through to the model — "Hi! Quick question ..." is a
# question, and an anchored pattern is what keeps it one.
_BARE_THANKS = re.compile(r"^(thank(s| you)|ty|thx)[\s!.,]*$", re.IGNORECASE)
_BARE_GREETING = re.compile(
    r"^(hi|hey|hello|good (morning|afternoon|evening))[\s!.,]*(everyone|all|there)?[\s!.,]*$",
    re.IGNORECASE,
)

NOISE_PROMPT_VERSION = "v1"

# Measured wording — it produced 2/2 noise caught with 0 false positives across
# 23 genuine questions. The sentence about length is load-bearing: without it the
# colleague's long greeting reads as a question. Changing any of this means
# re-measuring, so bump NOISE_PROMPT_VERSION alongside.
NOISE_SYS = (
    "Classify each webinar Q&A submission. Labels: 'question' (a genuine request for "
    "information the panel should answer), 'greeting', 'thanks', 'comment' (a remark, "
    "joke, or statement needing no answer), 'spam'. Long text is not automatically a "
    "question — a friendly note from a colleague or a joke is still noise.\n"
    'Reply ONLY with JSON: {"results":[{"i":<index>,"label":"<label>"}]}'
)


# The reply carries one JSON object per submission, so the output cap — not the
# input — is what a big webinar hits. Sending a whole webinar in one call was
# fine at 25 submissions and silently broke at 175: the JSON truncated mid-object,
# failed to parse, and left every row on that webinar unlabelled. Chunking keeps
# each reply far inside the cap and bounds the damage of a bad call to one chunk.
CHUNK_SIZE = 50
MAX_TOKENS = 2048


def _rule_label(text: str) -> str | None:
    stripped = text.strip()
    if _BARE_THANKS.match(stripped):
        return "thanks"
    if _BARE_GREETING.match(stripped):
        return "greeting"
    return None


def _label_chunk(chunk: list[WebinarQaQuestion], now: datetime) -> int:
    """Label one chunk from the model. Returns how many rows it set.

    Raises nothing: a chunk the model fails on is a gap of at most CHUNK_SIZE
    rows, and the chunks after it are independent and still worth asking for.
    """
    listing = "\n".join(f"{i}. {q.question_text}" for i, q in enumerate(chunk))
    try:
        parsed, input_tokens, output_tokens = call_json(
            system=NOISE_SYS,
            content=listing,
            max_tokens=MAX_TOKENS,
            invoke_type=bedrock_usage.QA_CLASSIFICATION,
        )
    except BedrockCallError as exc:
        logger.warning("Q&A classification chunk failed (%d rows left unlabelled): %s", len(chunk), exc)
        return 0

    by_index: dict[int, str] = {}
    for result in parsed.get("results") or []:
        try:
            by_index[int(result.get("i"))] = str(result.get("label") or "").strip().lower()
        except (TypeError, ValueError):
            continue

    labelled = 0
    for i, question in enumerate(chunk):
        label = by_index.get(i)
        # An unknown label is dropped rather than stored: a typo'd verdict in
        # the classification column would quietly filter a real question out
        # of the admin's default view.
        if label not in CLASSIFICATIONS:
            continue
        question.classification = label
        question.classified_by = "llm"
        question.classified_at = now
        labelled += 1

    logger.info(
        "Q&A classification — model=%s submissions=%d labelled=%d tokens_in=%d tokens_out=%d",
        settings.bedrock_haiku_model_id,
        len(chunk),
        labelled,
        input_tokens,
        output_tokens,
    )
    return labelled


def classify_questions(db: Session, questions: list[WebinarQaQuestion]) -> int:
    """Label each submission as a question or as noise. Returns the count labelled.

    Commits. Never raises: a Bedrock outage leaves ``classification`` null, which
    the API treats as "unclassified" and shows rather than hides. Losing the
    labels is a degraded view; losing the sync that carries the questions is not
    acceptable, and this is called from inside it.
    """
    pending = [q for q in questions if q.classification is None]
    if not pending:
        return 0

    now = datetime.now(tz=timezone.utc)
    needs_model: list[WebinarQaQuestion] = []
    labelled = 0

    for question in pending:
        label = _rule_label(question.question_text)
        if label:
            question.classification = label
            question.classified_by = "rule"
            question.classified_at = now
            labelled += 1
        else:
            needs_model.append(question)

    for start in range(0, len(needs_model), CHUNK_SIZE):
        labelled += _label_chunk(needs_model[start : start + CHUNK_SIZE], now)

    db.commit()
    logger.info("Q&A classified — labelled=%d of %d pending", labelled, len(pending))
    return labelled
