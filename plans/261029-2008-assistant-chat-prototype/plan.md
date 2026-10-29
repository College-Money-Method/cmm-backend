# Assistant chat prototype (local first)

Status: in progress · Owner: Vu · Created 2026-10-29
Context: `cmm-frontend/plans/reports/notion-261029-1821-ai-assistant-personalized-plans.md` (search design, model + cost)

## Outcome
Standalone, password-gated chat site (`/assistant`, separate from SRC) that answers family/counselor questions from CMM content via hybrid RAG, cites sources, and identifies a device by an anonymous local user id (no name/email).

## Constraints / decisions
- Password gate mirrors College Aid Explorer: scrypt hash in `app_config.assistant_password_hash`, set in admin Global Settings, JWT `aud="assistant"` bound to the password version. Password never in code/env/repo.
- Local user id: random UUID in an httpOnly cookie (1y) created by the frontend loader, sent as `X-Assistant-User-Id`. Conversations stored server-side keyed by it. Profile (role, grade, state) lives in browser localStorage, sent per question.
- Content: published topics, assets (published, `source='cmm'` or `review_status='approved'`, **no** school/state/cohort restriction rows), workshops, FAQs. Transcripts + webinar Q&A = next phase.
- Search: `assistant_chunks` (pgvector 1024 + generated tsvector), hybrid via RRF, ≤2 chunks/source, top 6.
- Models (settings, env-overridable): answer + enrich = Haiku 4.5 (5.5 not enabled on the AWS account yet → switch via `ASSISTANT_ANSWER_MODEL_ID`), embeddings = `cohere.embed-v4:0`.
- Sync: `python -m src.assistant.sync` + optional scheduler job (`ASSISTANT_SYNC_ENABLED`, default off), hash-based so only changed sources hit Bedrock.

## Non-goals
SRC integration, school-scoped content, transcripts/Q&A ingest, gap alerts, rate limiting, PostHog.

## API contract (`/api/v1/assistant`)
| Method | Path | Notes |
|---|---|---|
| POST | `/access` `{password}` | → `{token, expires_at}`; 403 `assistant_disabled`, 401 `assistant_bad_password` |
| POST | `/chat` | headers `X-Assistant-Token`, `X-Assistant-User-Id`; body `{conversation_id?, message (1–2000), profile:{role:"family"\|"counselor", grade?:9–12, state?:"CA"}}` → SSE: `meta {conversation_id, sources:[{n,title,url,source_type,heading}]}`, `delta {text}`, `done {message_id}`, `error {code}` |
| GET | `/conversations` | → `{items:[{id,title,updated_at}]}` (this user id only) |
| GET | `/conversations/{id}` | → `{id,title,messages:[{id,role,content,sources,created_at}]}`; 404 if other user |
| DELETE | `/conversations/{id}` | 204 |
| PUT/DELETE | `/api/v1/app-config/assistant-password` | admin, mirrors college-aid |

## Phases
1. Backend: migration 0143 (vector ext, chunks, conversations, messages, app_config cols), models, access token, admin password endpoints — Vu/Claude
2. Backend: ingest (sources → chunker → enrich → embed → reconcile) + CLI + scheduler job
3. Backend: hybrid search + answer streaming + router + tests
4. Frontend: `/assistant` route, password form, chat UI, SSE proxy resource route, local uid cookie, profile, admin settings card + tests
5. Local E2E: run sync on local DB, chat via debug Chrome :8222; code review

## Acceptance
- Wrong/absent password → form; right password → chat; rotating password voids sessions.
- "My parents are divorced, what do I report?" retrieves the separated-parents content and cites it.
- Another browser (new uid) cannot read the first browser's conversations.
- Backend + frontend tests, typecheck, lint pass.
