"""Topic tags for assistant questions, so admins can see what users ask about.

Each user question gets exactly one tag, written by Haiku in the background
after the question is saved (the chat never waits for it). A short follow-up
("yes, draft that") is classified with the previous question as context, so it
inherits that topic. A failed call leaves the tag null; the admin backfill
(``tag_untagged``) picks those up later. The list lives here, not in the
database, and is served to the admin UI, so renaming a label is a code change
only. Removing a slug leaves old rows with it; they show as "Other".
"""

from __future__ import annotations

import logging
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.assistant.models import AssistantMessage
from src.config import settings
from src.db.base import get_session_factory
from src.video_pipeline import bedrock_usage
from src.video_pipeline.bedrock_client import BedrockCallError, call_json

logger = logging.getLogger(__name__)

OTHER = "other"
BACKFILL_BATCH = 50
_BACKFILL_WORKERS = 8
_QUESTION_CHARS = 1500
_CONTEXT_CHARS = 400


@dataclass(frozen=True)
class Topic:
    slug: str
    label: str
    group: str  # "Program & Hub" | "Aid topics" | "Other"
    description: str


PROGRAM, AID = "Program & Hub", "Aid topics"

TOPICS: tuple[Topic, ...] = (
    Topic("workshop_registrations", "Workshop registrations & attendance", PROGRAM,
          "How many families registered for or attended a workshop, who signed up, live vs replay views."),
    Topic("workshop_schedule", "Upcoming workshops & schedule", PROGRAM,
          "Which workshops are next, dates, how many are left, which grades a workshop targets."),
    Topic("workshop_recap", "Workshop recaps & replays", PROGRAM,
          "Summaries of a past session, key takeaways, replay links for families who missed it."),
    Topic("program_engagement", "Program engagement & reach", PROGRAM,
          "How the school's program is going overall: families reached, Resource Center use, trends."),
    Topic("family_outreach_drafts", "Emails & messages to families", PROGRAM,
          "Drafting or editing an email, text, newsletter blurb or announcement to send families."),
    Topic("boosting_participation", "Boosting sign-ups & engagement", PROGRAM,
          "Ideas to raise registration or attendance, taglines, hooks, outreach strategy."),
    Topic("grade_checklists", "Grade-level checklists & timelines", PROGRAM,
          "What families of a grade should do and when; checklists for 9th-12th grade or a season."),
    Topic("hub_how_to", "Using the Hub & Resource Center", PROGRAM,
          "Where to find something in the Counselor Hub or Resource Center, templates, how a feature works."),
    Topic("fafsa_filing", "FAFSA filing & deadlines", AID,
          "Where and when to file the FAFSA, which forms to submit, FSA ID, required documents."),
    Topic("fafsa_corrections_verification", "FAFSA corrections & verification", AID,
          "Fixing a FAFSA mistake, verification or identity/fraud selection, missing assets, next steps."),
    Topic("css_profile", "CSS Profile", AID,
          "CSS Profile questions: which schools use it, how it differs from the FAFSA, its fees and waivers."),
    Topic("divorced_separated_parents", "Divorced, separated & non-custodial parents", AID,
          "Which parent files, whose income counts, non-custodial parent waivers, absent parents."),
    Topic("appeals_special_circumstances", "Appeals & special circumstances", AID,
          "Award appeals, professional judgment, unusual income or benefits, supporting letters."),
    Topic("aid_eligibility", "Who qualifies for aid", AID,
          "Whether a family (often higher income) should apply or could get need-based aid."),
    Topic("net_price_affordability", "Net price & affordability", AID,
          "Net price calculators, SAI, estimating cost or aid at a college, affordable college options."),
    Topic("merit_aid_college_lists", "Merit aid & college lists", AID,
          "Which colleges give generous merit aid, building a list for merit, finding merit scholarships."),
    Topic("college_specific_aid_data", "A college's aid data & policies", AID,
          "Facts about one named college: discount rate, % getting aid, leveraging, its aid policy."),
    Topic("outside_state_scholarships", "Outside & state scholarships", AID,
          "Private or local scholarships and state programs (e.g. Bright Futures, Cal Grant)."),
    Topic("international_students", "International & undocumented students", AID,
          "Aid for international, undocumented or DACA students who cannot get federal aid."),
    Topic("loans_529_paying", "Loans, 529 plans & paying", AID,
          "Student and Parent PLUS loans, 529 or prepaid plans, payment plans, budgeting to pay."),
    Topic(OTHER, "Other", "Other",
          "Greetings, thanks, off-topic or unclear questions, anything that fits no topic above."),
)
TOPIC_SLUGS = frozenset(t.slug for t in TOPICS)

_SYSTEM = (
    "You sort questions sent to a college-financing assistant used by high school counselors "
    "(and some families) into exactly one topic.\n\nTopics:\n"
    + "\n".join(f"- {t.slug}: {t.description}" for t in TOPICS)
    + "\n\nPick the single best topic for the question. If the question is a short follow-up "
    '("yes", "draft that", "for juniors?"), use the previous question to decide. Use "other" '
    'only when nothing fits.\n\nReply with JSON only: {"topic": "<slug>"}'
)

# Tagging runs here so the chat request never waits on it.
_TAGGER = ThreadPoolExecutor(max_workers=2, thread_name_prefix="assistant-topic-tag")


def classify_question(question: str, previous_question: str | None = None) -> str | None:
    """The topic slug for ``question``, or None if the call failed (retry later)."""
    content = f"Question: {question[:_QUESTION_CHARS]}"
    if previous_question:
        content = f"Previous question: {previous_question[:_CONTEXT_CHARS]}\n{content}"
    try:
        parsed, _, _ = call_json(
            system=_SYSTEM,
            content=content,
            invoke_type=bedrock_usage.ASSISTANT_TOPIC_TAG,
            max_tokens=30,
            model_id=settings.assistant_enrich_model_id,
        )
    except BedrockCallError as exc:
        logger.warning("Assistant topic tagging failed: %s", exc)
        return None
    topic = parsed.get("topic")
    return topic if topic in TOPIC_SLUGS else OTHER


def _previous_question(db: Session, message: AssistantMessage) -> str | None:
    return db.scalar(
        select(AssistantMessage.content)
        .where(
            AssistantMessage.conversation_id == message.conversation_id,
            AssistantMessage.role == "user",
            AssistantMessage.created_at < message.created_at,
        )
        .order_by(AssistantMessage.created_at.desc())
        .limit(1)
    )


def tag_message(db: Session, message: AssistantMessage) -> bool:
    """Classify one saved question and store its tag; False if the call failed."""
    topic = classify_question(message.content, _previous_question(db, message))
    if topic is None:
        return False
    message.topic_tag = topic
    db.commit()
    return True


def _tag_by_id(message_id: uuid.UUID) -> None:
    try:
        with get_session_factory()() as db:
            message = db.get(AssistantMessage, message_id)
            if message is not None and message.role == "user" and message.topic_tag is None:
                tag_message(db, message)
    except Exception:  # noqa: BLE001 - background bookkeeping must never surface
        logger.exception("Assistant topic tagging crashed for message %s", message_id)


def tag_question_soon(message_id: uuid.UUID) -> None:
    """Tag a just-saved question in the background."""
    _TAGGER.submit(_tag_by_id, message_id)


def tag_untagged(db: Session, limit: int = BACKFILL_BATCH) -> tuple[int, int]:
    """Tag up to ``limit`` untagged questions, oldest first; returns (tagged, failed).

    The model calls run side by side (they need no session), so one admin click
    stays well inside a request timeout.
    """
    pending = db.scalars(
        select(AssistantMessage)
        .where(AssistantMessage.role == "user", AssistantMessage.topic_tag.is_(None))
        .order_by(AssistantMessage.created_at)
        .limit(limit)
    ).all()
    previous = [_previous_question(db, m) for m in pending]
    with ThreadPoolExecutor(max_workers=_BACKFILL_WORKERS) as pool:
        topics = list(pool.map(classify_question, [m.content for m in pending], previous))
    tagged = 0
    for message, topic in zip(pending, topics, strict=True):
        if topic is not None:
            message.topic_tag = topic
            tagged += 1
    db.commit()
    return tagged, len(pending) - tagged
