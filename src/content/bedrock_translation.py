"""Bedrock-powered translation utility using the classic AnthropicBedrock client.

Calls Claude Haiku through Bedrock's InvokeModel path via a cross-region
inference profile. Model id is configurable via settings.bedrock_haiku_model_id
(default: us.anthropic.claude-haiku-4-5-20251001-v1:0).

Why not AnthropicBedrockMantle: the Mantle endpoint (bedrock-mantle.*.api.aws)
is a separate integration this AWS account is not provisioned for (403), and it
does not accept inference-profile ids. Haiku 4.5 requires an inference-profile
id — bare on-demand model ids are rejected — so the classic client is used.

No budget_tokens — not supported on Haiku. Streaming via stream() +
get_final_message() to handle large HTML payloads safely.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from functools import lru_cache

from anthropic import AnthropicBedrock, APIConnectionError, APIStatusError, APITimeoutError

from src.config import SUPPORTED_LOCALES, settings

logger = logging.getLogger(__name__)


@dataclass
class TranslationOutput:
    """Translated fields plus the Bedrock token usage for that invocation."""

    fields: dict[str, object]
    input_tokens: int
    output_tokens: int
    model_id: str

# Max tokens for the translated output. HTML content can be large; 8k covers
# most topic pages. Haiku's context window is 200k input / 8k output.
_MAX_OUTPUT_TOKENS = 8192

# Regex to strip an optional markdown code fence that the model may wrap JSON in.
# Uses re.DOTALL so `.` matches newlines; anchored to the WHOLE string (not per-line)
# so triple-backticks embedded inside content values are never touched.
_CODE_FENCE_RE = re.compile(r"^```(?:json)?\s*\n?(.*)\n?\s*```$", re.DOTALL)


class TranslationError(Exception):
    """Raised when Bedrock translation fails or returns unparseable output."""


# Brand / proper-noun terms the model must keep verbatim (never translated),
# including when embedded inside a larger sentence. Mirror of the frontend list
# in cmm-frontend/app/lib/i18n/protected-terms.ts — keep the two in sync.
PROTECTED_TERMS: tuple[str, ...] = ("College Money Method",)


@lru_cache(maxsize=1)
def _get_bedrock_client() -> AnthropicBedrock:
    """Return a cached Bedrock client (one per process)."""
    return AnthropicBedrock(
        aws_region=settings.bedrock_region,
        # Credentials from settings (mirrors S3 pattern); falls back to default
        # cred chain (task role / env vars) when values are empty strings.
        aws_access_key=settings.aws_access_key_id or None,
        aws_secret_key=settings.aws_secret_access_key or None,
    )


def _build_system_prompt(target_locale: str, extra_rules: str | None = None) -> str:
    language_name = SUPPORTED_LOCALES.get(target_locale, target_locale)
    protected = ", ".join(f'"{term}"' for term in PROTECTED_TERMS)
    suffix = f"\n\nAdditional rules for this content type:\n{extra_rules}" if extra_rules else ""
    return (
        f"You are a professional translator and educational content editor. "
        f"Your task is to translate educational financial-aid content into {language_name}.\n\n"
        "Rules (non-negotiable):\n"
        "1. Translate accurately into the target language.\n"
        "2. Refine tone to sound natural to a native speaker while keeping a professional, educational voice.\n"
        "3. Preserve ALL HTML tags and attributes exactly — never alter, add, or remove tags.\n"
        "4. For any field whose JSON value parses as a JSON object or array (Tiptap rich-text), "
        "preserve the entire JSON structure and translate ONLY human-readable text leaf values.\n"
        "5. Do NOT translate: code snippets, URLs, href values, placeholder tokens, or proper nouns "
        "that are brand/product names.\n"
        f"6. Keep these exact brand names verbatim in English wherever they appear, including "
        f"inside a sentence — never translate or transliterate them: {protected}.\n"
        "7. Return ONLY a valid JSON object with the SAME keys as the input and translated values. "
        "No explanation, no markdown, no code fences."
        + suffix
    )


def _build_user_message(fields: dict) -> str:
    return (
        "Translate the following JSON field map. Return only the JSON object with translated values.\n\n"
        + json.dumps(fields, ensure_ascii=False)
    )


def _strip_code_fences(text: str) -> str:
    """Remove an outer ```json ... ``` fence if the model wrapped its response in one.

    Only the outermost fence (bracketing the entire response) is stripped.
    Triple-backtick sequences inside translated content values are preserved.
    """
    stripped = text.strip()
    m = _CODE_FENCE_RE.match(stripped)
    return m.group(1).strip() if m else stripped


# Matches one "key": "value" pair. The value is non-greedy and terminated by a
# lookahead for the *structural* delimiter (`, "` or a closing brace), so raw
# quotes inside the value do not end the match. Works for both the pretty-printed
# and single-line compact objects the model emits — DOTALL covers multi-line
# values (two-line cues).
_JSON_PAIR_RE = re.compile(
    r'"([^"\\]+)"\s*:\s*"(.*?)"\s*(?=,\s*"|\}\s*$)',
    re.DOTALL,
)


def _repair_flat_json_object(text: str) -> str | None:
    """Best-effort repair of a flat {"k": "v"} object with unescaped inner quotes.

    Observed repeatedly on caption batches: cues quoting UI labels come back as
    ``"154": "到"我找不到我的学校"。"`` — raw double quotes inside the value,
    which invalidates the whole response and loses ~25 good translations with it.

    Pairs are re-extracted and re-encoded with ``json.dumps``, so escaping is
    handled by the encoder rather than by string surgery. Returns None when the
    text is not a flat object of string pairs, letting the caller fall through
    to its normal error path.
    """
    stripped = text.strip()
    if not (stripped.startswith("{") and stripped.endswith("}")):
        return None

    pairs = _JSON_PAIR_RE.findall(stripped)
    if not pairs:
        return None

    def decode(value: str) -> str:
        # Undo the escapes the model did emit correctly; json.dumps re-applies
        # them. Order matters: backslash last would double-unescape the others.
        return (
            value.replace('\\"', '"')
            .replace("\\n", "\n")
            .replace("\\t", "\t")
            .replace("\\\\", "\\")
        )

    return json.dumps({k: decode(v) for k, v in pairs}, ensure_ascii=False)


def translate_fields(
    fields: dict[str, object], target_locale: str, extra_rules: str | None = None
) -> TranslationOutput:
    """Translate a field map via Bedrock Haiku.

    Args:
        fields: Dict of field_name → value (strings, HTML strings, or JSON-serialisable
                structured data like action_items/faqs lists).
        target_locale: BCP-47 locale code present in SUPPORTED_LOCALES.
        extra_rules: Optional content-type-specific rules appended to the system
                prompt. Omitted by the site-translation callers, so their prompt
                is unchanged; used by the caption pipeline, whose fields are
                subtitle cues with constraints ordinary content does not have.

    Returns:
        TranslationOutput — translated fields (same keys) plus token usage.

    Raises:
        TranslationError: On Bedrock API error or unparseable JSON response.
    """
    if not fields:
        return TranslationOutput({}, 0, 0, settings.bedrock_haiku_model_id)

    client = _get_bedrock_client()
    system_prompt = _build_system_prompt(target_locale, extra_rules)
    user_message = _build_user_message(fields)

    logger.info(
        "Calling Bedrock Haiku for translation to %s (%d fields, model=%s)",
        target_locale,
        len(fields),
        settings.bedrock_haiku_model_id,
    )

    try:
        with client.messages.stream(
            model=settings.bedrock_haiku_model_id,
            max_tokens=_MAX_OUTPUT_TOKENS,
            system=system_prompt,
            messages=[{"role": "user", "content": user_message}],
        ) as stream:
            message = stream.get_final_message()
    except APIConnectionError as exc:
        raise TranslationError(f"Bedrock connection error: {exc}") from exc
    except APITimeoutError as exc:
        raise TranslationError(f"Bedrock request timed out: {exc}") from exc
    except APIStatusError as exc:
        raise TranslationError(
            f"Bedrock API error {exc.status_code}: {exc.message}"
        ) from exc

    # Guard against an empty content list or a non-text content block (e.g. tool_use).
    # A missing .text attr would otherwise raise an unhandled AttributeError → 500.
    if message.content:
        block = message.content[0]
        raw_text = block.text if hasattr(block, "text") else ""
    else:
        raw_text = ""
    if not raw_text:
        raise TranslationError("Bedrock returned no text content")
    clean_text = _strip_code_fences(raw_text)

    try:
        translated: dict[str, object] = json.loads(clean_text)
    except json.JSONDecodeError as exc:
        # Try to salvage the batch before discarding 25 good translations —
        # the usual cause is unescaped quotes inside otherwise valid values.
        repaired = _repair_flat_json_object(clean_text)
        try:
            if repaired is None:
                raise exc
            translated = json.loads(repaired)
            logger.info("Recovered a malformed Bedrock response by re-escaping quotes")
        except json.JSONDecodeError:
            logger.error("Bedrock returned non-JSON: %r", raw_text[:500])
            raise TranslationError(
                f"Bedrock response was not valid JSON: {exc}"
            ) from exc

    if not isinstance(translated, dict):
        raise TranslationError(
            f"Bedrock response must be a JSON object, got {type(translated).__name__}"
        )

    usage = message.usage
    return TranslationOutput(
        fields=translated,
        input_tokens=getattr(usage, "input_tokens", 0) or 0,
        output_tokens=getattr(usage, "output_tokens", 0) or 0,
        model_id=message.model or settings.bedrock_haiku_model_id,
    )
