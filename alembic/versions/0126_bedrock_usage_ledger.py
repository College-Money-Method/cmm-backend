"""Bedrock spend ledger: one row per model invocation

Token counts used to be kept by whatever the call produced — an extraction row,
a job record — so a call that produced no row recorded nothing. Frame
classification, the most-invoked call in the system, discarded its usage at the
call site, and topic segmentation and Q&A classification only ever logged it.
None of that could be summed, so a third of the spend was invisible.

``bedrock_client.call_json`` now appends a row here for every invocation. The
table is standalone and referenced by nothing: it outlives the job or webinar
that caused the call, because spend history should not disappear when the thing
it was spent on does.

``cost_usd`` is stored rather than derived at read time so a change to the
configured rates does not restate what past calls cost.

Translation spend keeps its own older ledger (``translation_usage``) and uses a
different client, so the two never double-count; the analytics endpoint unions
them.

Revision ID: 0126
Revises: 0125
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0126"
down_revision = "0125"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "bedrock_usage",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("invoke_type", sa.Text(), nullable=False),
        sa.Column("model_id", sa.Text(), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
        sa.Column("cost_usd", sa.Numeric(12, 6), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("idx_bedrock_usage_invoke_type", "bedrock_usage", ["invoke_type"])
    op.create_index("idx_bedrock_usage_created_at", "bedrock_usage", ["created_at"])


def downgrade() -> None:
    op.drop_index("idx_bedrock_usage_created_at", table_name="bedrock_usage")
    op.drop_index("idx_bedrock_usage_invoke_type", table_name="bedrock_usage")
    op.drop_table("bedrock_usage")
