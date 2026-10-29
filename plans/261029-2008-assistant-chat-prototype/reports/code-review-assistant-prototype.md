# Code review — assistant chat prototype (2026-10-29)

Scope: backend `src/assistant/*`, migration 0143, app_config password, `tests/assistant/*`; frontend `/assistant` route, chat proxy, hook, components, settings card.
Verdict: no critical issues, no auth bypass, markdown renderer safe apart from one href bypass (fixed).

## Fixed
| # | Sev | Finding | Fix |
|---|-----|---------|-----|
| 1 | High | `/access` `time.sleep(1)` on a sync endpoint pinned threadpool workers; ~40 bad guesses stall all sync endpoints | async endpoint, DB+scrypt via `run_in_threadpool`, `asyncio.sleep` (`router.py`) |
| 2 | High | `safeHref` let `/\evil.com` (and `/\t/…`) through → off-site link | reject backslash/whitespace/control chars (`render-assistant-markdown.ts`), test added |
| 4 | Med | boto embed client had 60s default read timeout while chat holds a DB connection | `Config(connect_timeout=5, read_timeout=30, max_attempts=2)` (`embeddings.py`) |
| 7 | Med | sync hash ignored enrich flag / chunk sizes / embed model; `--no-enrich` rows never upgraded | `index_hash(doc, enrich)` in `sync.py`, test added. Next sync re-indexes all sources once (~$0.40 enrichment) |
| 9 | Low | comment claimed strict alternation; API merges consecutive same-role turns | comment corrected |
| 11 | Low | stream aborted by `load()` cleared `streaming` of the newer stream | only the current controller clears the flag (`use-assistant-chat.ts`) |
| 13 | Low | chat proxy read whole body before size check | check `Content-Length` first (`assistant-chat.ts`) |

## Open — before production
- **3 Med** No rate limit / cost cap on `/chat`; device id is client-chosen, so per-device limits are bypassable. Need per-IP (forwarded) or per-token quota + conversations-per-device cap.
- **1b** Same `time.sleep` pattern in `src/colleges/public_router.py:56` (College Aid gate). Not changed.
- **5 Med** `role` self-declared → any password holder can read `for_counselor`-only chunks. OK only if that content isn't sensitive.
- **6 Med** No unique `(source_type, source_id, chunk_index)`; overlapping syncs (cron on >1 ECS task) can duplicate chunks. Add constraint or advisory lock.
- **8 Med** Migration: pgvector on Supabase usually in `extensions` schema; verify `search_path` for migration role + pooler, and `CREATE EXTENSION` rights. Dry-run on dev. `server_default` on for_family/for_counselor missing in model (autogenerate drift).
- **10 Low** Client disconnect mid-stream: no save, no usage record, orphan user turn. Save partial / record usage in `finally`.
- **12 Low** SSE parser split-CRLF across chunks (backend emits `\n` only; matters only behind rewriting proxy).
- **14 Low** Public app-config exposes `assistant_password_set` + `updated_at` (same as College Aid).
- **15 Low** `answer.py` logs `str(exc)` from Anthropic client — confirm no prompt echo.
- **16 Low** Keyword regex ASCII-only; accented words skipped on keyword side.

## Verified OK
JWT aud/exp/sub/pwv (constant-time), fail-closed secret outside local/test; HttpOnly+SameSite=Lax(+Secure prod) cookies scoped `/assistant`; ownership 404 on all conversation routes; tsquery terms `[A-Za-z0-9]` only; profile fields validated; raw HTML/images escaped, `javascript:` rejected, citations bounded to sources; no SSRF; no-store responses; password never logged; streaming save uses own session; sync keeps old chunks on failure, removes stale sources.

Verification after fixes: backend `tests/assistant` 36 passed, ruff E9/F clean; frontend typecheck clean, assistant tests 13 passed.

## Unresolved questions
- Is counselor-only content sensitive enough that self-declared role matters?
- Will nightly sync run on more than one ECS task?
- Fix the College Aid `/access` sleep in the same pass?
