"""Refactor role enum (hub_admin/hub_user) and add school_role column.

@67
@66
Create Date: 2026-06-25
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0067"
down_revision: Union[str, None] = "0066"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Populate school_role from current role BEFORE changing the enum.
    # Use ::text cast so the comparison works regardless of current enum values
    # (dev DBs created fresh already have hub_admin/hub_user, not director/counselor).
    op.execute("ALTER TABLE user_roles ADD COLUMN IF NOT EXISTS school_role VARCHAR")
    op.execute("UPDATE user_roles SET school_role = 'Director' WHERE role::text = 'director'")
    op.execute("UPDATE user_roles SET school_role = 'Counselor' WHERE role::text = 'counselor'")

    # Create new enum (idempotent — fresh DBs may already have it)
    op.execute("""
        DO $$ BEGIN
            CREATE TYPE app_role_enum_v2 AS ENUM ('super_admin', 'hub_admin', 'hub_user', 'viewer');
        EXCEPTION WHEN duplicate_object THEN NULL;
        END $$
    """)

    # Add temp column and migrate data.
    # Handle both legacy (director/counselor) and already-migrated (hub_admin/hub_user) values.
    # hub_permission column may not exist on fresh DBs, so handle it conditionally.
    op.execute("ALTER TABLE user_roles ADD COLUMN IF NOT EXISTS role_new app_role_enum_v2")
    op.execute("""
        UPDATE user_roles SET role_new = CASE
            WHEN role::text = 'super_admin' THEN 'super_admin'::app_role_enum_v2
            WHEN role::text IN ('hub_admin', 'director') THEN 'hub_admin'::app_role_enum_v2
            WHEN role::text = 'hub_user'   THEN 'hub_user'::app_role_enum_v2
            WHEN role::text = 'viewer'     THEN 'viewer'::app_role_enum_v2
            ELSE 'hub_user'::app_role_enum_v2
        END
    """)
    # Override counselor → hub_admin when hub_permission = 'admin' (legacy DBs only)
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'user_roles' AND column_name = 'hub_permission'
            ) THEN
                UPDATE user_roles SET role_new = 'hub_admin'::app_role_enum_v2
                WHERE role::text = 'counselor' AND hub_permission::text = 'admin';
            END IF;
        END $$
    """)
    op.execute("ALTER TABLE user_roles ALTER COLUMN role_new SET NOT NULL")
    op.execute("ALTER TABLE user_roles ALTER COLUMN role_new SET DEFAULT 'hub_user'::app_role_enum_v2")
    op.execute("ALTER TABLE user_roles DROP COLUMN role")
    op.execute("ALTER TABLE user_roles RENAME COLUMN role_new TO role")
    op.execute("DROP TYPE IF EXISTS app_role_enum")
    op.execute("ALTER TYPE app_role_enum_v2 RENAME TO app_role_enum")

    # Drop hub_permission (may not exist on fresh DBs)
    op.execute("ALTER TABLE user_roles DROP COLUMN IF EXISTS hub_permission")
    op.execute("""
        DO $$ BEGIN
            DROP TYPE hub_permission_enum;
        EXCEPTION WHEN undefined_object THEN NULL;
        END $$
    """)


def downgrade() -> None:
    pass
