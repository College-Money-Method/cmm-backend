"""AI pre-screen background task for counselor resource submissions.

Called via FastAPI BackgroundTasks when a submission is submitted for review.
Degrades gracefully if OPENAI_API_KEY is missing or the openai package is absent.
"""

from __future__ import annotations

import json
import logging
import os
import uuid

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

_REVIEW_PROMPT_TEMPLATE = """You are reviewing a resource submission for a college financial aid counseling platform.
The resource is intended for high school families (grades 9-12).

Evaluate the following resource:
Name: {name}
Description: {description}
Link: {link}

Score it 0.0 to 1.0 for quality (relevance, clarity, appropriateness).
Respond with JSON only: {{"score": 0.85, "summary": "Brief feedback in 1-2 sentences"}}"""


def _set_pending_admin(db: Session, submission_id: uuid.UUID) -> None:
    """Fallback: skip AI step and route directly to admin queue."""
    from src.content.models import ContentAsset

    asset = db.get(ContentAsset, submission_id)
    if asset:
        asset.review_status = "pending_admin"
        db.commit()


def ai_review_submission(submission_id: uuid.UUID, db: Session) -> None:
    """Fetch submission, call OpenAI, persist score + summary, advance status.

    All failures are caught — the task must never crash the background worker.
    """
    from src.content.models import ContentAsset

    # 1. Fetch submission
    asset = db.get(ContentAsset, submission_id)
    if not asset:
        logger.warning("ai_review_submission: submission %s not found", submission_id)
        return

    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        logger.info(
            "OPENAI_API_KEY not set — skipping AI review for submission %s", submission_id
        )
        _set_pending_admin(db, submission_id)
        return

    # 2. Try to import openai (optional dependency)
    try:
        from openai import OpenAI  # type: ignore[import-untyped]
    except ImportError:
        logger.warning("openai package not installed — skipping AI review for %s", submission_id)
        _set_pending_admin(db, submission_id)
        return

    prompt = _REVIEW_PROMPT_TEMPLATE.format(
        name=asset.name or "",
        description=asset.description or "(no description)",
        link=asset.link or "(no link)",
    )

    # 3. Call OpenAI
    try:
        client = OpenAI(api_key=api_key)
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.2,
            max_tokens=200,
        )
        raw = response.choices[0].message.content or ""
        # Strip markdown code fences if present
        raw = raw.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
        data = json.loads(raw)
        score = float(data.get("score", 0.0))
        summary = str(data.get("summary", ""))
    except Exception as exc:
        logger.exception(
            "ai_review_submission: OpenAI call failed for %s: %s", submission_id, exc
        )
        _set_pending_admin(db, submission_id)
        return

    # 4. Persist result
    try:
        asset = db.get(ContentAsset, submission_id)
        if asset:
            asset.ai_review_score = max(0.0, min(1.0, score))
            asset.ai_review_summary = summary
            asset.review_status = "pending_admin"
            db.commit()
            logger.info(
                "ai_review_submission: submission %s scored %.2f", submission_id, score
            )
    except Exception as exc:
        logger.exception(
            "ai_review_submission: DB update failed for %s: %s", submission_id, exc
        )
        db.rollback()
