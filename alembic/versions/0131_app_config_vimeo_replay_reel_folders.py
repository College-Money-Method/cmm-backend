"""Vimeo folders production replays and trailer reels upload into, editable by an admin.

Same shape as the audit folder (0123): nullable, meaning "fall back to the env
seed", so a blank value clears the override and an install with no value keeps
uploading into the library root as before.

Revision ID: 0131
Revises: 0130
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0131"
down_revision: Union[str, None] = "0130"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("app_config", sa.Column("vimeo_replay_folder_uri", sa.Text(), nullable=True))
    op.add_column("app_config", sa.Column("vimeo_reel_folder_uri", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("app_config", "vimeo_reel_folder_uri")
    op.drop_column("app_config", "vimeo_replay_folder_uri")
