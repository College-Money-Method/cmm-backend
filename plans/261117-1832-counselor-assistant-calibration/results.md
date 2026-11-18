# Calibration results — 2026-11-17

School: Annie Wright Schools · answers Haiku 4.5 · judge Sonnet · 81 cases (25 themes).
Reports: `scripts/output/assistant_eval/counselor-<stamp>-<label>.md/.json` (gitignored).

## Runs

Judge and cases were tightened between runs (looser clarify wording, team guidance counts as a source,
over-specified cases fixed), so compare within a row group, not across.

| Run | Prompt | Pass | Avg |
|---|---|---|---|
| baseline | old prompt | 11/81 (13%) | 2.2 |
| rules | + clarify / hand-off / no-price rules | 16/81 (19%) | 2.5 |
| rules + playbooks | + 3 team-guidance playbooks (not seeded) | 43/81 (53%) | 3.4 |
| **final-rules** | rules only, final judge + cases | **18/81 (22%)** | 2.5 |
| **final-playbooks** | rules + playbooks, final judge + cases | **49/81 (60%)** | 3.4 |

The final pair is the like-for-like comparison: **playbooks add +31 cases**.
No invented URLs, prices or contacts in the final-playbooks run. Retrieval found the right source in 47/49 cases.

## final-playbooks by mode

| Mode | rules only | + playbooks |
|---|---|---|
| route | 0/14 | 13/14 |
| answer_route | 6/21 | 14/21 |
| answer | 12/29 | 15/29 |
| clarify | 0/17 | 7/17 |

At 100% with playbooks: themes 13, 14, 18, 19, 20 and R1, R3, R4, R5 (policy and routing).

## Remaining failures (32) by cause

| Cause | Themes / cases | Fix |
|---|---|---|
| **Content missing** | 2 (0/3), 9 (0/3), 10 (0/3), 8 (1/3), parts of 5, 6 | Write the aid topics in [content-to-index.md](content-to-index.md) §2 |
| **Policy not written** | 15 (1/3), 17-c, 16-c | Counselor program guide §1 (Paul) |
| **Clarify behavior** | 1, 3, 4, 7-b, 12 (7/17 pass) | Model still often answers fully and skips the closing questions, or asks the wrong detail (theme 4: income instead of which colleges) |
| **Routing slip** | 11-b/c/d (1/4), r2-a | Skips the 1:1 suggestion on appeals; once claimed it "can send the request to Paul" |

Content gaps drive most of the remaining answer/answer_route failures. More prompt tuning there
would teach the model to guess, which the rules forbid.

## Next tuning ideas (not done, to avoid over-fitting on 81 cases)
- Clarify: a few short examples in the playbook (one line each: question → closing questions).
- Appeals: move the "suggest a 1:1" line into the base rules if Paul confirms it applies to every appeal.
- Re-check after content lands; the judge flagged some clarify cases where the detail was arguably given.

## Voice and style (2026-11-18)

Paul's style guide turned into two team-guidance sections in `counselor_playbooks.json`:
"Voice and style" (both assistants: no em/en dashes, sparing bold, answer first, no hype words,
merit vs need offices, how to write family emails/texts) and "Talking with families" (family chat only:
partnership "we", "your student", spell out acronyms, short paragraphs, warm close).
Eval now reports house style (dashes, hype phrases) and a judge voice score 1-5; style never fails a case.

| Run | Pass | Style clean | Voice |
|---|---|---|---|
| voice-baseline (DB playbooks, no voice) | 52/81 | 29/81 | 4.0 |
| voice v1 (one long section, both) | 43/81 | – | 3.9 |
| voice v3 (lean both + family-only) | 50/81 | 75/81 | 4.0 |

The long single section cost about 8 passes (shorter answers, skipped closing questions); keeping the
family warmth out of the counselor prompt fixed that. 50 vs 52 is within run-to-run noise (49 to 52).
Family chat spot check (5 questions): 0/5 style clean without the sections, 5/5 with them.
Bold is allowed for scan terms: source samples are plain text extracted from email, so they say nothing about bold.

## Unresolved questions
- Is ≥ 80% the right bar, or should route/policy themes be 100% and aid themes judged by hand?
- Should clarify always end with questions even when a common-case answer covers most families?
