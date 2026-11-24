"""On-demand summary of one assistant conversation, for admins reviewing it.

Haiku reads the transcript (with any ratings and comments the user left) and
writes a short brief: what the user wanted, what the assistant gave, and what
went wrong or stayed unanswered. The result is cached on the conversation with
the number of messages it covered, so the admin UI can tell when it is stale.
"""

from __future__ import annotations

from datetime import UTC, datetime

from src.assistant.models import AssistantConversation, AssistantMessage, AssistantMessageFeedback
from src.config import settings
from src.video_pipeline import bedrock_usage
from src.video_pipeline.bedrock_client import BedrockCallError, call_json

# Long chats are cut from the middle of each message, not dropped, so every turn stays visible.
_MESSAGE_CHARS = 2500
_TRANSCRIPT_CHARS = 60_000

_SYSTEM = """You help the College Money Method team review conversations between users (usually high
school counselors, sometimes families) and the College Money Method assistant.

Summarize the conversation for a reviewer in plain text with these three short parts:
Goal: what the user was trying to get done (one or two sentences).
What the assistant gave: the main answers, drafts or numbers it provided (2-4 bullets starting with "- ").
Gaps: anything wrong, missing, refused, or that frustrated the user, including any rating or comment
they left; write "None noticed." if there is nothing.

Be factual and brief; do not judge tone. Never repeat names of students or families.

Reply with JSON only: {"summary": "..."}"""


class SummaryError(RuntimeError):
    """The model call failed; nothing was stored."""


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    half = limit // 2
    return f"{text[:half]}\n[…]\n{text[-half:]}"


def _feedback_note(feedback: AssistantMessageFeedback | None) -> str:
    if feedback is None:
        return ""
    parts = []
    if feedback.rating == 1:
        parts.append("rated helpful")
    elif feedback.rating == -1:
        parts.append("rated not helpful")
    if feedback.flag == "wrong_or_outdated":
        parts.append("flagged wrong or out of date")
    if feedback.comment:
        parts.append(f'comment: "{feedback.comment}"')
    return f"\n[User feedback on this answer: {'; '.join(parts)}]" if parts else ""


def build_transcript(
    messages: list[AssistantMessage], feedback_by_message: dict, audience: str
) -> str:
    user = "Counselor" if audience == "counselor" else "User"
    lines = [
        f"{user if m.role == 'user' else 'Assistant'}: {_clip(m.content, _MESSAGE_CHARS)}"
        + _feedback_note(feedback_by_message.get(m.id))
        for m in messages
    ]
    return _clip("\n\n".join(lines), _TRANSCRIPT_CHARS)


def summarize_conversation(
    db, conversation: AssistantConversation, feedback_by_message: dict
) -> AssistantConversation:
    """Write and store the summary; raises SummaryError if the model call fails."""
    messages = list(conversation.messages)
    if not messages:
        raise SummaryError("empty_conversation")
    transcript = build_transcript(messages, feedback_by_message, conversation.audience)
    try:
        parsed, _, _ = call_json(
            system=_SYSTEM,
            content=transcript,
            invoke_type=bedrock_usage.ASSISTANT_SUMMARY,
            max_tokens=600,
            model_id=settings.assistant_enrich_model_id,
        )
    except BedrockCallError as exc:
        raise SummaryError(str(exc)) from exc
    summary = parsed.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        raise SummaryError("empty_summary")
    conversation.admin_summary = summary.strip()
    conversation.admin_summary_at = datetime.now(UTC)
    conversation.admin_summary_messages = len(messages)
    db.commit()
    return conversation
