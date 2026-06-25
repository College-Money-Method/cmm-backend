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
    # Populate school_role from current role BEFORE changing the enum
    op.execute("ALTER TABLE user_roles ADD COLUMN school_role VARCHAR")
    op.execute("UPDATE user_roles SET school_role = 'Director' WHERE role = 'director'")
    op.execute("UPDATE user_roles SET school_role = 'Counselor' WHERE role = 'counselor'")

    # Create new enum
    op.execute("CREATE TYPE app_role_enum_v2 AS ENUM ('super_admin', 'hub_admin', 'hub_user', 'viewer')")

    # Add temp column, migrate data, then swap
    op.execute("ALTER TABLE user_roles ADD COLUMN role_new app_role_enum_v2")
    op.execute("""
        UPDATE user_roles SET role_new = CASE
            WHEN role = 'super_admin' THEN 'super_admin'::app_role_enum_v2
            WHEN role = 'director' THEN 'hub_admin'::app_role_enum_v2
            WHEN role = 'counselor' AND hub_permission = 'admin' THEN 'hub_admin'::app_role_enum_v2
            WHEN role = 'counselor' AND hub_permission = 'user' THEN 'hub_user'::app_role_enum_v2
            WHEN role = 'counselor' THEN 'hub_user'::app_role_enum_v2
            WHEN role = 'viewer' THEN 'viewer'::app_role_enum_v2
            ELSE 'hub_user'::app_role_enum_v2
        END
    """)
    op.execute("ALTER TABLE user_roles ALTER COLUMN role_new SET NOT NULL")
    op.execute("ALTER TABLE user_roles ALTER COLUMN role_new SET DEFAULT 'hub_user'::app_role_enum_v2")
    op.execute("ALTER TABLE user_roles DROP COLUMN role")
    op.execute("ALTER TABLE user_roles RENAME COLUMN role_new TO role")
    op.execute("DROP TYPE app_role_enum")
    op.execute("ALTER TYPE app_role_enum_v2 RENAME TO app_role_enum")

    # Drop hub_permission
    op.execute("ALTER TABLE user_roles DROP COLUMN hub_permission")
    op.execute("DROP TYPE hub_permission_enum")


def downgrade() -> None:
    pass
