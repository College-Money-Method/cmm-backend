# Content the counselor assistant still needs

Two kinds of gap: **CMM policy facts** (only Paul/CMM can supply; the assistant must never guess) and
**aid topics** missing from the Resource Center. Both reach the assistant with no code change:

- Policy facts → a **counselor-only resource** (Admin → Content, `for_counselor` on, `for_family` off,
  published). The indexer picks it up on the next sync. Short routing rules can instead live in
  Admin → Assistant team guidance (draft in `scripts/assistant_eval/counselor_playbooks.json`).
- Aid topics → normal family resources or topic sections (family and counselor both see them).

Evidence: index survey 2026-11-17 (no chunk text for the topics below) and eval failures.

## 1. Counselor program guide (counselor-only resource) — every [TO CONFIRM] is Paul's call

Suggested headings (one H2 each, so each becomes its own chunk with a precise heading):

1. **1:1 advising** (themes 13, 14)
   - What a session covers: [TO CONFIRM]
   - Who runs sessions: [TO CONFIRM]
   - Cost; school-sponsored vs family-paid; how each is billed: [TO CONFIRM]
   - Booking link(s) — sponsored vs direct-pay: [TO CONFIRM]
   - Typical lead time; what to do before a deadline: [TO CONFIRM]
   - Whether the school can see usage (how many families booked): [TO CONFIRM]
2. **What the partnership includes** (theme 15)
   - Families: Resource Center + workshops; do families email CMM directly? [TO CONFIRM]
   - Counselors: may email Paul about a family's case? Turnaround? [TO CONFIRM]
   - What CMM does not do (individual financial-planning advice?): [TO CONFIRM]
3. **Workshops: recordings and slides** (theme 16)
   - Where recordings live and for how long: [TO CONFIRM]
   - May counselors share slides with families? Where to get them: [TO CONFIRM]
4. **Language access** (theme 17)
   - Spanish workshops / Spanish audio / captions on recordings: [TO CONFIRM]
   - Interpreters at live workshops: who arranges, who pays: [TO CONFIRM]
5. **Counselor Hub access** (theme 18)
   - Who adds a counselor (school Hub admin?), what to send if that fails, opt-in to emails: [TO CONFIRM]
6. **Resource Center problems** (theme 19)
   - Where to report a broken link / outdated year / missing topic: [TO CONFIRM]
7. **Contracts, invoices, dates** (theme 20)
   - Renewal timing, payment schedule, invoices, W-9, changing a workshop date — whom to contact: [TO CONFIRM]
8. **Partnership inquiries, speaking, hosting** (R1–R3)
   - Referring another school; restarting a conversation: [TO CONFIRM]
   - Speaking/campus visits: info to send (date, audience, location, budget) and to whom: [TO CONFIRM]
   - Hosting a webinar: host steps and where host links come from: [TO CONFIRM]
9. **Brand assets and usage data** (R4) — logo, overview slides, usage reports: [TO CONFIRM]
   (A draft counselor-only "College Money Method Logo" asset already exists — publish it.)
10. **Talking to Paul** (R5) — how to request a call: [TO CONFIRM]

Once written, also confirm each "Paul" route in `counselor_playbooks.json` → "Who handles what",
then seed it: `uv run python scripts/seed/seed_counselor_assistant_playbooks.py --apply`.

## 2. Aid topics with no indexed content (write as resources/topic sections)

| Theme | Missing content | Today |
|---|---|---|
| 1 | FAFSA identity / citizenship confirmation flags, "selected for verification": what each means, next steps, deadlines | Only the FSA ID guide; assistant can only route to studentaid.gov |
| 2 | FAFSA account errors and corrections: fixing a submitted FAFSA (household size etc.), login errors | Only FSA ID guide and studentaid.gov links |
| 4 | Noncustodial CSS Profile waivers: how to request, which colleges, what the custodial parent can see | Partial (CSS Profile rules and waiver process topic) |
| 5 | 529s by owner (parent / student / grandparent / custodial UTMA) on FAFSA vs CSS Profile | Partial (asset sections) |
| 6 | Rental property, LLCs, inherited/co-owned real estate valuation | Partial (assets topics) |
| 7 | Trusts and settlements (access restricted until an age) | Partial |
| 9 | CSS Profile when taxes are not filed yet (estimate, update later); optional questions — answer or leave blank | **None** |
| 10 | CSS Profile K-12 tuition section; siblings in college / private school | **None** |
| 12 | Self-employment, gig and retirement income on FAFSA and CSS Profile | Thin (2/4 retrievals hit) |

## 3. Not worth indexing (checked)

- Marketing pages (`who-i-work-with`, etc.): no rates or policy facts.
- `faqs` table: empty locally; fill it or skip.
- `high-school-curriculum` page: could help grade-fit questions (theme 16); workshop rows already give
  dates and grades, so low priority.
