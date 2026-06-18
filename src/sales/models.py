"""SQLAlchemy models for sales and invoices."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Boolean, Date, Enum, ForeignKey, Index, Integer, Numeric, Text, Uuid
from sqlalchemy.dialects.postgresql import TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from src.db.base import Base
from src.db.enums import ProposalType, SalesStatus


class Sale(Base):
    __tablename__ = "sales"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    airtable_id: Mapped[str | None] = mapped_column(Text, unique=True, index=True)
    school_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("schools.id"), nullable=False)
    cycle_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("cycles.id"), nullable=False)
    contract_signatory_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("contacts.id"))
    status: Mapped[SalesStatus] = mapped_column(
        Enum(SalesStatus, name="sales_status_enum", create_constraint=True, values_callable=lambda e: [m.value for m in e]),
        nullable=False,
        default=SalesStatus.PROSPECT,
        server_default="Prospect",
    )
    proposal_type: Mapped[ProposalType | None] = mapped_column(
        Enum(ProposalType, name="proposal_type_enum", create_constraint=True, values_callable=lambda e: [m.value for m in e]),
    )
    contract_url: Mapped[str | None] = mapped_column(Text)
    contract_doc_id: Mapped[str | None] = mapped_column(Text)
    proposal_url: Mapped[str | None] = mapped_column(Text)
    proposal_doc_id: Mapped[str | None] = mapped_column(Text)
    contract_signed_date: Mapped[date | None] = mapped_column(Date)
    contract_sent_date: Mapped[date | None] = mapped_column(Date)
    contract_created_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    proposal_sent_date: Mapped[date | None] = mapped_column(Date)
    proposal_accepted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    proposal_rejected: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    fixed_cost: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    signed_revenue: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    revenue_potential: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    contract_rate: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    hours_contracted_1on1: Mapped[Decimal | None] = mapped_column(Numeric(5, 1))
    payments_received: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    enrollment_at_signing: Mapped[int | None] = mapped_column(Integer)
    wp_updated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())

    school: Mapped[School] = relationship(back_populates="sales")
    cycle: Mapped[Cycle] = relationship(back_populates="sales")
    contract_signatory: Mapped[Contact | None] = relationship()
    invoices: Mapped[list[Invoice]] = relationship(back_populates="sale")

    __table_args__ = (
        Index("idx_sales_school_id", "school_id"),
        Index("idx_sales_cycle_id", "cycle_id"),
        Index("idx_sales_status", "status"),
    )


class Invoice(Base):
    __tablename__ = "invoices"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    sales_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("sales.id", ondelete="CASCADE"), nullable=False)
    amount: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    issued_date: Mapped[date | None] = mapped_column(Date)
    due_date: Mapped[date | None] = mapped_column(Date)
    paid_date: Mapped[date | None] = mapped_column(Date)
    status: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())

    sale: Mapped[Sale] = relationship(back_populates="invoices")
