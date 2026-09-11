"""Vimeo folder an audit run uploads into, editable by an admin.

An audit run exists to be watched in Vimeo before anyone trusts the pipeline
with a school's page, and which folder it lands in is an operator's decision —
a reviewer may want this week's runs kept apart from last month's. Until now the
folder came only from the environment, so changing it meant a deploy.

Nullable, meaning "fall back to the env seed" exactly as
``workshop_display_timezone`` does. A blank value is therefore clearable rather
than a configured empty folder, and every existing install keeps uploading where
its environment already points.

Revision ID: 0123
Revises: 0122
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0123"
down_revision: Union[str, None] = "0122"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("app_config", sa.Column("vimeo_audit_folder_uri", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("app_config", "vimeo_audit_folder_uri")
