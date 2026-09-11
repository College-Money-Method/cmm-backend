# Video pipeline config: what is hardcoded, what is a secret, what an admin edits

Four settings had no home. `VIMEO_UPLOAD_USER_URI` and `VIMEO_AUDIT_FOLDER_URI`
default to `""` and both break an upload when unset; `VIDEO_PIPELINE_ALERT_EMAIL`
unset makes the pipeline silent; `VIMEO_EMBED_DOMAINS` unset makes every replay
unplayable. None of them reached a deployed container. They are now sorted into
the three places config actually lives here, and the one an operator has a
reason to change is editable without a deploy.

## The split

**Hardcoded in `src/config.py`** — `vimeo_embed_domains`
(`collegemoneymethod.com`), `video_caption_locales` (`es,zh,zh-Hant`),
`workshop_display_timezone` (`America/New_York`). All three already carried
exactly these defaults, so "hardcode it" meant *stop plumbing them* rather than
change a value: the terraform variables and the `VIMEO_EMBED_DOMAINS` entries in
both env lists are gone. One entry replaces the three prod listed and the
four dev did, on your reading that the apex covers `dev.next` and `next` — I
have not confirmed against Vimeo that its whitelist matches subdomains, and if
it does not, a dev upload will refuse to play on `dev.collegemoneymethod.com`.
Cheap to find out: the first audit run's player either loads there or does not.

**SSM secrets** — `VIMEO_UPLOAD_USER_URI`, `VIMEO_AUDIT_FOLDER_URI`,
`VIDEO_PIPELINE_ALERT_EMAIL`, added to `.env.dev`, `.env.prod`, `manifest.yml`,
and (the two Vimeo URIs only) `module "video_task"`'s `secret_names` in both
terraform environments. Not credentials, but they travel with the token they are
meaningless without, and `manifest.yml` is the only injection path the backend
has. The one-shot task has no deploy workflow, hence the terraform list; it does
not send email, so the alert address is not on it.

Dev carries the same values as prod deliberately. Dev receives no
`recording.completed` webhook, so nothing there uploads on its own — a dev
upload only happens when someone starts an audit run by hand, which is when
having the folder configured is the point.

**Editable by an admin** — the audit folder. See below.

## The audit folder is now a Global Setting

Which folder this month's audits land in is a reviewer's call, and it was an
env var, so changing it meant a deploy.

`app_config.vimeo_audit_folder_uri` (migration `0123`) is nullable, meaning
"fall back to the env seed" — the same contract `workshop_display_timezone`
already has. `audit_folder_uri()` in `src/integrations/vimeo_upload.py` keeps its
signature and both call sites; it now consults the override first. A blank
override is a *cleared* override, not a configured empty folder, so clearing the
field cannot disable the guard that keeps audits out of the main library.

The reader lives in `src/app_config/operator_settings.py` rather than beside its
consumer, which is where the timezone precedent puts it. That is deliberate:
`src/integrations` talks to Vimeo, not to Postgres, and the audit folder is read
inside an ECS task where a five-minute in-process cache and a never-raise
contract matter. The router clears the cache on PATCH, next to the timezone
reset, so an admin sees the change on the next run instead of in five minutes.

`AppConfigUpdate` accepts what an admin will actually paste. Picking a folder
means looking at its Vimeo page, so `https://vimeo.com/user/151255816/folder/30467578`
is converted to `/users/151255816/projects/30467578` rather than rejected — the
two forms share their ids and nothing else, and a rejection here would otherwise
be discovered by an audit run that has already downloaded, trimmed and sampled
the recording. Free text that names no folder is refused at the form.

Both refusal messages (`manual_run.py`, `process_recording.py`) now say Global
Settings first and name the env var second, because Global Settings is where the
person reading the message can act.

## Files

Backend: `src/app_config/models.py`, `schemas.py`, `router.py`,
`operator_settings.py` (new), `src/integrations/vimeo_upload.py`,
`src/video_pipeline/manual_run.py`, `process_recording.py`,
`alembic/versions/0123_app_config_vimeo_audit_folder_uri.py` (new),
`tests/conftest.py`, `tests/video_pipeline/test_audit_folder_setting.py` (new),
`manifest.yml`, `.env.example`, `.env.dev`, `.env.prod`.

Frontend: `app/types/app-config.ts`, `app/routes/admin/settings.tsx` (a "Video
Pipeline" card above Topic Overview Video).

Infra: `environments/dev/main.tf`, `environments/prod/main.tf`.

## Verification

- `tests/video_pipeline/test_audit_folder_setting.py` — 15 passed. Covers which
  value wins, the two blank cases, trailing-slash trimming, every paste form, a
  database read through the real reader, an unreadable row reporting unset
  rather than raising, and an edit being invisible until the cache is dropped.
- `tests/video_pipeline/ tests/video_cc/ tests/emails/` — 841 passed.
- Per directory: `auth` 58 passed, `calculators` 19 passed, `emails` 313 passed.
- No single whole-suite invocation completes on this machine, and that is not
  this change's doing. Two pre-existing tests hang on network calls they never
  mocked, and the run sits on them until the harness kills the task:
  `tests/analytics/test_analytics_endpoints.py::test_date_params_forwarded_to_posthog`
  patches `get_batched_breakdowns` but leaves `get_hogql_query` unpatched, so the
  endpoint dials PostHog for real; and
  `tests/schools/test_school_custom_slug.py::test_update_sets_custom_slug` hangs
  90 s+ on its own.
- Two pre-existing failures, both in analytics, both `KeyError: 'other_videos'`:
  `test_content_endpoint_shape` and `test_content_video_views_scoped_to_topic`.
  `grep -rn other_videos src/` returns nothing — the key exists only in that test
  file — and `git status` shows the file and all of `src/analytics` unmodified
  here. Same area as the known `tests/analytics/test_resource_breakdown_queries.py`
  collection error.
- The schools hang was checked against a pristine `git worktree` at HEAD, which
  has none of this work: the test passes there in 1.85 s, and still passes in
  1.04 s after copying this change's `tests/conftest.py` and
  `operator_settings.py` in. So the new autouse fixture is not the cause. What
  the worktree lacks is a real `.env`, which is the remaining difference — the
  test builds the whole `src.main` app, so with live credentials present
  something on that path reaches the network. Not narrowed further; it is
  pre-existing either way.
- `terraform fmt -check` clean on both environments. `terraform validate` still
  reports `Module not installed` for `module "video_task"`, which is
  pre-existing: no `terraform init` has been run since that module was added,
  and none of this is applied anywhere.
- `tsc --noEmit` and `eslint` clean on the two frontend files.
- SSM: 30 parameters written to `/copilot/cmm-backend/dev/secrets/`, 29 to
  `prod`. The six new ones resolve; the whole `manifest.yml` list resolves in
  both environments, which is the check `build-secrets.sh` runs at deploy time.

A suite-wide fixture in `tests/conftest.py` stubs the override reader out.
Without it every audit-folder lookup in the suite would open a session against
whatever `.env` points at and cache the answer across unrelated tests. The two
tests that want the real reader put it back and point `get_session_factory` at
their own database.

## Concerns

- **Migrations `0121`, `0122` and `0123` have never been run.** `GET
  /api/v1/app-config` will 500 on the missing column until `alembic upgrade
  head`, which takes the admin Settings page and every school home page with it.
  Say the word and I will run it against local; dev and prod need it before the
  next deploy.
- **A redeploy is required for the SSM values to reach a container.** ECS
  resolves both `environment` and `secrets` at task launch, so writing the
  parameters changed nothing that is currently running.
- `AWS_ACCESS_KEY_ID` is in `manifest.yml` and in dev's `.env.dev`, but not in
  `.env.prod`; prod's SSM parameter survives from an earlier sync and so the
  deploy preflight passes. Pre-existing, and worth deciding on separately —
  the backend service has a task role.
- `scratchpad/e2e.log` and `e2e2.log` still hold raw Vimeo tus upload tokens in
  logged PATCH URLs. They should be deleted or moved somewhere private.

## Unresolved

- Run the three migrations now, or wait?
- `recording.transcript_completed` is still not wired into
  `src/zoom/webhook_router.py`, so the pipeline can still start before the VTT
  exists and chapter from frames alone.

## Status

**Status:** DONE_WITH_CONCERNS

**Summary:** All four parts landed — the three missing vars added to `.env.dev`
and `.env.prod` and synced to SSM (30 dev / 29 prod parameters resolving);
`vimeo_embed_domains`, `video_caption_locales` and `workshop_display_timezone`
hardcoded in `src/config.py` with their terraform plumbing removed; and
`VIMEO_AUDIT_FOLDER_URI` turned into an admin-editable Global Setting, seeded
from the env var, cached 300 s, with a frontend field that accepts the pasted
browser folder URL. 15 new tests, all passing.

**Concerns/Blockers:** Nothing is applied or committed. Migrations `0121`–`0123`
have never been run, so `GET /api/v1/app-config` will 500 on the missing column
until `alembic upgrade head` — that takes the admin Settings page and every
school home page with it. Say the word and I will run it against local; dev and
prod need it before the next deploy. A redeploy is also required before any SSM
value reaches a container.
