# Zoom registration silent failures — production investigation

Date: 2026-09-15 · Scope: prod DB (`.env.prod`) + CloudWatch `/ecs/cmm-prod-backend` (us-east-1, profile `cmm`, 30d retention)
Trigger: parent feedback "I signed up for the workshop but didn't receive the link."

## Verdict

Registration **was** broken, independently of the "Previous Workshops" lockout.

**294 of 1,873 registrations (16%) on still-upcoming webinars never reached Zoom.** Those parents see a success screen, hold an `approved` row in `workshop_registrations`, and have `zoom_registrant_id IS NULL` — so Zoom never issued a join link and never emailed them.

CloudWatch over 30 days: **282 failed Zoom registration calls vs 1,655 successes — 14.6% failure rate.** Every failure swallowed: `register_webinar` logs a WARNING and returns `None`; the caller already committed the DB row, so nothing surfaces to the parent or to admins.

## The named webinar

`f37d2836-13ea-4c23-99d6-7c9df37818dd` — "Applying for Financial Aid and Scholarships in Senior Year", Zoom `88250053050`, start 2026-09-15 01:00 UTC.

- 109 registrations, **6 with no `zoom_registrant_id`**
- 0 registrations after start time (that is the lockout, fixed separately by `src/workshops/upcoming_window.py`)
- all 6 failures = Cause 2 below (free-text answer 142–399 chars)

DB counts match CloudWatch exactly for this webinar (103 created / 6 failed), confirming the log is a complete record.

## Cause 1 — stale required school dropdown (233 failures, 83%)

```
400 {"code":300,"message":"The parameter is required in custom_questions: Which school does your student attend?."}
```

Zoom custom question per webinar:

```json
{"title":"Which school does your student attend?","type":"single_dropdown","required":true,
 "answers":["St. Ignatius Preparatory School"]}
```

The `answers` list was never synced to the schools actually attached to the webinar's cohort. Chain:

1. registrant's school not in `answers`
2. `_match_answer` (`src/integrations/zoom.py:94`) returns `None`, logs `Zoom answer match failed`
3. payload builder (`src/integrations/zoom.py:183`) drops the question entirely
4. Zoom rejects — the question is `required`

Worst upcoming sessions (no-Zoom-link / total registrations):

| Session | Date | Zoom ID | Stranded |
|---|---|---|---|
| Applying for Financial Aid… | 2026-10-06 | 81276546458 | **106 / 108 (98%)** |
| Succeeding in the Fin. Aid Process (Intl) | 2026-09-23 | 86409527993 | 37 / 45 |
| Understanding How to Qualify… | 2027-02-03 | 81463721868 | 29 / 29 (100%) |
| Evaluating Schools for Award Opportunities | 2027-04-21 | 81213281108 | 18 / 18 (100%) |
| Comparing Awards… (Carondelet) | 2027-03-10 | 88100845134 | 16 / 17 |
| Comparing Awards… (Brentwood) | 2027-03-02 | 81690448418 | 12 / 12 (100%) |

Six upcoming webinars at ~100% failure — every parent registering gets nothing.

Webinars split cleanly by error type, matching their `required` flag: `required:true` → this error; `required:false` → Cause 2 only. (`86409527993` shows both — flag toggled mid-window.)

## Cause 2 — free-text answer over Zoom's 128-char limit (49 failures, 17%)

```
400 {"code":300,"message":"Invalid parameter: custom_questions."}
```

"Are there any questions you would like to submit on this workshop topic?" is `type: short`. `_match_answer` returns free-text as-is (empty `answers` list), so the parent's full text is POSTed unbounded. Zoom's limit is 128 chars. Boundary is exact across all prod rows with a free-text answer:

| Answer length | Succeeded | Failed |
|---|---|---|
| ≤ 128 chars | 3,065 | 25 |
| > 128 chars | 2 | **52** |

Max length among successes: 124. Min among failures: 142. The parents who wrote the most were the ones silently dropped.

## Recommended fixes (not implemented — investigation only)

1. **Truncate free-text to 128 chars** before POST — one-line, no external writes, kills 17% of failures.
2. **Sync the school dropdown** to the cohort's schools via `PATCH /webinars/{id}/registrants/questions`, or set `required:false`. Non-required demonstrably works (807 successes with the question omitted). Fixes the remaining 83%.
3. **Stop swallowing the failure** — flag the registration so admins can see who has no join link, rather than only a CloudWatch WARNING.
4. **Backfill the 294 stranded registrations** after 1–3 land, starting with the 106 on 2026-10-06. Note: this sends ~294 real Zoom confirmation emails.

## Method notes

- `_question_cache` (`src/integrations/zoom.py:59`) is per-process and cleared only on restart; after any Zoom question config change, prod tasks must be restarted or they keep matching against stale answer lists.
- Registrant emails never surfaced in raw form during this investigation; DB output aggregated or hashed.

## Unresolved

- Who owns the Zoom webinar question config — is it set by hand per webinar, or provisioned by code? Determines whether fix 2 belongs in webinar creation or is an ops task.
- Should a Zoom registration failure block the DB commit (parent sees an error and can retry) or stay non-fatal with admin follow-up? Current design is non-fatal, which is why this went unnoticed for 30+ days.
- 128 is empirically derived, not documented by Zoom. Confirm against Zoom API docs before hardcoding.
