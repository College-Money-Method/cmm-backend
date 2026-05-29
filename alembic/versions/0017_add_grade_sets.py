"""Add grade_sets table and link to grade_configs and schools.

@17
@16
Create Date: 2026-04-12
"""

from alembic import op
import sqlalchemy as sa

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Create grade_sets table
    op.create_table(
        "grade_sets",
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("is_default", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
    )

    # 2. Insert default grade set and capture its ID
    conn = op.get_bind()
    result = conn.execute(
        sa.text(
            "INSERT INTO grade_sets (name, description, is_default) "
            "VALUES (:name, :desc, true) RETURNING id"
        ),
        {"name": "Standard (9th–12th Grade)", "desc": "Default grade set for 9th through 12th grade."},
    )
    default_id = result.scalar()

    # 3. Add grade_set_id column to grade_configs (nullable first for backfill)
    op.add_column(
        "grade_configs",
        sa.Column("grade_set_id", sa.Uuid(), nullable=True),
    )

    # 4. Backfill existing rows
    conn.execute(
        sa.text("UPDATE grade_configs SET grade_set_id = :id"),
        {"id": default_id},
    )

    # 5. Set NOT NULL now that all rows are filled
    op.alter_column("grade_configs", "grade_set_id", nullable=False)

    # 6. Add FK constraint
    op.create_foreign_key(
        "fk_grade_configs_grade_set_id",
        "grade_configs",
        "grade_sets",
        ["grade_set_id"],
        ["id"],
        ondelete="RESTRICT",
    )

    # 7. Drop old unique constraint on grade alone, add composite unique
    op.drop_constraint("grade_configs_grade_key", "grade_configs", type_="unique")
    op.create_unique_constraint(
        "uq_grade_configs_grade_set_grade",
        "grade_configs",
        ["grade_set_id", "grade"],
    )

    # 8. Index on grade_set_id for fast lookups
    op.create_index("idx_grade_configs_grade_set_id", "grade_configs", ["grade_set_id"])

    # 9. Add grade_set_id to schools table
    op.add_column(
        "schools",
        sa.Column("grade_set_id", sa.Uuid(), nullable=True),
    )
    op.create_foreign_key(
        "fk_schools_grade_set_id",
        "schools",
        "grade_sets",
        ["grade_set_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("idx_schools_grade_set_id", "schools", ["grade_set_id"])


def downgrade() -> None:
    # Reverse schools changes
    op.drop_index("idx_schools_grade_set_id", table_name="schools")
    op.drop_constraint("fk_schools_grade_set_id", "schools", type_="foreignkey")
    op.drop_column("schools", "grade_set_id")

    # Reverse grade_configs changes
    op.drop_index("idx_grade_configs_grade_set_id", table_name="grade_configs")
    op.drop_constraint("uq_grade_configs_grade_set_grade", "grade_configs", type_="unique")
    op.create_unique_constraint("grade_configs_grade_key", "grade_configs", ["grade"])
    op.drop_constraint("fk_grade_configs_grade_set_id", "grade_configs", type_="foreignkey")
    op.drop_column("grade_configs", "grade_set_id")

    # Drop grade_sets table
    op.drop_table("grade_sets")
