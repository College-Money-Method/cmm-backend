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


def call_json(
    *,
    system: str,
    content: Any,
    max_tokens: int = 1024,
) -> tuple[dict[str, Any], int, int]:
    """Call Haiku and parse the reply as a JSON object.

    `content` is passed straight through as the user message content, so it takes
    either a plain string or a list of blocks (text + image) for vision calls.

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

    # Guard an empty content list or a non-text block; a missing .text would
    # otherwise raise an unhandled AttributeError.
    raw = ""
    if message.content:
        block = message.content[0]
        raw = block.text if hasattr(block, "text") else ""
    if not raw:
        raise BedrockCallError("Bedrock returned no text content")

    try:
        parsed = json.loads(strip_code_fences(raw))
    except json.JSONDecodeError as exc:
        logger.error("Bedrock returned non-JSON: %r", raw[:500])
        raise BedrockCallError(f"Bedrock response was not valid JSON: {exc}") from exc

    if not isinstance(parsed, dict):
        raise BedrockCallError(
            f"Expected a JSON object, got {type(parsed).__name__}"
        )

    usage = message.usage
    return (
        parsed,
        getattr(usage, "input_tokens", 0) or 0,
        getattr(usage, "output_tokens", 0) or 0,
    )
