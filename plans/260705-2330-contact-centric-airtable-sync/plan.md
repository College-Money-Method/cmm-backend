# Contact-Centric Airtable Sync Refactor

## Context

- `contacts` table becomes source of truth for counselors (Counselors page → Contacts page soon).
- Current sync drops Airtable contacts with no `Sch` school link: 12 contacts missing in dev, 14 in local (audit 260703). They have Supabase auth users + `user_roles` but no `contacts` row.
- Related findings: `contacts.school_id` NOT NULL blocks unlinked contacts; no `contacts.user_id` link to auth; two sync paths independently read Airtable and drift.
- Decision: NO contact_schools junction. A contact belongs to at most ONE school — first entry of Airtable `Sch` wins.

## Target Architecture

```
Airtable ──(schools sync)──> schools
Airtable ──(contacts sync)──> contacts            ← source of truth, EVERY contact
contacts ──(provisioning)──> auth.users + user_roles (+ write back contacts.user_id)
```

One Airtable reader per entity; auth provisioning derives from DB, never from Airtable directly.

## Phases

### Phase 1 — Schema migration (0076)
File: `alembic/versions/0076_contacts_nullable_school_add_user_id.py`
- `contacts.school_id` → nullable; FK `ondelete` CASCADE → SET NULL (deleting school must not delete people).
- Add `contacts.user_id UUID NULL`, unique index. References `auth.users(id)` logically — no cross-schema FK (same pattern as `user_roles.user_id`, src/auth/models.py:20).
- Update `Contact` model (src/schools/models.py:87): `school_id` Optional, add `user_id`.
- Backfill in migration: `UPDATE contacts c SET user_id = u.id FROM auth.users u WHERE lower(c.email)=lower(u.email) AND c.user_id IS NULL` (guard duplicates: only when email maps to exactly one contact).

### Phase 2 — Contact-centric sync (src/schools/sync.py)
- New `sync_contacts_from_airtable(db) -> dict`: single pass over ALL Airtable contacts.
  - Upsert key: `airtable_id`; fallback `(school_id, lower(email))` then backfill airtable_id.
  - `school_id` = first `Sch` entry resolved via `school_by_airtable_id`, else NULL. First school wins — ignore extra links.
  - Update mutable fields on existing rows: first/last name, email, role, receive_comms, auto_emails, softr_access, school_id.
  - Counters: `contacts_created`, `contacts_updated`, `contacts_unlinked` (school NULL), `skipped`.
- `sync_schools_contacts_from_airtable`: strip contact + Supabase provisioning from school loop (sync.py:184-309). Keep schools upsert only. Endpoint `/api/v1/schools/sync-airtable` chains: schools sync → contacts sync; response keeps `contacts_created` etc. so frontend `SchoolSyncResult` stays compatible.
- Module >200 LOC → split: `src/schools/sync_schools.py`, `src/schools/sync_contacts.py`, `src/schools/sync_provisioning.py`; keep `sync.py` as thin re-export to avoid import churn.

### Phase 3 — Provisioning from contacts (`/api/v1/counselors/sync-airtable`)
- `sync_counselors_from_airtable(db, supabase)`: replace `get_contacts_records()` with `SELECT contacts WHERE email IS NOT NULL`.
- For contacts with `user_id NULL`: look up Supabase user by email (batch list_users, existing logic) else `create_user`; write `contacts.user_id`.
- UserRole upsert unchanged (role from `contact.role`: Director→hub_admin else hub_user; preserve super_admin; sync school_role). `school_id` from contact.
- Run contacts sync first inside the endpoint (or require callers run schools sync first — pick: endpoint chains contacts sync itself so it stays one-click).

### Phase 4 — Verify
- Local: run `/api/v1/schools/sync-airtable` then `/api/v1/counselors/sync-airtable`; assert contacts count == Airtable contact count (463 at audit time), incl. the 14 previously missing; assert the 12 unlinked have rows with `school_id NULL` and `user_id` set.
- Dev: same; re-diff against Emails_List (audit expects: file's 460 all present; dev keeps its 2 legit extras).
- pytest suite; update tests touching sync.

## Data Cleanups (Airtable-side, independent)
- Unlinked contacts are VALID data per admin: counselors not yet in talk with schools. `school_id NULL` is expected, do not "fix".
- Fix "Oliver Schools" → "Oliver Scholars"; delete duplicate "Trevor School" (re-point aexline contact + Trevor Day School); fix `clatempa@mfriends.or` typo; dedupe Jameel Freeman (no-email ×2, Nightingale-Bamford).

## Risks
- Backfill email collision: 2 dup-email pairs in dev (aexline, cmonroy) — backfill maps user to ONE contact only when unambiguous; ambiguous left NULL for provisioning pass to resolve after Trevor dedup.
- `user_roles.user_id` is UNIQUE → a user has one role/school; contact model now agrees (single school). No change needed.
- Frontend admin "Sync Airtable" buttons: response shapes preserved; verify `SchoolSyncResult`/`CounselorSyncResult` fields still match (cmm-frontend app/lib/api/schools.ts:82, counselors API).

## Status
- [ ] Phase 1 migration + model
- [ ] Phase 2 contacts sync + schools sync strip + module split
- [ ] Phase 3 provisioning from contacts
- [ ] Phase 4 verify local + dev
