"""Shared Bedrock access for the video pipeline.

Mirrors the client setup in `src/content/bedrock_translation.py` rather than
importing its private helper. Both calls in this pipeline (trim-point text and
frame-classification vision) run on `settings.bedrock_haiku_model_id`.
"""

from __future__ import annotations

import json
import logging
import re
from functools import lru_cache
from typing import Any

from anthropic import (
    AnthropicBedrock,
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
)

from src.config import settings
from src.video_pipeline import bedrock_usage

logger = logging.getLogger(__name__)

_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.MULTILINE)


class BedrockCallError(RuntimeError):
    """A Bedrock call failed or returned something unusable."""


@lru_cache(maxsize=1)
def get_client() -> AnthropicBedrock:
    """Return a cached Bedrock client (one per process)."""
    return AnthropicBedrock(
        aws_region=settings.bedrock_region,
        # Empty strings fall back to the default credential chain (task role).
        aws_access_key=settings.aws_access_key_id or None,
        aws_secret_key=settings.aws_secret_access_key or None,
    )


def strip_code_fences(text: str) -> str:
    """Remove ```json ... ``` wrapping that the model sometimes adds."""
    return _FENCE_RE.sub("", text).strip()


def parse_leading_object(text: str) -> dict[str, Any]:
    """Read the JSON object at the front of a reply, ignoring anything after it.

    Asking for JSON and nothing else does not always get it: the model sometimes
    appends a sentence explaining its answer, and a plain ``json.loads`` rejects
    the whole reply over trailing prose it does not need. That cost frames — a
    classification the model got right was thrown away because it was too
    talkative about it.

    Only trailing text is tolerated. A reply that does not *begin* with an object
    is still an error, because there is then no answer to take.
    """
    obj, _end = json.JSONDecoder().raw_decode(text)
    if not isinstance(obj, dict):
        raise json.JSONDecodeError("expected an object", text, 0)
    return obj


def call_json(
    *,
    system: str,
    content: Any,
    invoke_type: str,
    max_tokens: int = 1024,
) -> tuple[dict[str, Any], int, int]:
    """Call Haiku and parse the reply as a JSON object.

    `content` is passed straight through as the user message content, so it takes
    either a plain string or a list of blocks (text + image) for vision calls.

    `invoke_type` names the call site for the spend ledger and is required, so a
    new caller cannot quietly add cost that the admin page has no name for. Use
    a constant from `bedrock_usage`.

    Returns (parsed_object, input_tokens, output_tokens).
    Raises BedrockCallError on transport failure, empty output, or non-object JSON.
    """
    client = get_client()
    try:
        with client.messages.stream(
            model=settings.bedrock_haiku_model_id,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": content}],
        ) as stream:
            message = stream.get_final_message()
    except APIConnectionError as exc:
        raise BedrockCallError(f"Bedrock connection error: {exc}") from exc
    except APITimeoutError as exc:
        raise BedrockCallError(f"Bedrock request timed out: {exc}") from exc
    except APIStatusError as exc:
        raise BedrockCallError(
            f"Bedrock API error {exc.status_code}: {exc.message}"
        ) from exc

    # Recorded before the reply is inspected: the tokens below were billed
    # whether or not what came back turns out to be usable, and a ledger that
    # dropped the failures would understate exactly the spend worth finding.
    usage = message.usage
    input_tokens = getattr(usage, "input_tokens", 0) or 0
    output_tokens = getattr(usage, "output_tokens", 0) or 0
    bedrock_usage.record(
        invoke_type, settings.bedrock_haiku_model_id, input_tokens, output_tokens
    )

    # Guard an empty content list or a non-text block; a missing .text would
    # otherwise raise an unhandled AttributeError.
    raw = ""
    if message.content:
        block = message.content[0]
        raw = block.text if hasattr(block, "text") else ""
    if not raw:
        raise BedrockCallError("Bedrock returned no text content")

    try:
        parsed = parse_leading_object(strip_code_fences(raw))
    except json.JSONDecodeError as exc:
        logger.error("Bedrock returned non-JSON: %r", raw[:500])
        raise BedrockCallError(f"Bedrock response was not valid JSON: {exc}") from exc

    return parsed, input_tokens, output_tokens
