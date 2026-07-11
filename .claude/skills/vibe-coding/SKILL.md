---
name: vibe-coding
description: Guardrails for sessions with non-technical users making small changes to cmm-backend. Activate when the user mentions vibe coding, identifies as non-technical, asks for a simple API/copy tweak in plain language, or invokes the disaster protocol ("everything is broken", "revert", "undo everything", "go back to when it worked"). Enforces branch safety, checkpoint commits, verification, migration protection, and safe rollback.
---

# Vibe Coding Guardrails — cmm-backend

The user is likely **non-technical**. Make their change safely, explain in plain English, and keep every step reversible. Human-facing companion doc: `docs/vibe-coding-guide.md`. Coding conventions: follow `LLM_GUIDELINES.md` (imports, router/schema patterns, error handling) — do not improvise style.

**Before your first edit, read `docs/architecture-and-conventions.md`** — it maps the router → auth dep → session → schema chain, the two DB paths (Supabase = auth only), background-work/threading rules, query optimization (`selectinload`, indexes), migration gotchas, and the before-you-code checklist.

## Communication rules

- Plain English, no jargon. Say "save point" not "SHA"; "checks" not "pytest".
- Before running anything with side effects, say what it does in one sentence.
- After each change, summarize *what changed and how to see it* (e.g. "the workshop list now includes `school_name` — here's a sample response").
- Never dump raw tracebacks on the user — interpret them.

## Session preflight (before the first edit)

1. `git status` — if there are uncommitted changes the user didn't make, STOP and explain; offer to stash.
2. Confirm current branch. **Never make edits on `main` or `production`** — pushing `main` auto-deploys to the dev environment **and runs Alembic migrations** (`.github/workflows/deploy.yml`). Create a branch: `feature/<short-slug>` or `fix/<short-slug>`.
3. Offer to start the dev server: `make dev` → http://localhost:8001 (health check: `GET /health`).

## Scope guard

- One small change per session. If the request expands, checkpoint the current change first.
- If the request touches a **danger zone** (below), stop and tell the user this needs a developer. Only proceed on explicit confirmation.

### Danger zones — warn before touching
- **Database migrations: `alembic/`** — the highest-risk area. Never create or edit a migration in a vibe session without explicit developer sign-off; merged migrations run automatically against the dev database. Never hand-edit an already-applied migration.
- Model changes (`src/<domain>/models.py`) that alter columns/tables — these *require* migrations, so same rule applies.
- Auth: `src/auth/` (`deps.py` role gates, `hub_password.py`)
- Webhooks: `src/zoom/webhook_router.py` (threaded, secret-verified)
- Integrations: `src/integrations/` (Zoom, Airtable), `src/workshops/sync_*.py`
- Background/AI tasks: `src/content/ai_review_task.py`
- Config & deploy: `src/config.py`, `src/main.py`, `Dockerfile`, `docker-compose.yml`, `manifest.yml`, `.github/**`
- Secrets: any `.env*` file — never read, edit, or commit
- One-off scripts in `scripts/` — never run against non-local data

### Safe zones — proceed freely
- Response fields / request validation: `src/<domain>/schemas.py`
- Small endpoint tweaks within one router: `src/<domain>/router.py`
- Email/communication copy: `src/communications/`
- Read-only analytics endpoints: `src/analytics/`

## Verify every change

There is **no lint/typecheck tooling wired up**. Verification is:

1. App imports and boots: dev server reloads without errors (or `uv run python -c "from src.main import app"`).
2. `uv run pytest` — must pass (coverage is thin; passing tests are necessary, not sufficient).
3. Exercise the changed endpoint with a real request (curl or FastAPI docs at `/docs`) and show the user the response.

## Checkpoint commits

After each verified working state, commit: `feature: <plain description>` or `fix: <...>` (no `chore`/`docs` for `.claude/` files, no AI references). Tell the user: "Save point created — we can always come back here."

## Shipping

When the user is happy: run pytest → push the branch → open a PR with `gh pr create` targeting `main` → give the user the PR link and remind them **a developer must review and merge; never merge for them.** If the PR contains a migration or model change, flag it prominently in the PR description.

## 🚨 Disaster protocol

Trigger phrases: "disaster protocol", "everything is broken", "revert everything", "go back to when it worked". When triggered:

1. **Stop fixing forward.** No further edits.
2. Assess: `git status` + `git log --oneline -10`. Identify the last good save point (last checkpoint commit, else the branch point from `main`).
3. **Check for applied migrations first:** if `alembic upgrade` was run this session, reverting code is not enough — note the revision, run `uv run alembic downgrade -1` only against the LOCAL database, and escalate to a developer if any shared database was touched.
4. Recover, least destructive option that fully restores a working state:
   - Uncommitted mess → `git restore .` (+ `git clean -fd` **only after listing what it deletes and getting a yes**).
   - Bad commits on the vibe branch, not pushed → `git reset --hard <last-good>`.
   - Pushed to the vibe branch → `git revert` the bad commits, or reset + `git push --force-with-lease` **on the vibe branch only** — never force-push `main`/`production`.
   - Total loss → confirm, then delete the branch and check out clean `main`.
5. Verify recovery: app boots, `uv run pytest` passes.
6. Explain in one paragraph what was rolled back and what (if anything) was lost.
7. Escalate to a developer (don't attempt yourself) if: anything reached `main`/dev environment, a migration ran against a shared database, secrets or deploy files were touched, or recovery fails once.
