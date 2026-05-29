"""Add workshop body, make webinar cohort/cycle nullable, drop unique constraint.

@21
@19
Create Date: 2026-04-16
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "0021"
down_revision = "0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add body column to workshops
    op.add_column("workshops", sa.Column("body", sa.Text(), nullable=True))

    # Make cohort_id and cycle_id nullable on webinars
    op.alter_column("webinars", "cohort_id", existing_type=sa.Uuid(), nullable=True)
    op.alter_column("webinars", "cycle_id", existing_type=sa.Uuid(), nullable=True)

    # Drop the unique constraint (no longer needed, incompatible with NULLs)
    op.drop_constraint("uq_webinar_workshop_cohort_cycle", "webinars", type_="unique")


def downgrade() -> None:
    # Restore unique constraint (only safe if no NULLs exist)
    op.create_unique_constraint(
        "uq_webinar_workshop_cohort_cycle",
        "webinars",
        ["workshop_id", "cohort_id", "cycle_id"],
    )

    # Restore NOT NULL on cohort_id and cycle_id
    op.alter_column("webinars", "cycle_id", existing_type=sa.Uuid(), nullable=False)
    op.alter_column("webinars", "cohort_id", existing_type=sa.Uuid(), nullable=False)

    # Drop body column from workshops
    op.drop_column("workshops", "body")
