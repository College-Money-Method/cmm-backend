# Vibe Coding Guide for Non-Technical Teammates

> **Who this is for:** Anyone at CMM who wants to make small changes to our apps using Claude Code, without being a developer.
> **What it covers:** Both repos — `cmm-frontend` (the website/app) and `cmm-backend` (the API/server).
> This is a synced copy. The canonical version lives in `cmm-frontend/docs/vibe-coding-guide.md`.

---

## The 5 Golden Rules

1. **Never work directly on `main`.** Pushing to `main` **automatically deploys to the dev environment** in both repos. Always ask Claude to "create a new branch first" — it will do it for you.
2. **Never touch the `production` branch.** Ever. That deploys to real users.
3. **One small change per session.** "Change the button text on the contact page" — great. "Redesign the dashboard and add three features" — not vibe coding, that's a project. Ask a developer.
4. **Verify before you share.** Look at the change in your browser, then ask Claude to run the checks (it knows which ones). If checks fail, don't push.
5. **Finish with a Pull Request (PR), reviewed by a developer.** You never merge your own changes. Claude will open the PR for you.

---

## The Two Repos at a Glance

| | `cmm-frontend` | `cmm-backend` |
|---|---|---|
| What it is | The website & app people see | The server/API behind it |
| Location | `~/WebstormProjects/cmm-frontend` | `~/PycharmProjects/cmm-backend` |
| Run locally | `pnpm dev` → http://localhost:5173 | `make dev` → http://localhost:8001 |
| Check your work | `pnpm typecheck` then `pnpm build` | `uv run pytest` |
| Good for vibe coding | Text, colors, layout, small UI tweaks | Adding a field to a response, email copy |

You don't need to memorize the commands — Claude knows them (they're in each repo's `vibe-coding` skill). Just know they exist so you can say *"run the checks"*.

---

## The Workflow (every session, same loop)

### 1. Start clean
Open the repo in Claude Code and say:

> "I want to vibe code a small change. Start me on a fresh branch and make sure nothing is half-finished from before."

Claude will check the state of the repo and create a branch like `feature/contact-page-copy`.

### 2. Describe the change — be specific
Good prompts:
- *"On the contact page, change the heading 'Get in touch' to 'Talk to us' "*
- *"The primary buttons look too dark — make them slightly lighter, show me before/after"*
- *"Add a `school_name` field to the workshop list API response"*

Weak prompts (Claude will have to guess):
- *"Make the site better"*
- *"Fix the thing on the page"*

Tips:
- **Paste screenshots.** Drag an image into the chat and circle what you mean.
- **One change at a time.** When it works, commit, then do the next one.
- **Ask Claude to explain** anything in plain English: *"Explain what you changed like I'm not a developer."*

### 3. Look at it
- Frontend: keep `pnpm dev` running and refresh http://localhost:5173.
- Backend: hit the endpoint or ask Claude to demonstrate the change with a test request.

### 4. Checkpoint when it works
Say: *"That looks right — commit this as a checkpoint."*
Commits are save points. If the next prompt breaks things, you can always come back here.

### 5. Ship it for review
Say: *"Run the checks, then push the branch and open a PR for review."*
Then message a developer to review it. **Done — do not merge it yourself.**

---

## What's Safe vs. What's Off-Limits

### 🟢 Safe zones — go ahead

**Frontend (`cmm-frontend`):**
- Page text and content: files under `app/routes/` and `app/components/`
- Colors and theme: `app/app.css`, `app/styles/theme.css`
- Buttons, cards, spacing, small layout tweaks: `app/components/`
- Images and static files: `public/`

**Backend (`cmm-backend`):**
- Adding/renaming a field in an API response: `src/<domain>/schemas.py`
- Small tweaks to a single endpoint: `src/<domain>/router.py`
- Email/communication copy: `src/communications/`

### 🔴 Danger zones — stop and ask a developer

If Claude says your request touches one of these, **stop and ask a developer** instead:

- **Login/auth** — anything with "auth", "login", "password", "supabase" in the name
- **Payments/billing** — none exist today; if that changes, it's automatically off-limits
- **Database migrations** — backend `alembic/` folder (these permanently change the database)
- **Webhooks & integrations** — Zoom, Airtable (`src/zoom/`, `src/integrations/`)
- **Deployment files** — `Dockerfile`, `manifest.yml`, anything in `.github/`
- **Secret files** — anything named `.env*` (never open, never share, never commit)
- **Core plumbing** — frontend `app/lib/api-base.ts`, `app/routes.ts`, `app/root.tsx`; backend `src/config.py`, `src/auth/`, `src/main.py`

The `vibe-coding` skill in each repo tells Claude to warn you before touching any of these.

---

## 🚨 Disaster Protocol — "Everything Is Broken"

It happens to everyone. A session goes sideways, the page is blank, errors everywhere. **Do not keep prompting "fix it" — that often digs deeper.** Instead:

### Step 1: Stop and say the magic phrase

> **"Disaster protocol: stop what you're doing and revert everything to the last working state."**

Claude will figure out the last good save point (your last checkpoint commit, or the branch start) and restore it. This is exactly why we commit checkpoints in step 4.

### Step 2: If Claude itself is confused
Start a **new** Claude Code session (old context can be part of the problem) and say:

> "The previous session broke this repo. Show me `git status` and `git log` in plain English, then revert to the last commit where things worked. Don't try to fix forward — revert."

### Step 3: Nuclear option (throws away ALL your session's work)
If nothing was worth keeping:

> "Discard all my uncommitted changes and delete this branch. Take me back to a clean `main`."

Since you never worked on `main` directly (Rule 1), `main` is always safe to return to.

### Step 4: When to call a developer
- The **dev or production site** is broken (not just your local machine)
- Claude mentions it ran a **database migration** or changed **deployment files**
- You reverted and it's *still* broken
- You're not sure whether anything was pushed

Tell them: what you asked for, roughly what Claude did, and whether anything was pushed. No shame — this is a normal part of the workflow.

### What can and can't be undone

| Situation | Undoable? |
|---|---|
| Local changes, not committed | ✅ Instantly |
| Committed on your branch, not pushed | ✅ Easily |
| Pushed to your branch, PR not merged | ✅ Easily (nothing deployed) |
| Merged to `main` | ⚠️ Deployed to dev — developer reverts it |
| Database migration ran | ⚠️ Developer needed — sometimes hard |
| Deleted `.env` files or secrets | ❌ Ask a developer immediately |

---

## Glossary

- **Repo** — the project folder, tracked by git
- **Branch** — your private copy of the code where changes are safe to make
- **Commit** — a save point you can return to
- **Push** — uploading your commits to GitHub
- **PR (Pull Request)** — "please review my change" — how changes get into `main`
- **`main`** — the shared branch; merging here deploys to the **dev** environment
- **`production`** — the branch behind the live site; hands off
- **Dev server** — the app running on your own machine (localhost); breaking it affects nobody
- **Migration** — a permanent change to the database structure; developer territory

---

## For Developers Maintaining This Guide

- Agent-facing guardrails live in `.claude/skills/vibe-coding/SKILL.md` in **each** repo — update those when commands or danger zones change.
- Technical deep-dive (structure, file interactions, conventions, engineering pitfalls): `docs/architecture-and-conventions.md` in **each** repo.
- Backend coding conventions for agents: `cmm-backend/LLM_GUIDELINES.md` (already comprehensive; the skill links to it).
- Keep the copy in `cmm-backend/docs/vibe-coding-guide.md` in sync with this one.
