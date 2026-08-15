"""email automation — scheduler-driven automation settings table

Adds `email_automation` (one row per scheduler automation, e.g.
"pre_workshop_reminder") and seeds a single disabled row so the pre-workshop
reminder scheduler (Phase 5) ships dark until explicitly turned on via
Phase 4's Automations admin tab.

Revision ID: 0095
Revises: 0094
Create Date: 2026-08-15
"""

from __future__ import annotations

import uuid
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0095"
down_revision: Union[str, None] = "0094"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "email_automation",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("offset_days", sa.Integer(), nullable=False, server_default="7"),
        sa.Column("template_id", sa.Uuid(), nullable=True),
        sa.Column("subject_override", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("updated_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.UniqueConstraint("key", name="uq_email_automation_key"),
    )
    op.create_foreign_key(
        "fk_email_automation_template_id",
        "email_automation",
        "workshop_email_templates",
        ["template_id"],
        ["id"],
        ondelete="SET NULL",
    )

    op.execute(
        sa.text(
            "INSERT INTO email_automation (id, key, name, enabled, offset_days) "
            "VALUES (:id, :key, :name, false, 7)"
        ).bindparams(
            id=str(uuid.uuid4()),
            key="pre_workshop_reminder",
            name="Pre-Workshop Reminder",
        )
    )


def downgrade() -> None:
    op.drop_constraint("fk_email_automation_template_id", "email_automation", type_="foreignkey")
    op.drop_table("email_automation")
