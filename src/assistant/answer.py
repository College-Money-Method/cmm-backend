"""Stream Haiku's answer to a prebuilt system prompt as server-sent events.

The prompt (built per audience) numbers the retrieved chunks and tells the model
to cite them as ``[n]``; the same numbers go to the browser in the ``meta``
event so citations link to the right page. The answer is saved when the stream ends — from a
session of its own, because the request's session is closed by the time a
streaming body runs — even when the family has left by then.
"""

from __future__ import annotations

import json
import logging
import queue
import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

from src.assistant.models import AssistantConversation, AssistantMessage
from src.assistant.search import RetrievedChunk
from src.config import settings
from src.db.base import get_session_factory
from src.video_pipeline import bedrock_usage
from src.video_pipeline.bedrock_client import get_client

logger = logging.getLogger(__name__)

MAX_ANSWER_TOKENS = 1200
# Prior turns sent with a question: enough for "what about for juniors?" to
# make sense, small enough to keep the prompt cheap.
HISTORY_MESSAGES = 6
# Answers being written at once; more wait their turn. Python lets these finish
# before the process exits, so a deploy does not cut an answer short.
_ANSWER_WORKERS = ThreadPoolExecutor(max_workers=16, thread_name_prefix="assistant-answer")
# Marks the end of an answer's events.
_END = object()

def source_list(chunks: list[RetrievedChunk]) -> list[dict]:
    """The citation list sent to the browser and stored with the answer."""
    return [
        {"n": n, "title": c.title, "url": c.url_path, "source_type": c.source_type, "heading": c.heading}
        for n, c in enumerate(chunks, start=1)
    ]


def numbered_sources(chunks: list[RetrievedChunk]) -> str:
    """The prompt's numbered source blocks, matching the numbers in ``source_list``."""
    return "\n\n".join(
        f"[{n}] {c.title}{f' — {c.heading}' if c.heading else ''}\n{c.text}" for n, c in enumerate(chunks, start=1)
    ) or "(no sources matched this question)"


def sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _save_answer(
    conversation_id: uuid.UUID, text: str, sources: list[dict], profile: dict, tokens: tuple[int, int]
) -> uuid.UUID:
    with get_session_factory()() as db:
        message = AssistantMessage(
            conversation_id=conversation_id,
            role="assistant",
            content=text,
            sources=sources,
            profile=profile,
            model_id=settings.assistant_answer_model_id,
            input_tokens=tokens[0],
            output_tokens=tokens[1],
        )
        db.add(message)
        conversation = db.get(AssistantConversation, conversation_id)
        if conversation is not None:
            conversation.updated_at = datetime.now(UTC)
        db.commit()
        return message.id


def _reported_tokens(stream) -> tuple[int, int]:
    """(input, output) tokens the model has reported on this stream so far.

    After a full answer this is the bill. After a Bedrock failure mid-answer
    input is exact but output is short, since the final output count only
    arrives with the last event.
    """
    try:
        usage = stream.current_message_snapshot.usage
    except Exception:  # noqa: BLE001 - no message has started, so nothing is reported
        return (0, 0)
    return (usage.input_tokens or 0, usage.output_tokens or 0)


def stream_answer(
    *,
    conversation_id: uuid.UUID,
    history: list[dict],
    question: str,
    chunks: list[RetrievedChunk],
    profile: dict,
    system_prompt: str,
) -> Iterator[str]:
    """Yield ``meta``, then ``delta``s, then ``done`` (or ``error``).

    A worker writes and saves the answer; this only relays its events.
    The server drops a response when the family leaves (reload, closed tab), so
    an answer tied to it would be lost from their history but still billed.
    """
    events: queue.SimpleQueue = queue.SimpleQueue()
    _ANSWER_WORKERS.submit(
        _answer_into, events, conversation_id, history, question, chunks, profile, system_prompt
    )
    while (event := events.get()) is not _END:
        yield event


def _answer_into(
    events: queue.SimpleQueue,
    conversation_id: uuid.UUID,
    history: list[dict],
    question: str,
    chunks: list[RetrievedChunk],
    profile: dict,
    system_prompt: str,
) -> None:
    try:
        for event in _answer_events(conversation_id, history, question, chunks, profile, system_prompt):
            events.put(event)
    except Exception:  # noqa: BLE001 - the relay must still end
        logger.exception("Assistant answer crashed for conversation %s", conversation_id)
        events.put(sse("error", {"code": "assistant_upstream_error"}))
    finally:
        events.put(_END)


def _answer_events(
    conversation_id: uuid.UUID,
    history: list[dict],
    question: str,
    chunks: list[RetrievedChunk],
    profile: dict,
    system_prompt: str,
) -> Iterator[str]:
    sources = source_list(chunks)
    yield sse("meta", {"conversation_id": str(conversation_id), "sources": sources})

    model = settings.assistant_answer_model_id
    parts: list[str] = []
    tokens = (0, 0)
    try:
        with get_client().messages.stream(
            model=model,
            max_tokens=MAX_ANSWER_TOKENS,
            system=system_prompt,
            messages=[*history, {"role": "user", "content": question}],
        ) as stream:
            try:
                for text in stream.text_stream:
                    parts.append(text)
                    yield sse("delta", {"text": text})
            finally:
                # Also runs when the stream fails midway: Bedrock bills those answers too.
                tokens = _reported_tokens(stream)
                bedrock_usage.record(bedrock_usage.ASSISTANT_ANSWER, model, *tokens)
    except Exception as exc:  # noqa: BLE001 - any failure must reach the browser as an event
        logger.warning("Assistant answer failed: %s", exc)
        yield sse("error", {"code": "assistant_upstream_error"})
        return

    answer = "".join(parts).strip()
    if not answer:
        yield sse("error", {"code": "assistant_empty_answer"})
        return
    try:
        message_id = _save_answer(conversation_id, answer, sources, profile, tokens)
    except Exception:  # noqa: BLE001 - the family already has the answer on screen
        logger.exception("Assistant answer not saved for conversation %s", conversation_id)
        yield sse("error", {"code": "assistant_save_failed"})
        return
    yield sse("done", {"message_id": str(message_id)})
