"""auth: add profiles table mirroring Supabase auth.users

Denormalises email + name from Supabase auth.users into a local, person-centric
``profiles`` table so counselor search/pagination run as one indexed SQL join
instead of enumerating the entire Supabase Auth directory per request.

Backfills existing role-holders from auth.users (same Postgres instance).

Revision ID: 0079
Revises: 0078
Create Date: 2026-07-14
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0079"
down_revision: Union[str, None] = "0078"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "profiles",
        sa.Column("user_id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("email", sa.String(), nullable=False),
        sa.Column("first_name", sa.String(), nullable=True),
        sa.Column("last_name", sa.String(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_profiles_email", "profiles", ["email"])

    # Backfill from Supabase auth.users (same DB) for everyone who already holds
    # a role. New accounts stay in sync via src/auth/profile_sync.upsert_profile.
    op.execute(
        """
        INSERT INTO profiles (user_id, email, first_name, last_name, updated_at)
        SELECT u.id,
               COALESCE(u.email, ''),
               NULLIF(u.raw_user_meta_data->>'first_name', ''),
               NULLIF(u.raw_user_meta_data->>'last_name', ''),
               now()
        FROM auth.users u
        JOIN user_roles ur ON ur.user_id = u.id
        ON CONFLICT (user_id) DO NOTHING;
        """
    )


def downgrade() -> None:
    op.drop_index("ix_profiles_email", table_name="profiles")
    op.drop_table("profiles")
