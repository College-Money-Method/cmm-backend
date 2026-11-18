# Code review: counselor assistant calibration eval

Scope: counselor_context.py, counselor_router.py, prompt_sections.py, embeddings.py, bedrock_usage.py, counselor_prompt.py, scripts/assistant_eval/*, seed script, tests.
Verification: `pytest tests/assistant` = 197 passed, 5 skipped. `ruff` NOT installed in .venv (`uv run ruff` fails to spawn, `python -m ruff` missing) so lint was not run; uvx attempt blocked by hook.

## Live chat path: no regression found
- counselor_prompt_builder order and values match the old inline code (workshops, program, guidance, own_name, template values, asker_name). `own_name(...) if user_id else None` is equivalent for the router (user_id always set).
- Only difference: `email_origin()` is now read once at builder time instead of lazily inside the lambda. Same within one request; the router test monkeypatch was updated to match.
- guidance_parts/format_guidance: same query, same filtering of empty bodies, same header/format. Output unchanged.
- embed_query default keeps ASSISTANT_QUESTION_EMBED. Prompt rule edits are additive (rule 25 gains "and you have what you need").

## Findings (ranked)

### High
1. scripts/assistant_eval/run_counselor_eval.py:101-103 (`pool.map` + `run_case`) and :87: `judge()` is outside the try/except. Any Bedrock throttle/timeout or a judge call error in a worker re-raises from `list(pool.map(...))`, kills the run, and discards every completed (already paid) result; nothing is written until the end. With 4 workers x 2 calls, throttling is likely at 81 cases. Fix: wrap judge in try/except returning `{"error": ...}` (verdict_ok already fails on "error"), and ideally add retry/backoff plus incremental JSON writes.

### Medium
2. scripts/assistant_eval/eval_grading.py:20,45-46 (`_URL` / `_found`): false positives. Only `.,;:` are stripped, so `https://x/y**` (bold), `https://x/y!`, `https://x/y?` or a trailing-slash difference are reported as "invented" and fail the case. The `[^\s)\]>"']+` class includes `*`. Fix: also strip `*_!?` and a trailing `/`, or normalise via urlparse before comparing.
3. eval_grading.py:19-21 money/phone/email coverage gaps (false negatives): "500 dollars", "USD 500", "$1.5k"; `$5000` vs `$5,000` mismatches (false positive); phones like "(206) 555-0100" are not matched. Also the check compares against the whole prompt, so a CMM price is "grounded" if any unrelated source chunk contains that number. The new "never state CMM prices" rule is therefore only fully policed by the judge. Fine as a heuristic, but document it; normalise commas/spaces in money.
4. eval_grading.py:77-79 `verdict_ok`: if the judge omits or returns an empty `covered` list the case passes (`not covered`), and coverage only needs 50%. A malformed-but-parseable verdict can pass silently. Require `len(covered)==len(must_cover)` when must_cover is non-empty (likewise `asked` vs must_ask).
5. run_counselor_eval.py:117-120 `--guidance-file` appends to the DB guidance. After running the seed script with --apply, the same sections exist in the DB, so the eval prompt would contain them twice and diverge from live. Either dedupe by title or document "use one or the other".
6. seed_counselor_assistant_playbooks.py:48-58: no environment guard and `enabled=True`; `--apply` against the prod DB (whatever DATABASE_URL is) changes the live counselor prompt for all schools immediately. Dry-run default is good. Add a printed DB host, or a confirmation. Title match is exact `==` on the stripped file title, so an admin-edited title (trailing space, casing) creates a duplicate section instead of updating; re-run also overwrites admin edits to body (documented). Use `func.trim`/lower for match.

### Low
7. run_counselor_eval.py:62-66 `prepare` embeds sequentially (81 serial Cohere calls) and any embed error aborts before spend on answers; acceptable. Retrieval uses `case["message"]` alone, matching live only for a first turn (no history), which is the right baseline.
8. eval_grading.py:17 `_CITATION` `\[(\d+)\]` also matches bracketed years/numbers (e.g. "[2025]") -> spurious bad_citations. Cap at `\d{1,2}`.
9. No unit tests for eval_grading (regexes, parse_verdict, verdict_ok) or the seed script dry-run/apply; regex logic is where the bugs above sit. Cheap to add, no Bedrock needed.
10. File sizes: counselor_cases.json 1860 lines (data, fine); other files under ~150 lines.
11. scripts/assistant_eval/__pycache__ exists on disk; confirm it is gitignored before committing (check could not run, hook blocked).

## Specific questions
- Thread safety of run_counselor_eval: OK. DB session is closed before the pool starts; threads touch only `prepared` items (strings, dataclass chunks, dict cases; read-only) and `get_client()`, which is `lru_cache`d AnthropicBedrock (httpx client, thread-safe). One caveat: first call to `get_client()` could race in the cache, harmless (maybe two clients).
- bedrock_usage.record from threads: safe. It opens its own short-lived Session per call from the cached engine (pool 10+10, 4 workers fine), commits, swallows and logs errors. It does a DB write per Bedrock call (about 2 x 81 rows plus 81 embed rows into `bedrock_usage`) on whatever DB `DATABASE_URL` points to; intentional ledger behaviour, but note a prod-pointed `.env` will write there. Failures are silent (warning only), so spend may be under-recorded without any run-level signal.
- Unexpected DB writes: only bedrock_usage rows (eval) and assistant_prompt_sections (seed, `--apply` only). Eval does not create conversations/messages. Seed dry-run rolls back (note `db.add` is flushed by autoflush but rolled back; ok).
- Secrets/PII: grep of cases/playbooks/plan folder found no emails, phone numbers, keys or passwords. Playbooks name "Paul" (staff first name) and https://studentaid.gov only. Eval output goes to gitignored `scripts/output/` and stores answers and source titles, not the full prompt. Confirm counselor_cases.json messages contain no real family data (they appear synthetic).

## Positives
Single shared prompt builder (eval grades what counselors get), guidance override kept eval-only, dry-run-by-default seed, separate ASSISTANT_EVAL ledger type, retrieval/prompt prepared on one thread before fan-out.

## Unresolved questions
- Is ruff expected via a dev dependency group? Not in pyproject; lint unverified.
- Is DATABASE_URL for eval runs meant to be dev, not prod?

Status: DONE_WITH_CONCERNS
Findings: High 1 (run_counselor_eval.py:101 uncaught judge error aborts run); Medium 2-6 (eval_grading.py:20,45 URL false positives; :19-21 money/phone gaps; :77 lenient verdict_ok; run_counselor_eval.py:117 duplicate guidance; seed:48 prod write / exact-title match); Low 7-11.
