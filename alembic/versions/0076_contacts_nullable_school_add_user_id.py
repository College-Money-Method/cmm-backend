"""contacts: nullable school_id (SET NULL), add user_id link to auth.users

Contacts become the source of truth for counselors:
- school_id nullable — a counselor may not be attached to a school yet
- deleting a school detaches contacts instead of deleting them
- user_id links a contact to its Supabase auth user (no cross-schema FK,
  same pattern as user_roles.user_id)

@76
@75
Create Date: 2026-07-05
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0076"
down_revision: Union[str, None] = "0075"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column("contacts", "school_id", existing_type=sa.Uuid(), nullable=True)
    op.drop_constraint("contacts_school_id_fkey", "contacts", type_="foreignkey")
    op.create_foreign_key(
        "contacts_school_id_fkey",
        "contacts",
        "schools",
        ["school_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.add_column("contacts", sa.Column("user_id", sa.Uuid(), nullable=True))
    op.create_index("uq_contacts_user_id", "contacts", ["user_id"], unique=True)

    # Backfill user_id from auth.users by email — only unambiguous matches
    # (an email owned by exactly one contact). Postgres-only: auth schema
    # is Supabase-specific and absent in unit-test databases.
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            """
            WITH email_counts AS (
                SELECT lower(trim(email)) AS em, count(*) AS n
                FROM contacts
                WHERE email IS NOT NULL AND trim(email) <> ''
                GROUP BY 1
            )
            UPDATE contacts c
            SET user_id = u.id
            FROM auth.users u, email_counts ec
            WHERE c.user_id IS NULL
              AND c.email IS NOT NULL
              AND lower(trim(c.email)) = lower(u.email)
              AND ec.em = lower(trim(c.email))
              AND ec.n = 1
            """
        )


def downgrade() -> None:
    op.drop_index("uq_contacts_user_id", table_name="contacts")
    op.drop_column("contacts", "user_id")
    op.drop_constraint("contacts_school_id_fkey", "contacts", type_="foreignkey")
    # Restore original CASCADE FK; NULL school_ids must be resolved manually first
    op.create_foreign_key(
        "contacts_school_id_fkey",
        "contacts",
        "schools",
        ["school_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.alter_column("contacts", "school_id", existing_type=sa.Uuid(), nullable=False)
