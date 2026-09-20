"""Record what a Bedrock invocation cost.

``bedrock_client.call_json`` calls ``record`` on every invocation that reported
usage, so no call site has to remember to. That is the whole point: the counts
that went missing before went missing because recording them was each caller's
own job, and three callers did not do it.

Two consequences of recording at the client are deliberate:

* **It opens its own session.** Callers are a mix of request handlers, Celery
  tasks and scripts; several have no session to lend, and frame classification
  runs far from one. A short transaction per call keeps the ledger out of the
  caller's transaction, so a failed job still leaves its spend recorded.
* **It never raises.** A ledger write is bookkeeping. A pipeline that publishes
  a video must not fail because an insert into an analytics table did, so
  everything here is inside a try and the worst case is a warning and a missing
  row.
"""

from __future__ import annotations

import logging
from decimal import Decimal

from src.config import settings
from src.db.base import get_session_factory
from src.video_pipeline.bedrock_usage_models import BedrockUsage

logger = logging.getLogger(__name__)

# The call sites, named once so the analytics grouping and the callers cannot
# drift apart on a typo. Stored as plain text, so adding one here is the only
# step needed to make it appear on the admin page.
QA_EXTRACTION = "qa_extraction"
QA_CLASSIFICATION = "qa_classification"
TRIM_POINT = "trim_point"
TOPIC_SEGMENT = "topic_segment"
FRAME_CLASSIFY = "frame_classify"


def cost_usd(input_tokens: int, output_tokens: int) -> Decimal:
    """USD for one invocation, from the configured per-1M-token rates."""
    cost = (
        input_tokens * settings.bedrock_haiku_input_usd_per_mtok
        + output_tokens * settings.bedrock_haiku_output_usd_per_mtok
    ) / 1_000_000
    return Decimal(str(round(cost, 6)))


def record(invoke_type: str, model_id: str, input_tokens: int, output_tokens: int) -> None:
    """Append one ledger row. Silent on success, warns and continues on failure."""
    if not input_tokens and not output_tokens:
        # Nothing was billed, so there is nothing to account for. Storing a zero
        # row would inflate the invocation count with calls that never happened.
        return
    try:
        session_factory = get_session_factory()
        with session_factory() as db:
            db.add(
                BedrockUsage(
                    invoke_type=invoke_type,
                    model_id=model_id,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cost_usd=cost_usd(input_tokens, output_tokens),
                )
            )
            db.commit()
    except Exception as exc:  # noqa: BLE001 - bookkeeping must not break the caller
        logger.warning(
            "Bedrock usage not recorded (invoke_type=%s tokens_in=%d tokens_out=%d): %s",
            invoke_type,
            input_tokens,
            output_tokens,
            exc,
        )
