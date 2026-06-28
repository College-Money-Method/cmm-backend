"""add app_config global settings table

Revision ID: 31765d344a94
Revises: 0068
Create Date: 2026-06-27 19:33:12.145525
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '31765d344a94'
down_revision: Union[str, None] = '0068'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'app_config',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('welcome_video_embed_code', sa.Text(), nullable=True),
        sa.Column('welcome_video_title', sa.Text(), nullable=True),
        sa.Column('welcome_video_caption', sa.Text(), nullable=True),
        sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )


def downgrade() -> None:
    op.drop_table('app_config')
