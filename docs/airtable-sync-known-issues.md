# Airtable Sync — Known Issues (for AI agent to fix)

**Status:** current-state report. Documents behavior found in the Airtable→Supabase sync as of 2026-07-14. Each issue is written to be actionable by an AI agent: location, current behavior, impact, expected behavior, suggested direction, acceptance criteria.

**Direction of sync:** Airtable → Supabase, one-way. Mode today is *"add new + overwrite matches"* — NOT a full mirror. Verify each claim at the cited line before fixing; line numbers may drift.

---

## Resolution status (2026-07-14, branch `feat/airtable-sync-offboarding`)

**Access rule (now enforced):** a contact has counselor-hub access ⟺ `email IS NOT NULL AND school_id IS NOT NULL AND deleted_at IS NULL`. Airtable stays source-of-truth. Revocation is *soft* — drops `UserRole` + `profiles`, keeps the Supabase auth user (reversible). `super_admin` never touched.

| Issue | Status | Notes |
|-------|--------|-------|
| ISSUE-1 offboarding revoke | ✅ Resolved | Reconcile pass in `sync_provisioning._reconcile_revocations`. Gated by `SYNC_ENABLE_REVOKE` (default **log-only** for first deploy). |
| ISSUE-2 empty email | ✅ Resolved | Emptying a set email is now a no-op + warning; access is tied to `school_id`, not email. |
| ISSUE-3 contacts deletion | ✅ Resolved | New `contacts.deleted_at` (migration 0080). Contacts absent from the pull (matched by `airtable_id` only) are soft-deactivated; guarded by `SYNC_DEACTIVATION_MAX_MISSING_FRACTION` (default 0.2). Reappearing `airtable_id` reactivates. |
| ISSUE-3 schools/cohorts/workshops/webinars | ⏭️ Deferred | Intentionally out of scope this pass. |
| ISSUE-4 cohort chaining | ✅ Resolved | `sync_schools_contacts_from_airtable` runs cohort sync first; unresolved school→cohort links counted (`cohorts_unresolved`). |
| ISSUE-6 cohort upsert | ✅ Resolved | `hide_unavailability_calendar` upserted on match; `name` stays create-only. |
| ISSUE-7 duplicate email | ✅ Resolved | First-occurrence-only, collisions logged with record IDs + `email_collisions` count. |
| ISSUE-5, ISSUE-8 | ⏭️ Deferred | Out of scope this pass. |

**Access signal note:** `softr_access` is the *legacy* Softr hub flag and is NOT the new-hub access signal — do not use it for offboarding.

---

**Key files**
- `src/schools/sync_contacts.py` — contact upsert
- `src/schools/sync_provisioning.py` — auth user + UserRole + profile provisioning
- `src/schools/sync_schools.py` — school upsert
- `src/schools/sync.py` — schools/contacts/provisioning orchestrator
- `src/cycles/sync.py` — cohort sync (create-only)
- `src/workshops/sync_workshops.py`, `src/workshops/sync_webinars.py` — workshop/webinar sync
- `src/auth/profile_sync.py` — `upsert_profile` / `delete_profile` for the `profiles` mirror table
- `src/auth/router.py` — admin counselor CRUD, incl. `DELETE /api/v1/counselors/{user_id}` (the only revoke path)

**Recent change (migration 0079, 2026-07-14):** a local `profiles` table now mirrors Supabase `auth.users` (email + name) for fast counselor search. Provisioning keeps it in sync (`sync_provisioning.py:122-124`); the admin delete endpoint cleans it up. This adds a third place email is stored (contacts, profiles, auth.users) — relevant to ISSUE-2.

---

## ISSUE-1 — Airtable-driven offboarding does not revoke access [HIGH]

> **Updated 2026-07-14 (profiles-table work, migration 0079):** a *manual* deprovisioning path now exists — `DELETE /api/v1/counselors/{user_id}` in `src/auth/router.py:414` deletes the Supabase auth user + `UserRole` + `profiles` row. So the earlier "no revoke anywhere" framing is no longer true. The remaining gap is that **the Airtable sync is not wired to it**.

**Location:** `src/schools/sync_provisioning.py` (whole module); `src/schools/sync_contacts.py`; admin delete lives in `src/auth/router.py:414-434`

**Current behavior:**
- The sync path only ever *creates/reads* Supabase auth users and `UserRole` rows — it never revokes. Revocation exists only as a manual admin API call.
- Removing a contact from Airtable does nothing (see ISSUE-3). Clearing a contact's email (ISSUE-2) leaves the auth user + `UserRole` + `profiles` row fully intact.
- `provision_counselors_from_contacts` iterates only `Contact.email IS NOT NULL` (`sync_provisioning.py:46`), so any contact that loses access-relevant state is simply skipped, never revoked.

**Impact:** No way to offboard a counselor *via Airtable*. Former staff retain working hub access until an admin manually calls the delete endpoint. Airtable is not a complete access-management source.

**Expected behavior:** A defined, auditable Airtable-driven deprovisioning signal — e.g. a "Softr Access"/active flag or record removal — that revokes the `UserRole` (and optionally disables the auth user), reusing the existing delete/`delete_profile` plumbing. Must be explicit and logged; must never touch `super_admin`.

**Suggested direction:** Add a reconciliation pass to provisioning: for each existing counselor `UserRole`, if the driving contact is gone/inactive, revoke via the same helpers `delete_counselor` uses. Decide product rule for the auth user (disable vs. keep). Gate behind an explicit flag to avoid mass-revoke accidents (see partial-fetch safety).

**Acceptance criteria:**
- [ ] A documented Airtable signal (flag or deletion) results in `UserRole` revocation + `profiles` cleanup.
- [ ] `super_admin` never affected.
- [ ] Every revoke is logged with contact identity + reason.
- [ ] Idempotent and safe to re-run.

---

## ISSUE-2 — Clearing email in Airtable orphans the contact but keeps access [HIGH]

**Location:** `src/schools/sync_contacts.py:58,78,92-95`

**Current behavior:** When an Airtable contact's `Email` is emptied, the contact is matched by `airtable_id`, `new_values["email"] = None`, and the diff loop sets `contacts.email = None`. The `user_id` link is NOT in `new_values`, so it persists. Auth user + `UserRole` are untouched (ISSUE-1). **Now also:** provisioning skips email-less contacts (`sync_provisioning.py:46`), so `upsert_profile` is never called for them — the `profiles` row keeps the OLD email. Result: `contacts.email` is NULL while `profiles.email` and `auth.users.email` still hold the stale address. The mirror silently drifts.

**Impact:** Worst of both worlds — the contact loses the email that identifies it, but the person keeps hub access, and now the `profiles` mirror is out of sync with `contacts`. Admins may (wrongly) use "remove email" as a deactivation gesture.

**Expected behavior:** Decide the intended semantics of an emptied email. Either (a) treat it as a no-op / warning (don't blank a previously-set email), or (b) treat it as deactivation and trigger ISSUE-1's revoke path. Do not silently orphan.

**Acceptance criteria:**
- [ ] Emptying an email no longer leaves a stranded login+role with no path to detect it.
- [ ] Behavior is logged and documented.

---

## ISSUE-3 — Nothing is ever deleted or deactivated [HIGH]

**Location:** all sync modules (`sync_schools.py`, `sync_contacts.py`, `cycles/sync.py`, `sync_workshops.py`, `sync_webinars.py`)

**Current behavior:** No sync removes or soft-deletes records absent from Airtable. Every sync only iterates Airtable records and upserts. Deleting a school / cohort / contact / workshop / webinar in Airtable leaves the Supabase copy live forever.

**Impact:** Stale data accumulates. Deleted schools/workshops still appear in the app. No cleanup path.

**Expected behavior:** A deliberate policy per entity — likely soft-delete/deactivate (not hard delete) for records that disappear from Airtable, or an explicit "active" flag synced from Airtable. Must be conservative (never mass-delete on a partial/failed Airtable fetch).

**Acceptance criteria:**
- [ ] Records removed from Airtable are flagged inactive (or documented as intentionally retained).
- [ ] Guard against wiping everything when Airtable returns empty/partial due to API error.
- [ ] Per-entity policy documented.

---

## ISSUE-4 — Cohort sync not chained before school sync [MEDIUM]

**Location:** `src/schools/sync.py` (`sync_schools_contacts_from_airtable`); cohort sync in `src/cycles/sync.py`

**Current behavior:** The schools pipeline does NOT run cohort sync first. Schools link to cohorts by cohort `airtable_id`/name; if cohorts are stale in Supabase, the school→cohort link silently resolves to NULL.

**Impact:** Schools silently end up with no cohort after a sync, with no error surfaced. Requires an admin to remember to run "Sync Cohorts" first, manually.

**Expected behavior:** Either chain cohort sync into the school pipeline, or surface a clear warning/count when a school's cohort link can't be resolved.

**Acceptance criteria:**
- [ ] School cohort links resolve reliably without manual ordering, OR unresolved links are counted + logged.

---

## ISSUE-5 — School descriptive fields are write-once (Airtable edits ignored) [MEDIUM]

**Location:** `src/schools/sync_schools.py` (update branch)

**Current behavior:** On an existing school, only `airtable_id`, `is_current_customer`, `cohort_id`, `airtable_slug` are refreshed. `name`, address fields, and URL fields are set on create and never updated. Editing them in Airtable afterward has no effect.

**Impact:** Contradicts the "Airtable is source of truth" assumption. Address/URL corrections in Airtable silently don't propagate.

**Expected behavior:** Decide which school fields Airtable owns and update them consistently. Document intentional exceptions (e.g. `slug` deliberately app-owned for URL stability).

**Acceptance criteria:**
- [ ] Field ownership documented per column.
- [ ] Airtable-owned fields update on existing schools.

---

## ISSUE-6 — Cohorts are create-only (existing never updated) [LOW]

**Location:** `src/cycles/sync.py`

**Current behavior:** If a cohort matches by `airtable_id` or `name`, it's skipped entirely (only backfills a missing `airtable_id`). Field changes in Airtable (e.g. `Hide Unavailability Calendar`, renamed `Name`) never reach Supabase.

**Impact:** Cohort edits in Airtable are silently dropped.

**Expected behavior:** Upsert cohort fields on match (at least `hide_unavailability_calendar`; decide on `name`).

**Acceptance criteria:**
- [ ] Existing cohorts update editable fields from Airtable.

---

## ISSUE-7 — Duplicate email in Airtable → order-dependent "last wins" [MEDIUM]

**Location:** `src/schools/sync_contacts.py:70-72,86-98`

**Current behavior:** Two Airtable contacts with the same email map to the same Supabase row (global email dedup). The first to run claims the `airtable_id` slot; each later one still matches by email and overwrites all fields. Net result depends on Airtable iteration order — effectively "last processed wins" on visible values.

**Impact:** Nondeterministic-feeling outcome; a shared/typo'd email silently blends two people or moves someone to the wrong school.

**Expected behavior:** Detect duplicate emails within an Airtable pull and handle deterministically — skip + warn, or pick a documented winner. Surface a count of collisions.

**Acceptance criteria:**
- [ ] Duplicate emails detected and logged with both record IDs.
- [ ] Outcome deterministic and documented.

---

## ISSUE-8 — Contact reassignment to an un-synced school silently unlinks [LOW]

**Location:** `src/schools/sync_contacts.py:60-68`

**Current behavior:** When a contact's `Sch` link points to a school not yet in Supabase, `school_id` resolves to NULL and the contact becomes "unlinked" — rather than retaining its previous school. Counted in `contacts_unlinked` but not surfaced as an error.

**Impact:** A reassignment (e.g. Annie Wright → Hampton) done before the target school is synced drops the contact to no school. Mostly mitigated by school-before-contact ordering, but silent when it happens.

**Expected behavior:** Either retain prior `school_id` when the new target can't be resolved, or surface unresolved links prominently. Document the chosen rule.

**Acceptance criteria:**
- [ ] Unresolved `Sch` links are logged per-contact (not just counted).
- [ ] Chosen retain-vs-unlink policy documented.

---

## Cross-cutting notes

- **Verify before fixing:** confirm each behavior at the cited location; line numbers may have shifted.
- **Idempotency:** all fixes must keep syncs safe to re-run.
- **Partial-fetch safety:** any deletion/deactivation logic (ISSUE-1, ISSUE-3) MUST guard against Airtable returning empty/partial data on API failure.
- **`super_admin`:** must never be revoked or altered by any sync path.

## Unresolved questions

1. Is "never delete" intentional? (affects ISSUE-1, ISSUE-3)
2. What is the intended offboarding signal in Airtable — a flag, or record deletion? (ISSUE-1)
3. Which school fields should Airtable own vs. app own? (ISSUE-5)
4. `airtable_asset_base_id` is configured but unused by any sync — legacy or planned feature?
