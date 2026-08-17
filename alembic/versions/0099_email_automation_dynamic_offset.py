"""email_automation dynamic offset — type + offset_value/unit/direction, drop key

Generalizes `email_automation` from a single hardcoded pre-workshop-reminder
row to a typed, dynamic-offset automation (type, offset_value, offset_unit,
offset_direction), and repoints `template_id` at the new `email_template`
table (0097) instead of the counselor-facing `workshop_email_templates`.

Revision ID: 0099
Revises: 0098
Create Date: 2026-08-15
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0099"
down_revision: Union[str, None] = "0098"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- type (backfilled from the existing `key`, which already matches) ---
    op.add_column(
        "email_automation",
        sa.Column("type", sa.Text(), nullable=False, server_default="pre_workshop_reminder"),
    )
    op.create_check_constraint(
        "ck_email_automation_type",
        "email_automation",
        "type IN ('pre_workshop_reminder', 'post_workshop_reminder')",
    )
    op.execute("UPDATE email_automation SET type = key")

    # --- offset_value (backfilled from offset_days, which it replaces) ---
    op.add_column(
        "email_automation", sa.Column("offset_value", sa.Integer(), nullable=False, server_default="7")
    )
    op.execute("UPDATE email_automation SET offset_value = offset_days")
    op.drop_column("email_automation", "offset_days")

    # --- offset_unit / offset_direction (new dimensions, no prior data) ---
    op.add_column(
        "email_automation", sa.Column("offset_unit", sa.Text(), nullable=False, server_default="days")
    )
    op.create_check_constraint(
        "ck_email_automation_offset_unit", "email_automation", "offset_unit IN ('days', 'hours')"
    )
    op.add_column(
        "email_automation", sa.Column("offset_direction", sa.Text(), nullable=False, server_default="before")
    )
    op.create_check_constraint(
        "ck_email_automation_offset_direction", "email_automation", "offset_direction IN ('before', 'after')"
    )

    # --- drop `key` (type is now the stable identifier admins pick by) ---
    op.drop_constraint("uq_email_automation_key", "email_automation", type_="unique")
    op.drop_column("email_automation", "key")

    # --- repoint template_id at email_template (old workshop_email_templates
    # rows are a different concept entirely — no safe mapping exists, so any
    # pinned template is cleared and must be re-picked from the new table) ---
    op.drop_constraint("fk_email_automation_template_id", "email_automation", type_="foreignkey")
    op.execute("UPDATE email_automation SET template_id = NULL")
    op.create_foreign_key(
        "fk_email_automation_template_id",
        "email_automation",
        "email_template",
        ["template_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("fk_email_automation_template_id", "email_automation", type_="foreignkey")
    op.execute("UPDATE email_automation SET template_id = NULL")
    op.create_foreign_key(
        "fk_email_automation_template_id",
        "email_automation",
        "workshop_email_templates",
        ["template_id"],
        ["id"],
        ondelete="SET NULL",
    )

    op.add_column("email_automation", sa.Column("key", sa.Text(), nullable=True))
    op.execute("UPDATE email_automation SET key = type")
    op.alter_column("email_automation", "key", nullable=False)
    op.create_unique_constraint("uq_email_automation_key", "email_automation", ["key"])

    op.drop_constraint("ck_email_automation_offset_direction", "email_automation", type_="check")
    op.drop_column("email_automation", "offset_direction")

    op.drop_constraint("ck_email_automation_offset_unit", "email_automation", type_="check")
    op.drop_column("email_automation", "offset_unit")

    op.add_column("email_automation", sa.Column("offset_days", sa.Integer(), nullable=True))
    op.execute("UPDATE email_automation SET offset_days = offset_value")
    op.alter_column("email_automation", "offset_days", nullable=False, server_default="7")
    op.drop_column("email_automation", "offset_value")

    op.drop_constraint("ck_email_automation_type", "email_automation", type_="check")
    op.drop_column("email_automation", "type")
