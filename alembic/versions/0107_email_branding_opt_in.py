"""opt-in CMM branding on email templates and broadcasts

Adds ``email_template.include_branding`` and ``broadcast.include_branding``.
The email shell now defaults to a plain, Gmail-like message (no logo, no card,
no branded footer); a template opts back into the branded shell, and a
broadcast copies that choice from whichever template prefilled it.

Defaults to False so every existing row keeps rendering through the new plain
shell — that is the intended new default, not a regression.

Revision ID: 0107
Revises: 0106
Create Date: 2026-08-26
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0107"
down_revision: Union[str, None] = "0106"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "email_template",
        sa.Column("include_branding", sa.Boolean(), nullable=False, server_default="false"),
    )
    op.add_column(
        "broadcast",
        sa.Column("include_branding", sa.Boolean(), nullable=False, server_default="false"),
    )


def downgrade() -> None:
    op.drop_column("broadcast", "include_branding")
    op.drop_column("email_template", "include_branding")
