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

## Unresolved questions
- Is ≥ 80% the right bar, or should route/policy themes be 100% and aid themes judged by hand?
- Should clarify always end with questions even when a common-case answer covers most families?
