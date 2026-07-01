"""Shared PostgreSQL enum types used across feature models."""

import enum


class SalesStatus(str, enum.Enum):
    PROSPECT = "Prospect"
    PROPOSAL_SENT = "Proposal Sent"
    PROPOSAL_ACCEPTED = "Proposal Accepted"
    PROPOSAL_REJECTED = "Proposal Rejected"
    CONTRACT_SIGNED = "Contract Signed"
    NOT_MOVING_FORWARD = "Not Moving Forward"
    CURRENT_CUSTOMER = "Current Customer"


class ProposalType(str, enum.Enum):
    FIXED = "Fixed"
    VARIABLE = "Variable"


class RegistrationStatus(str, enum.Enum):
    APPROVED = "approved"
    PENDING = "pending"
    DENIED = "denied"


class CycleStatus(str, enum.Enum):
    CURRENT = "Current"
    NEXT = "Next"
    ARCHIVE = "Archive"


class AppRole(str, enum.Enum):
    SUPER_ADMIN = "super_admin"
    HUB_ADMIN = "hub_admin"
    HUB_USER = "hub_user"
    VIEWER = "viewer"
