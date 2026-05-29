"""Add user_roles table for RBAC.

@02
@01
Create Date: 2026-03-18 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Create the app_role enum type
    app_role_enum = postgresql.ENUM(
        "super_admin", "counselor", "viewer",
        name="app_role_enum",
        create_type=True,
    )
    app_role_enum.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "user_roles",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "role",
            postgresql.ENUM("super_admin", "counselor", "viewer", name="app_role_enum", create_type=False),
            nullable=False,
            server_default="counselor",
        ),
        sa.Column(
            "school_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("schools.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint("user_id", name="uq_user_roles_user_id"),
    )
    op.create_index("idx_user_roles_user_id", "user_roles", ["user_id"])
    op.create_index("idx_user_roles_school_id", "user_roles", ["school_id"])


def downgrade() -> None:
    op.drop_index("idx_user_roles_school_id", table_name="user_roles")
    op.drop_index("idx_user_roles_user_id", table_name="user_roles")
    op.drop_table("user_roles")
    op.execute("DROP TYPE IF EXISTS app_role_enum")
