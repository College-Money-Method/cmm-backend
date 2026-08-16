"""add email_sandbox_mode to app_config

Adds the runtime email-sandbox flag to the global app config singleton. When on,
outbound email only reaches team-domain recipients; everyone else is logged, not
sent. Defaults to False (production sending). Replaces the former env-var guards
(email_send_enabled / email_sandbox_mode / email_sandbox_domain).

Revision ID: 0102
Revises: 0101
Create Date: 2026-08-16
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0102"
down_revision: Union[str, None] = "0101"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "app_config",
        sa.Column("email_sandbox_mode", sa.Boolean(), nullable=False, server_default="false"),
    )


def downgrade() -> None:
    op.drop_column("app_config", "email_sandbox_mode")
