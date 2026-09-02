"""Pydantic schemas for auth/role management."""

import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from src.schools.display_timezone import DisplayTimezoneField


class UserRoleOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    user_id: uuid.UUID
    role: Literal["super_admin", "hub_admin", "hub_user", "viewer"]
    school_id: uuid.UUID | None = None
    school_role: str | None = None  # cosmetic Airtable title, e.g. "Director"
    # Hub display preference from the signed-in user's Contact row. None = read
    # workshop times on the browser's own clock. Screen-only — see
    # `MePreferencesUpdate`.
    timezone: str | None = None


class MePreferencesUpdate(BaseModel):
    """Settings a signed-in hub user changes for themselves alone.

    Distinct from `ContactUpdate`, which is the teammate-management endpoint and
    carries fields a director may set on somebody else. Nothing here can affect
    another person, and nothing here changes what is emailed to families:
    `timezone` moves the clock on this user's own Hub screen and nothing more.
    """

    # None is a real value (clear the preference, fall back to the browser), so
    # the caller must send the field explicitly to change anything.
    timezone: DisplayTimezoneField = None


class CurrentUser(BaseModel):
    user_id: uuid.UUID
    role: Literal["super_admin", "hub_admin", "hub_user", "viewer"]
    school_id: uuid.UUID | None = None
    school_role: str | None = None  # cosmetic Airtable title, e.g. "Director"
    # The email on the Supabase auth user (what the person logs in with). Used
    # as the test-send fallback target when the admin has no Contact row.
    email: str | None = None


class ContactCreate(BaseModel):
    email: EmailStr
    # Optional: directors can "just add the email" — name defaults to the email handle
    first_name: str | None = None
    last_name: str | None = None
    # Optional: hub_admin (director) creations are forced to the director's own school
    school_id: uuid.UUID | None = None
    # "no_access" creates a contact record only — no Supabase login / UserRole.
    role: Literal["hub_admin", "hub_user", "viewer", "no_access"] = "hub_user"
    title: str | None = None
    # If provided, used as initial password. Otherwise defaults to the email handle +
    # the school's resource-center password (falls back to Supabase invite if unset).
    password: str | None = None


class ContactUpdate(BaseModel):
    first_name: str | None = None
    last_name: str | None = None
    school_id: uuid.UUID | None = None
    role: Literal["hub_admin", "hub_user", "viewer"] | None = None
    title: str | None = None
    # Self-service email opt-ins — any hub user may toggle these on their OWN
    # contact row, independent of their access role. The two are independent:
    # `auto_emails` covers scheduler-driven workshop updates, `broadcast_emails`
    # covers one-off program announcements.
    auto_emails: bool | None = None
    broadcast_emails: bool | None = None


class ContactOut(BaseModel):
    # Contacts are sourced from the contacts table. A contact may not have a
    # provisioned login yet (no school → no auth user/role), so user_id/role are
    # optional; `id` is the stable contact id used by the UI.
    id: uuid.UUID | None = None
    user_id: uuid.UUID | None = None
    email: str | None = None
    first_name: str | None
    last_name: str | None
    full_name: str | None
    role: str | None = None
    school_id: uuid.UUID | None = None
    school_name: str | None = None
    title: str | None = None
    school_role: str | None = None
    # The email on the Supabase auth user — i.e. what the person actually types to
    # log in. Only resolved by the single-contact detail endpoint (one admin API
    # call per contact); always None on list rows. Diverges from `email` when
    # Airtable renamed the contact after provisioning: the sync updates
    # contacts.email but never auth.users.email, so login keeps using the old
    # address. The detail UI surfaces the mismatch + a "match auth email" action.
    auth_email: str | None = None
    # The two email opt-ins. Default False on rows built from a bare role record
    # (no Contact object, e.g. right after create-contact) — see
    # `_contact_out_from_role`.
    auto_emails: bool = False
    broadcast_emails: bool = False


class ContactListResponse(BaseModel):
    items: list[ContactOut]
    total: int
    skip: int
    limit: int


class HubPasswordResetRequest(BaseModel):
    """Reset a contact's hub password. Omit `password` to reset to the default."""

    password: str | None = None


class HubPasswordResetOut(BaseModel):
    """Result of resetting a contact's hub login password."""

    password: str


class AuthEmailSyncOut(BaseModel):
    """Result of pointing a contact's Supabase auth user at the contact's email."""

    # False when the two were already identical (nothing was written).
    updated: bool
    # The email the auth user had before the call (None when it had none).
    previous_email: str | None = None
    # The auth user's email after the call — matches the contact email on success.
    auth_email: str


class ChangePasswordRequest(BaseModel):
    """Change the current user's own hub password (verifies the current password)."""

    current_password: str = Field(min_length=1)
    new_password: str = Field(min_length=8)


class CheckEmailRequest(BaseModel):
    """Look up whether an email belongs to a registered user (forgot-password warn)."""

    email: EmailStr


class CheckEmailOut(BaseModel):
    # Intentionally exposes account existence so the reset form can warn on a typo.
    # See the check_email endpoint for the enumeration trade-off + rate limiting.
    exists: bool


class ContactSyncResult(BaseModel):
    contacts_created: int = 0
    contacts_updated: int = 0
    contacts_deactivated: int = 0
    contacts_reactivated: int = 0
    email_collisions: int = 0
    counselors_created: int
    school_roles_updated: int
    counselors_revoked: int = 0
    skipped: int
    synced_at: str
