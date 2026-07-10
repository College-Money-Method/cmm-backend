# cmm-backend — Architecture & Conventions

> Medium-depth map of how this repo is structured, how files interact, and what to think about before changing things. Written for AI agents and developers.
> **Code-level patterns (imports, router/schema/model syntax, CRUD recipes) live in `LLM_GUIDELINES.md` — follow that for style; this doc covers architecture and engineering judgment.** Companion: `docs/vibe-coding-guide.md` (workflow/safety).

## Stack

FastAPI (uvicorn) · Python 3.12 · **uv** (deps + runner) · SQLAlchemy 2.0 ORM + Alembic (migrations) · Supabase = Postgres hosting + **Auth only** (see §3) · AWS S3 (boto3) · Airtable (pyairtable) · Zoom API · OpenAI/Anthropic (AI review). Run: `make dev` → **port 8001** (the local-dev convention), interactive docs at `/docs`, health at `/health`. The Docker container (`Dockerfile`, `docker-compose.yml`, ECS) runs on **port 8000** — don't "fix" either to match the other.

## Directory map

```
src/
├── main.py                # app entry: model imports → routers → CORS → lifespan
├── config.py              # pydantic-settings singleton `settings` (env-file based)
├── db/
│   ├── base.py            # DeclarativeBase, engine (pool_pre_ping), session factory
│   ├── deps.py            # get_db() per-request session → DbDep
│   ├── client.py          # Supabase client (lru_cached, service-role) — AUTH ONLY
│   ├── models.py          # barrel: re-exports every model (Alembic reads this)
│   └── enums.py           # shared enums (AppRole, RegistrationStatus, ...)
├── auth/                  # deps.py (role gates), router.py (user admin), hub_password.py
├── <domain>/              # one folder per feature: workshops/ schools/ communications/
│   ├── router.py          #   endpoints + business logic (no separate service layer)
│   ├── models.py          #   SQLAlchemy models
│   ├── schemas.py         #   Pydantic Create/Update/Out
│   └── *_service.py       #   only for cross-cutting work (syncs, AI tasks)
├── integrations/          # zoom.py, airtable.py — external API clients
alembic/versions/          # numbered migrations 0001_… (applied automatically on deploy!)
scripts/                   # one-off backfills/imports — never run against non-local data
tests/                     # pytest (thin coverage, analytics only)
```

## How a request works (the core interaction chain)

```
src/main.py                 include_router(workshops_router) — 17 routers registered explicitly
        │
        ▼
src/<domain>/router.py      @router.get("/webinars/{id}")
        │                   def get_webinar(webinar_id: uuid.UUID, _admin: AdminDep, db: DbDep)
        │                        │              │                       │
        │                        │   src/auth/deps.py: bearer token     src/db/deps.py:
        │                        │   → supabase.auth.get_user()         per-request Session
        │                        │   → UserRole row → CurrentUser       (router commits itself)
        ▼
SQLAlchemy query            select(Webinar).options(selectinload(...)) → scalar_one_or_none()
        │
        ▼
src/<domain>/schemas.py     Pydantic Out schema via model_validate() or a _to_out() helper
```

Key structural facts:

- **`src/main.py` import order is load-bearing:** it imports *every* `*.models` module first so SQLAlchemy can resolve cross-module relationships, then imports routers. A new model must be imported in **both** `src/main.py` and the `src/db/models.py` barrel (the latter is what Alembic autogenerate sees — miss it and your table is silently omitted from migrations).
- **There is no service layer for most features.** Business logic lives in router functions, with `_to_out()`-style helpers for response shaping. Dedicated service modules exist only for cross-cutting work (`workshops/attendance_sync_service.py`, `workshops/sync*.py`, `content/ai_review_task.py`). Match this: don't invent a services/ layer for a simple endpoint, and don't stuff a multi-step sync into a router function.
- **No custom middleware or exception handlers** — just CORS (`allow_origin_regex` for `*.collegemoneymethod.com` + localhost:5173) and inline `HTTPException`s.

## The two database paths (don't mix them up)

| Path | Used for | Files |
|---|---|---|
| **SQLAlchemy ORM** | ALL application data | `src/db/base.py`, `deps.py`, every `models.py` |
| **Supabase client** | Auth ONLY — `auth.get_user(token)`, `auth.admin.*` user management | `src/db/client.py`, `src/auth/deps.py`, `src/auth/router.py`, `schools/sync_provisioning.py` |

There are **zero** `supabase.table(...)` data calls and essentially no raw SQL in app code (the ORM expression API is used for complex queries, e.g. correlated `scalar_subquery()` counts in `workshops/router.py`). Note: `LLM_GUIDELINES.md` §5 shows Supabase table-query patterns — that section is **stale**; use SQLAlchemy for data.

**Session handling:** `DbDep` yields a per-request `Session` and only closes it — **routers call `db.commit()` / `db.rollback()` explicitly**. Pattern for unique violations: catch `IntegrityError` → `db.rollback()` → raise `HTTPException(409)` (see `workshops/router.py` webinar create). Sessions use `expire_on_commit=False`, so objects stay readable after commit.

## Auth

`src/auth/deps.py` → `get_current_user`: bearer token → `supabase.auth.get_user(token)` → local `UserRole` lookup → `CurrentUser(user_id, role, school_id, school_role)`. Missing token = 401; authenticated but no role row = 403.

Role gates as `Annotated` deps — inject one per endpoint:

| Dep | Allows |
|---|---|
| `AdminDep` | `super_admin` |
| `AdminOrViewerDep` | + `viewer` |
| `HubAdminDep` | `super_admin`, `hub_admin` |
| `CounselorDep` | `super_admin`, `hub_admin`, `hub_user`, `viewer` |

Conventions: name the param `_admin: AdminDep` when identity is unused; `user: CounselorDep` when the handler scopes by `user.school_id` (always scope hub/counselor queries by school — see `list_my_registrations`). Roles live in `AppRole` (`src/db/enums.py`).

## Background work & threading — think before you async

This repo has **no task queue, no scheduler, no persistence for pending jobs**. Everything runs in-process. What exists:

1. **Zoom webhook** (`src/zoom/webhook_router.py`): verifies HMAC signature, answers Zoom's `endpoint.url_validation` challenge, always returns 200 (so Zoom won't retry). On `webinar.ended` it schedules `_sync_with_retry` via FastAPI `BackgroundTasks`: retry delays `[0, 900, 1800]`s, a **fresh DB session per attempt**, and the blocking sync wrapped in `asyncio.to_thread(...)` so it doesn't stall the event loop.
2. **AI review** (`src/content/ai_review_task.py`, triggered from `submissions_router.py`): endpoint sets `review_status="ai_reviewing"`, commits, then `background_tasks.add_task(_run_ai_review)`. The task opens **its own session** (the request session is closed by then), degrades gracefully when the API key/package is missing, and swallows all exceptions.

**Known risks an agent must respect (and not silently worsen):**
- A **deploy/restart kills in-flight background tasks** — a Zoom sync can be lost mid-retry (up to 45 min window); an AI review can leave a submission stuck in `ai_reviewing`. Manual fallbacks exist (`POST /webinars/{id}/sync-attendance`).
- There are **no timeouts, no idempotency keys, no external retries**. If you add background work, follow the existing pattern: own session, log-and-swallow (never crash the worker), status field on the row so a human can see/retrigger, and design the task to be safely re-runnable.
- **Never do blocking I/O directly in an `async def` handler** — use `asyncio.to_thread` like the webhook does, or make the handler sync (`def`) so FastAPI runs it in the threadpool.
- If a job genuinely needs durability (must survive restarts), that's a real queue/scheduler discussion — flag it to a developer instead of layering more `BackgroundTasks`.

**Long syncs** (`workshops/sync.py` Airtable import, `attendance_sync_service.py`): triggered **synchronously from admin endpoints**, bounded to one base/one webinar, batch by building in-memory lookup dicts (`by_registrant_id`, `by_email`) then matching in one pass, and **commit once at the end** with a sync-log row. Keep that shape: single commit, log row, bounded scope.

## Query patterns & optimization — before you write a query

1. **Eager-load every relationship the response touches** with `selectinload(...)` (nested chains are fine: `webinar → workshop → content_assets → asset_type`). This is the repo's N+1 defense; response helpers freely read relationship attributes, so a missing `selectinload` = one query per row.
2. **Batch by IDs** with `.in_(ids)` — and remember SQL `IN` doesn't preserve order; reorder in Python if order matters (see `update_workshop_objectives`).
3. **Counts via correlated `scalar_subquery()`**, not Python loops over rows.
4. **Indexes:** filter/sort/FK columns get `index=True` in the model — adding one means a migration.
5. **Pagination:** there is currently **no offset/limit convention** — list endpoints return all rows. Fine at current scale, but for anything user-generated or unbounded, add `limit/offset` params rather than following the existing pattern blindly.
6. Commit explicitly; one commit per logical operation.

## Migrations (highest-risk area)

**Preferred workflow: hand-write the migration file** (proven accurate in practice; `make revision` autogenerate is a fallback/cross-check — it misses renames and anything not imported into `src/db/models.py`). Conventions for a hand-written migration:

- File: `alembic/versions/NNNN_short_snake_slug.py` where `NNNN` = next sequential number (check with `ls alembic/versions | sort | tail` or `make history`).
- Header: `revision = "NNNN"`, `down_revision = "<current head>"` — chain off the actual head, and always write a real `downgrade()` that reverses `upgrade()`.
- Match the model exactly (types, nullability, `server_default`, indexes) — the migration and `models.py` change ship in the same commit.
- Apply locally with `make upgrade` (runs `alembic upgrade heads`). The chain is strictly linear and sequentially numbered (`0001`…) — keep it that way: one head, no hash-id filenames. Test the downgrade too when feasible: `make downgrade`.
- **Merged PRs run migrations automatically against the dev database** (CI step in `.github/workflows/deploy.yml`). Never edit an already-applied migration — write a new one.
- New model? Still import it in `src/db/models.py` (keeps autogenerate/metadata honest) and `src/main.py` (relationship resolution). `alembic/env.py` pins `search_path` for Supabase's pooler — don't touch.

## Models & schemas conventions

- `Base` is a bare `DeclarativeBase` — **no shared mixin**. Each model repeats `id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)` and `created_at` with `server_default=func.now()` (not all tables have `updated_at`). Use SQLAlchemy 2.0 `Mapped[...]`/`mapped_column` style; JSON columns are `JSONB` with `server_default="[]"`.
- Shared enums in `src/db/enums.py` as `(str, enum.Enum)`.
- Pydantic per feature: **Create / Update / Out** (plus Summary/ListItem where needed). Out schemas: `model_config = ConfigDict(from_attributes=True)` + `model_validate(obj)`. Update schemas: all-optional fields + `model_dump(exclude_unset=True)` for PATCH semantics.

## Errors, logging, config

- Errors: inline `fastapi.HTTPException` (404/403/409/422/503); no custom exception classes or handlers. Background tasks log-and-swallow.
- Logging: stdlib, `logging.basicConfig(level=INFO)` in `main.py`; per-module `logger = logging.getLogger(__name__)`. Log entry/exit and counts for syncs.
- Config: `src/config.py` pydantic-settings singleton `settings`; every field has a safe local default. **Env switching is file-based** — the Makefile runs `uv run --env-file .env.$(ENV)` (`local` default), the app itself doesn't branch on ENV.

## Before-you-code checklist (agent self-review)

1. New endpoint? → right `src/<domain>/router.py`, correct role gate dep, Out schema in `schemas.py`, register nothing (router already included) unless it's a new domain → then `src/main.py`.
2. New model? → import it in **both** `src/db/models.py` and `src/main.py`; hand-write the migration per the Migrations section (sequential `NNNN` id, chained `down_revision`, real `downgrade()`); flag it prominently in the PR.
3. Query touches relationships? → `selectinload` them. Filter column? → check it's indexed.
4. Endpoint could run >2–3s? → consider `BackgroundTasks` with the own-session + status-field pattern; if it must survive restarts, escalate.
5. Blocking I/O (requests, boto3, Zoom) in async code? → `asyncio.to_thread` or sync handler.
6. Committing? → explicit `db.commit()`, `IntegrityError → rollback → 409`.
7. Verify: app boots (`make dev` reloads clean), `uv run pytest` passes, exercise the endpoint via `/docs` or curl. There's no lint/typecheck wired up — read your own diff carefully.

## Unresolved questions

- `LLM_GUIDELINES.md` §5 (Supabase table queries) contradicts actual practice (SQLAlchemy-only data access) — update or remove that section?
- No pagination on list endpoints — intentional at current scale?
- Background-work durability (lost Zoom syncs / stuck `ai_reviewing` on deploy) — accepted risk or worth a persistent queue?
