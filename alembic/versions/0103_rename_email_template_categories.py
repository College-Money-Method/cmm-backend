"""rename email_template categories to general/workshop

Renames the two template categories to plainer names that match what admins see:
``broadcast`` -> ``general`` (any one-off send: broadcasts, communications) and
``workshop_automation`` -> ``workshop``. Data-only rename plus the check
constraint; no behavioural change.

Revision ID: 0103
Revises: 0102
Create Date: 2026-08-16
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0103"
down_revision: Union[str, None] = "0102"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_CONSTRAINT = "ck_email_template_category"


def upgrade() -> None:
    # Drop first: the old constraint would reject the new values mid-update.
    op.drop_constraint(_CONSTRAINT, "email_template", type_="check")
    op.execute("UPDATE email_template SET category = 'general' WHERE category = 'broadcast'")
    op.execute("UPDATE email_template SET category = 'workshop' WHERE category = 'workshop_automation'")
    op.create_check_constraint(_CONSTRAINT, "email_template", "category IN ('general', 'workshop')")


def downgrade() -> None:
    op.drop_constraint(_CONSTRAINT, "email_template", type_="check")
    op.execute("UPDATE email_template SET category = 'broadcast' WHERE category = 'general'")
    op.execute("UPDATE email_template SET category = 'workshop_automation' WHERE category = 'workshop'")
    op.create_check_constraint(
        _CONSTRAINT, "email_template", "category IN ('broadcast', 'workshop_automation')"
    )
