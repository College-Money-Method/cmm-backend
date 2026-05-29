"""initial schema — drop legacy tables and create fresh CMM schema.

@01
Revises:
Create Date: 2026-03-16 12:10:48.248284
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Old tables to drop (order: junctions first, then entities)
OLD_TABLES = [
    "assets_objectives", "objectives_assets", "objectives_workshop",
    "workshop_objectives", "assets_workshop", "assets_tags", "tags_assets",
    "assets_state", "state_assets", "assets_asset_type", "asset_type_assets",
    "assets_cycle", "cycle_assets", "cohorts_webinars", "webinars_cohorts",
    "cohorts_schools", "schools_cohorts", "webinars_schools", "schools_webinars",
    "webinars_cycle", "cycle_webinars", "webinars_workshop", "workshop_webinars",
    "synced_assets_workshop", "workshop_synced_assets",
    "synced_assets_cycle", "cycle_synced_assets",
    "objectives", "tags", "state", "asset_type",
    "synced_assets", "suggested_grades",
    "workshop", "cycle", "schools_legacy",
    # existing CMM tables that will be recreated
    "workshop_assets", "assets", "webinars", "schools", "cohorts",
]


def upgrade() -> None:
    # --- Phase 1: drop every old table (CASCADE to handle FK deps) ----------
    conn = op.get_bind()
    for tbl in OLD_TABLES:
        conn.execute(sa.text(f'DROP TABLE IF EXISTS "{tbl}" CASCADE'))

    # --- Phase 2: create enums -----------------------------------------------
    op.execute("DROP TYPE IF EXISTS sales_status_enum")
    op.execute("DROP TYPE IF EXISTS proposal_type_enum")
    op.execute("DROP TYPE IF EXISTS registration_status_enum")

    sa.Enum(
        "Prospect", "Proposal Sent", "Proposal Accepted", "Proposal Rejected",
        "Contract Signed", "Not Moving Forward", "Current Customer",
        name="sales_status_enum",
    ).create(op.get_bind())
    sa.Enum("Fixed", "Variable", name="proposal_type_enum").create(op.get_bind())
    sa.Enum("approved", "pending", "denied", name="registration_status_enum").create(op.get_bind())

    # --- Phase 3: create all new tables --------------------------------------

    # Standalone tables first
    op.create_table("cycles",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("beginning_date", sa.Date(), nullable=True),
        sa.Column("end_date", sa.Date(), nullable=True),
        sa.Column("is_current", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("next_cycle_id", sa.Uuid(), nullable=True),
        sa.Column("prev_cycle_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["next_cycle_id"], ["cycles.id"]),
        sa.ForeignKeyConstraint(["prev_cycle_id"], ["cycles.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )
    op.create_index("idx_cycles_one_current", "cycles", ["is_current"], unique=True,
                    postgresql_where=sa.text("is_current = true"))

    op.create_table("cohorts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("hide_unavailability_calendar", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )

    op.create_table("workshops",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("key_actions", sa.Text(), nullable=True),
        sa.Column("sequence_number", sa.Integer(), nullable=True),
        sa.Column("suggested_grades", sa.Text(), nullable=True),
        sa.Column("resource_center_slug", sa.Text(), nullable=True),
        sa.Column("workshop_art_url", sa.Text(), nullable=True),
        sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("resource_center_slug"),
        sa.UniqueConstraint("sequence_number"),
    )

    op.create_table("paul_martin_calendar",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("start_datetime", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("end_datetime", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("google_event_id", sa.Text(), nullable=True),
        sa.Column("event_link", sa.Text(), nullable=True),
        sa.Column("hangouts_link", sa.Text(), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("location", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=True),
        sa.Column("creator", sa.Text(), nullable=True),
        sa.Column("is_recurring", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("is_all_day", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("google_updated_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("google_event_id"),
    )

    op.create_table("settings",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("action", sa.Text(), nullable=True),
        sa.Column("days_prior", sa.Integer(), nullable=True),
        sa.Column("trigger_url", sa.Text(), nullable=True),
        sa.Column("webhook_url", sa.Text(), nullable=True),
        sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )

    # Tables with FK to cohorts/cycles
    op.create_table("schools",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("street_address", sa.Text(), nullable=True),
        sa.Column("city", sa.Text(), nullable=True),
        sa.Column("state", sa.String(length=2), nullable=True),
        sa.Column("zip_code", sa.Text(), nullable=True),
        sa.Column("enrollment_9_12", sa.Integer(), nullable=True),
        sa.Column("enrollment_range", sa.Text(),
                  sa.Computed("CASE WHEN enrollment_9_12 IS NULL THEN NULL "
                              "WHEN enrollment_9_12 < 250 THEN '< 250' "
                              "WHEN enrollment_9_12 <= 500 THEN '250 - 500' "
                              "ELSE '> 500' END"), nullable=True),
        sa.Column("cmm_website_password", sa.Text(), nullable=True),
        sa.Column("slug", sa.Text(), nullable=True),
        sa.Column("school_resource_center_url", sa.Text(), nullable=True),
        sa.Column("appointlet_link", sa.Text(), nullable=True),
        sa.Column("calendar_link", sa.Text(), nullable=True),
        sa.Column("logo_url", sa.Text(), nullable=True),
        sa.Column("is_current_customer", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("cohort_id", sa.Uuid(), nullable=True),
        sa.Column("bubble_rec_id", sa.Text(), nullable=True),
        sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["cohort_id"], ["cohorts.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("slug"),
    )
    op.create_index("idx_schools_cohort_id", "schools", ["cohort_id"])
    op.create_index("idx_schools_slug", "schools", ["slug"])

    op.create_table("contacts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("school_id", sa.Uuid(), nullable=False),
        sa.Column("first_name", sa.Text(), nullable=True),
        sa.Column("last_name", sa.Text(), nullable=True),
        sa.Column("full_name", sa.Text(),
                  sa.Computed("TRIM(COALESCE(first_name, '') || ' ' || COALESCE(last_name, ''))"), nullable=True),
        sa.Column("email", sa.Text(), nullable=True),
        sa.Column("role", sa.Text(), nullable=True),
        sa.Column("magic_link", sa.Text(), nullable=True),
        sa.Column("receive_comms", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("auto_emails", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("softr_access", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["school_id"], ["schools.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_contacts_school_id", "contacts", ["school_id"])

    op.create_table("webinars",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workshop_id", sa.Uuid(), nullable=False),
        sa.Column("cohort_id", sa.Uuid(), nullable=False),
        sa.Column("cycle_id", sa.Uuid(), nullable=False),
        sa.Column("webinar_name", sa.Text(), nullable=True),
        sa.Column("zoom_webinar_id", sa.Text(), nullable=True),
        sa.Column("start_datetime", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("end_datetime", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("duration_minutes", sa.Integer(),
                  sa.Computed("CASE WHEN start_datetime IS NOT NULL AND end_datetime IS NOT NULL "
                              "THEN EXTRACT(EPOCH FROM (end_datetime - start_datetime))::INTEGER / 60 "
                              "ELSE NULL END"), nullable=True),
        sa.Column("join_url", sa.Text(), nullable=True),
        sa.Column("start_url", sa.Text(), nullable=True),
        sa.Column("registration_url", sa.Text(), nullable=True),
        sa.Column("zoom_link", sa.Text(), nullable=True),
        sa.Column("video_embed_code", sa.Text(), nullable=True),
        sa.Column("audio_transcript", sa.Text(), nullable=True),
        sa.Column("track_registrations", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["workshop_id"], ["workshops.id"]),
        sa.ForeignKeyConstraint(["cohort_id"], ["cohorts.id"]),
        sa.ForeignKeyConstraint(["cycle_id"], ["cycles.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workshop_id", "cohort_id", "cycle_id", name="uq_webinar_workshop_cohort_cycle"),
        sa.UniqueConstraint("zoom_webinar_id"),
    )
    op.create_index("idx_webinars_workshop_id", "webinars", ["workshop_id"])
    op.create_index("idx_webinars_cohort_id", "webinars", ["cohort_id"])
    op.create_index("idx_webinars_cycle_id", "webinars", ["cycle_id"])
    op.create_index("idx_webinars_start_datetime", "webinars", ["start_datetime"])

    op.create_table("workshop_registrations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("webinar_id", sa.Uuid(), nullable=False),
        sa.Column("school_id", sa.Uuid(), nullable=True),
        sa.Column("first_name", sa.Text(), nullable=True),
        sa.Column("last_name", sa.Text(), nullable=True),
        sa.Column("full_name", sa.Text(),
                  sa.Computed("TRIM(COALESCE(first_name, '') || ' ' || COALESCE(last_name, ''))"), nullable=True),
        sa.Column("email", sa.Text(), nullable=False),
        sa.Column("grade", sa.Text(), nullable=True),
        sa.Column("status", postgresql.ENUM("approved", "pending", "denied",
                                             name="registration_status_enum", create_type=False),
                  server_default="approved", nullable=False),
        sa.Column("attended", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("join_time", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("leave_time", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("zoom_registrant_id", sa.Text(), nullable=True),
        sa.Column("questions", sa.Text(), nullable=True),
        sa.Column("registration_time", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["webinar_id"], ["webinars.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["school_id"], ["schools.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_workshop_reg_webinar_id", "workshop_registrations", ["webinar_id"])
    op.create_index("idx_workshop_reg_school_id", "workshop_registrations", ["school_id"])
    op.create_index("idx_workshop_reg_email", "workshop_registrations", ["email"])

    op.create_table("portal_mapping",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("school_id", sa.Uuid(), nullable=False),
        sa.Column("webinar_id", sa.Uuid(), nullable=False),
        sa.Column("pre_webinar_reminder_sent_on", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("post_webinar_update_sent_on", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("show_zoom", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["school_id"], ["schools.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["webinar_id"], ["webinars.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("school_id", "webinar_id", name="uq_portal_mapping_school_webinar"),
    )
    op.create_index("idx_portal_mapping_school_id", "portal_mapping", ["school_id"])
    op.create_index("idx_portal_mapping_webinar_id", "portal_mapping", ["webinar_id"])

    op.create_table("sales",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("school_id", sa.Uuid(), nullable=False),
        sa.Column("cycle_id", sa.Uuid(), nullable=False),
        sa.Column("contract_signatory_id", sa.Uuid(), nullable=True),
        sa.Column("status", postgresql.ENUM(
            "Prospect", "Proposal Sent", "Proposal Accepted", "Proposal Rejected",
            "Contract Signed", "Not Moving Forward", "Current Customer",
            name="sales_status_enum", create_type=False),
            server_default="Prospect", nullable=False),
        sa.Column("proposal_type", postgresql.ENUM("Fixed", "Variable",
                                                     name="proposal_type_enum", create_type=False), nullable=True),
        sa.Column("contract_url", sa.Text(), nullable=True),
        sa.Column("contract_doc_id", sa.Text(), nullable=True),
        sa.Column("proposal_url", sa.Text(), nullable=True),
        sa.Column("proposal_doc_id", sa.Text(), nullable=True),
        sa.Column("contract_signed_date", sa.Date(), nullable=True),
        sa.Column("contract_sent_date", sa.Date(), nullable=True),
        sa.Column("contract_created_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("proposal_sent_date", sa.Date(), nullable=True),
        sa.Column("proposal_accepted", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("proposal_rejected", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("fixed_cost", sa.Numeric(precision=10, scale=2), nullable=True),
        sa.Column("signed_revenue", sa.Numeric(precision=10, scale=2), nullable=True),
        sa.Column("revenue_potential", sa.Numeric(precision=10, scale=2), nullable=True),
        sa.Column("contract_rate", sa.Numeric(precision=10, scale=2), nullable=True),
        sa.Column("hours_contracted_1on1", sa.Numeric(precision=5, scale=1), nullable=True),
        sa.Column("payments_received", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("enrollment_at_signing", sa.Integer(), nullable=True),
        sa.Column("wp_updated", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["school_id"], ["schools.id"]),
        sa.ForeignKeyConstraint(["cycle_id"], ["cycles.id"]),
        sa.ForeignKeyConstraint(["contract_signatory_id"], ["contacts.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_sales_school_id", "sales", ["school_id"])
    op.create_index("idx_sales_cycle_id", "sales", ["cycle_id"])
    op.create_index("idx_sales_status", "sales", ["status"])

    op.create_table("invoices",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("sales_id", sa.Uuid(), nullable=False),
        sa.Column("amount", sa.Numeric(precision=10, scale=2), nullable=True),
        sa.Column("issued_date", sa.Date(), nullable=True),
        sa.Column("due_date", sa.Date(), nullable=True),
        sa.Column("paid_date", sa.Date(), nullable=True),
        sa.Column("status", sa.Text(), nullable=True),
        sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["sales_id"], ["sales.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table("assets",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("file_link", sa.Text(), nullable=True),
        sa.Column("attachment_url", sa.Text(), nullable=True),
        sa.Column("asset_date", sa.Date(), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("cycle_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["cycle_id"], ["cycles.id"]),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table("workshop_assets",
        sa.Column("workshop_id", sa.Uuid(), nullable=False),
        sa.Column("asset_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(["workshop_id"], ["workshops.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["asset_id"], ["assets.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("workshop_id", "asset_id"),
    )

    op.create_table("one_on_one_meetings",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("school_id", sa.Uuid(), nullable=True),
        sa.Column("cycle_id", sa.Uuid(), nullable=True),
        sa.Column("first_name", sa.Text(), nullable=True),
        sa.Column("last_name", sa.Text(), nullable=True),
        sa.Column("full_name", sa.Text(),
                  sa.Computed("TRIM(COALESCE(first_name, '') || ' ' || COALESCE(last_name, ''))"), nullable=True),
        sa.Column("email", sa.Text(), nullable=True),
        sa.Column("grade", sa.Text(), nullable=True),
        sa.Column("scheduled_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("status", sa.Text(), nullable=True),
        sa.Column("meeting_goals", sa.Text(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("college_list", sa.Text(), nullable=True),
        sa.Column("conference_url", sa.Text(), nullable=True),
        sa.Column("is_school_sponsored", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("is_invoiced", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("ai_meeting_summary", sa.Text(), nullable=True),
        sa.Column("reminder_1_sent_on", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("reminder_2_sent_on", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["school_id"], ["schools.id"]),
        sa.ForeignKeyConstraint(["cycle_id"], ["cycles.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_1on1_school_id", "one_on_one_meetings", ["school_id"])
    op.create_index("idx_1on1_cycle_id", "one_on_one_meetings", ["cycle_id"])

    op.create_table("school_date_selector",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("school_id", sa.Uuid(), nullable=False),
        sa.Column("workshop_id", sa.Uuid(), nullable=True),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["school_id"], ["schools.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["workshop_id"], ["workshops.id"]),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("school_date_selector")
    op.drop_table("one_on_one_meetings")
    op.drop_table("workshop_assets")
    op.drop_table("assets")
    op.drop_table("invoices")
    op.drop_table("sales")
    op.drop_table("portal_mapping")
    op.drop_table("workshop_registrations")
    op.drop_table("webinars")
    op.drop_table("contacts")
    op.drop_table("schools")
    op.drop_table("paul_martin_calendar")
    op.drop_table("settings")
    op.drop_table("workshops")
    op.drop_table("cohorts")
    op.drop_table("cycles")
    sa.Enum(name="sales_status_enum").drop(op.get_bind())
    sa.Enum(name="proposal_type_enum").drop(op.get_bind())
    sa.Enum(name="registration_status_enum").drop(op.get_bind())
