"""The Bedrock spend ledger — one row per model invocation.

Lives beside ``bedrock_client`` because that client is what writes it, and the
client is shared: the video pipeline, the workshops Q&A services and anything
else that calls a model all go through it. The table is therefore not a video
pipeline concern despite its neighbours.

Why a ledger at all, when several callers already keep their own token counts:
those counts are attached to whatever the call produced — an extraction row, a
job record — so a call that produces no row records nothing. Frame
classification discarded its usage entirely, and it is the most-invoked call in
the system. Spend that only exists inside a log line cannot be added up.

``cost_usd`` is computed and stored at insert time, like ``translation_usage``
does, so a change to the configured rates does not silently restate history.

Query examples:
    SELECT invoke_type, SUM(cost_usd) FROM bedrock_usage GROUP BY invoke_type;
    SELECT date_trunc('day', created_at) d, SUM(cost_usd) FROM bedrock_usage GROUP BY d;
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import TIMESTAMP, Index, Integer, Numeric, Text, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from src.db.base import Base


class BedrockUsage(Base):
    """One Bedrock invocation: what called it, what it cost, when.

    Written even when a call fails partway, as long as the model answered and
    reported usage — a reply that was billed but then rejected as unparseable
    is still money spent, and hiding it would make the ledger flattering rather
    than accurate.
    """

    __tablename__ = "bedrock_usage"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    # Which call site spent this. Free text, not an enum: a new caller should
    # show up in the analytics the day it ships, not the day someone remembers
    # to extend a whitelist. Values are the constants in `bedrock_usage`.
    invoke_type: Mapped[str] = mapped_column(Text, nullable=False)
    model_id: Mapped[str] = mapped_column(Text, nullable=False)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        Index("idx_bedrock_usage_invoke_type", "invoke_type"),
        Index("idx_bedrock_usage_created_at", "created_at"),
    )
