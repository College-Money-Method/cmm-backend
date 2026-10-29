"""Family-wording questions per chunk, written once at sync time by Haiku.

Content is written in financial-aid vocabulary ("noncustodial parent",
"Student Aid Index"); families ask in their own ("we're divorced", "what will
we have to pay"). Storing a few such questions next to each chunk puts both
vocabularies into the keyword index and the embedding, so either wording finds
the chunk. A failed call leaves the chunk unenriched rather than failing sync.
"""

from __future__ import annotations

import logging

from src.config import settings
from src.video_pipeline import bedrock_usage
from src.video_pipeline.bedrock_client import BedrockCallError, call_json

logger = logging.getLogger(__name__)

_SYSTEM = """You help a college-financing education site match family questions to its content.

Given one passage, write 4-6 short questions that a parent or student might type
into a chat box, which this passage answers. Use plain, everyday wording and the
synonyms real families use (e.g. "divorced" or "separated" for "noncustodial
parent", "what will we pay" for "net price"). Do not copy sentences from the
passage. Questions only; no answers.

Reply with JSON only: {"questions": ["...", "..."]}"""


def enrich_chunk(title: str, heading: str | None, text: str) -> str | None:
    """Newline-joined questions this chunk answers, or None if the call failed."""
    passage = f"Page: {title}\nSection: {heading or '(intro)'}\n\n{text}"
    try:
        parsed, _, _ = call_json(
            system=_SYSTEM,
            content=passage,
            invoke_type=bedrock_usage.ASSISTANT_ENRICH,
            max_tokens=400,
            model_id=settings.assistant_enrich_model_id,
        )
    except BedrockCallError as exc:
        logger.warning("Assistant enrichment failed for %r: %s", title, exc)
        return None
    questions = [q.strip() for q in parsed.get("questions") or [] if isinstance(q, str) and q.strip()]
    return "\n".join(questions[:6]) or None
