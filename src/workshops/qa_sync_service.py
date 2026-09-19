"""Q&A sync service — pulls the Zoom Q&A report and upserts it into our tables.

Shaped like ``attendance_sync_service``: a sync function taking the session,
committing itself, returning a bool the webhook retry ladder reads as "done" or
"come back later".

The one rule that is not obvious from the code: **a sync writes ingested fact
only.** Every column an admin can edit is untouched here, on every path. An
admin who corrects an answer must still see their correction after the next
re-sync, so the update below names its columns explicitly rather than looping
over a dict of everything Zoom sent.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.integrations import zoom as zoom_client
from src.workshops.models import Webinar, WorkshopRegistration
from src.workshops.qa_classification_service import classify_questions
from src.workshops.qa_models import WebinarQaQuestion, WebinarQaSync

logger = logging.getLogger(__name__)

# Zoom's sentinel in the `answer` field for a question a panelist answered out
# loud. There is no answer text anywhere in the report for these — recovering it
# from the recording's transcript is what `qa_extraction_service` exists for.
_LIVE_ANSWERED = "live answered"

# Zoom fills `name`, `email` AND `user_id` with this literal string for the
# group holding every anonymous submission. Left as-is it would end up stored as
# an email address and offered to the registration matcher, so it is stripped to
# NULL and recorded as a flag instead.
_ANONYMOUS = "anonymous"

_VISIBILITY_BY_ANSWER_TYPE = {
    "host_answered_publicly": "public",
    "host_answered_privately": "private",
}


def _parse_time(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None


def _clean(value: str | None) -> str | None:
    """Trim, and turn Zoom's ``"anonymous"`` placeholder into a real NULL."""
    text = (value or "").strip()
    if not text or text.lower() == _ANONYMOUS:
        return None
    return text


def sync_webinar_qa(zoom_webinar_id: str, db: Session) -> bool:
    """Fetch the Zoom Q&A report for one webinar and upsert its questions.

    Returns True when the report was fetched and written, False when it is not
    available yet (the caller retries) or the webinar is unknown to us.
    """
    zoom_webinar_id = (zoom_webinar_id or "").replace(" ", "").strip()

    webinar = db.scalars(
        select(Webinar).where(Webinar.zoom_webinar_id == zoom_webinar_id)
    ).first()
    if not webinar:
        logger.warning("Q&A sync skipped — no DB record for zoom_webinar_id=%s", zoom_webinar_id)
        return False

    report = zoom_client.get_webinar_qa(zoom_webinar_id)
    if report is None:
        # Recorded, not just logged: without a row there is no way to tell a
        # webinar nobody asked anything at from one whose report never arrived.
        db.add(
            WebinarQaSync(
                webinar_id=webinar.id,
                zoom_webinar_id=zoom_webinar_id,
                status="failed",
                error_text="Zoom Q&A report unavailable",
            )
        )
        db.commit()
        logger.warning("Q&A sync — report unavailable for webinar=%s", zoom_webinar_id)
        return False

    groups = report.get("questions") or []
    details = [
        (group, detail)
        for group in groups
        for detail in (group.get("question_details") or [])
    ]

    sync = WebinarQaSync(
        webinar_id=webinar.id,
        zoom_webinar_id=zoom_webinar_id,
        zoom_webinar_uuid=str(report.get("uuid") or "") or None,
        webinar_start_time=_parse_time(report.get("start_time")),
        raw_payload=report,
        question_count=len(details),
        status="ok",
    )
    db.add(sync)
    db.flush()

    existing = {
        q.zoom_question_id: q
        for q in db.scalars(
            select(WebinarQaQuestion).where(WebinarQaQuestion.webinar_id == webinar.id)
        ).all()
    }
    registrations_by_email = {
        r.email.lower(): r
        for r in db.scalars(
            select(WorkshopRegistration).where(WorkshopRegistration.webinar_id == webinar.id)
        ).all()
        if r.email
    }

    created = updated = skipped = 0
    for group, detail in details:
        zoom_question_id = str(detail.get("question_id") or "").strip()
        question_text = (detail.get("question") or "").strip()
        if not zoom_question_id or not question_text:
            # Neither is optional in practice; a row missing one cannot be kept
            # idempotently or shown to anyone, so it is dropped rather than
            # invented around.
            skipped += 1
            continue

        asker_email = _clean(group.get("email"))
        answer_details = detail.get("answer_details") or []
        first_answer = answer_details[0] if answer_details else {}
        raw_answer = (detail.get("answer") or "").strip()

        if raw_answer.lower() == _LIVE_ANSWERED:
            answer_source, typed_answer_text = "live", None
        elif raw_answer:
            answer_source, typed_answer_text = "typed", raw_answer
        else:
            answer_source, typed_answer_text = "unanswered", None

        fields = {
            "sync_id": sync.id,
            "webinar_id": webinar.id,
            "question_text": question_text,
            "asked_at": _parse_time(detail.get("create_time")),
            "question_status": (detail.get("question_status") or "").strip() or None,
            "content_hash": hashlib.sha256(question_text.encode()).hexdigest(),
            "asker_name": _clean(group.get("name")),
            "asker_email": asker_email,
            "asker_zoom_user_id": _clean(group.get("user_id")),
            "is_anonymous": (group.get("name") or "").strip().lower() == _ANONYMOUS,
            "registration_id": (
                registrations_by_email[asker_email.lower()].id
                if asker_email and asker_email.lower() in registrations_by_email
                else None
            ),
            "answer_source": answer_source,
            "typed_answer_text": typed_answer_text,
            "answer_visibility": _VISIBILITY_BY_ANSWER_TYPE.get(first_answer.get("type")),
            "marked_answered_at": _parse_time(first_answer.get("create_time")),
            # Who marked the question answered in Zoom. For a live answer that
            # is the panelist who ticked it off, which is often NOT the person
            # who spoke — the speaker comes from the transcript extraction.
            "responder_name": _clean(first_answer.get("name")),
            "responder_email": _clean(first_answer.get("email")),
        }

        row = existing.get(zoom_question_id)
        if row is None:
            db.add(WebinarQaQuestion(zoom_question_id=zoom_question_id, **fields))
            created += 1
        else:
            # Ingested fact only. `answer_text_override`, `classification_override`,
            # `is_hidden` and the `edited_*` columns are deliberately absent from
            # `fields` — a re-sync must never undo an admin's edit.
            for column, value in fields.items():
                setattr(row, column, value)
            updated += 1

    db.commit()
    logger.info(
        "Q&A synced — webinar=%s questions=%d created=%d updated=%d skipped=%d",
        zoom_webinar_id,
        len(details),
        created,
        updated,
        skipped,
    )

    # Labelling is a nicety on top of the sync, not part of it. It reaches
    # Bedrock, so it is the part most likely to be unavailable, and an
    # unlabelled question still shows up for the admin — losing the questions
    # themselves over it would not be a fair trade.
    try:
        classify_questions(
            db,
            db.scalars(
                select(WebinarQaQuestion).where(WebinarQaQuestion.webinar_id == webinar.id)
            ).all(),
        )
    except Exception as exc:
        db.rollback()
        logger.warning("Q&A classification failed — webinar=%s error=%s", zoom_webinar_id, exc)

    return True
