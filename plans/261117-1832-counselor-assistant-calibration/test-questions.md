# Counselor assistant — Phase 1 test questions

Source: `AI_Companion_Phase1_Top20_Questions.pdf`. Generated from `scripts/assistant_eval/counselor_cases.json`.
Paste each question into the counselor assistant (Counselor Hub) and check the answer against the expectations.

**Note:** the eval scores assume the three team-guidance playbooks are loaded. Until `scripts/seed/seed_counselor_assistant_playbooks.py --apply` runs, live chat behaves like the rules-only run (routing questions mostly fail).

Every answer must also: cite sources as [n]; never invent a price, fee, date, link, phone or email; never claim it sent, logged or scheduled anything.

## Top 20 questions

### 1. FAFSA identity or citizenship flag

> A senior finished the FAFSA but it says the student's identity needs to be confirmed. What does that mean and what should we do next?

- **Expected:** Give the common-case answer, then ask for the missing detail
- **Should ask:** which message or flag the student sees (identity confirmation, citizenship match, selected for verification); whether the student or parent already completed the identity or mobile step
- **Should cover:** the next step for the flag, from CMM content; that federal guidance on this is still changing or to watch studentaid.gov
- **Eval (rules + playbooks):** 1/4 phrasings pass

<details><summary>Other phrasings</summary>

- `01-b`: A student's FAFSA came back with a citizenship flag. Is this time-sensitive?
- `01-c`: One of my students got a message on the FAFSA that's holding up processing. What should the family do?
- `01-d` (detail given → should answer directly): A student's FAFSA says 'selected for verification'. She hasn't done anything else yet. What happens next and what should she gather?

</details>

### 2. FAFSA account errors and corrections

> A parent keeps getting an 'unknown error' trying to create their FAFSA account. Is this a known issue?

- **Expected:** Answer what sources cover, then say who handles the rest
- **Should cover:** the options: retry, make a correction on the submitted FAFSA, or contact Federal Student Aid support
- **Routes to:** Federal Student Aid support, and Paul at College Money Method if the error persists or is unfamiliar
- **Eval (rules + playbooks):** 0/3 phrasings pass

<details><summary>Other phrasings</summary>

- `02-b`: A family submitted the FAFSA and then realized they entered the wrong household size. How do they fix it?
- `02-c`: Parent can't log in to studentaid.gov to finish the FAFSA, it keeps erroring out. Options?

</details>

### 3. Separated or divorced parents

> A student's parents are separated. Which parent fills out the FAFSA?

- **Expected:** Give the common-case answer, then ask for the missing detail
- **Should ask:** whether the parents are legally separated or divorced, or still married and filing jointly; which parent the student lives with or who provides more financial support
- **Should cover:** which parent completes the FAFSA (the one who provides more support); that the CSS Profile may also ask for the noncustodial parent; how to report one parent's share of a joint return
- **Eval (rules + playbooks):** 2/4 phrasings pass

<details><summary>Other phrasings</summary>

- `03-b`: Parents are separated but still file taxes jointly. How should they report income on the FAFSA?
- `03-c`: Do parents need to be legally separated for only one of them to do the FAFSA?
- `03-d` (detail given → should answer directly): Divorced parents, student lives with mom most of the year, dad pays more of her expenses. Who does the FAFSA and who does the CSS Profile?

</details>

### 4. Noncustodial parent on the CSS Profile

> Mom finished her part of the CSS Profile. Can she see whether dad finished his?

- **Expected:** Give the common-case answer, then ask for the missing detail
- **Should ask:** which colleges the student is applying to, since each sets its own noncustodial-parent rule
- **Should cover:** the first parent cannot see whether the other parent finished their part; waivers are requested from and decided by each college's aid office
- **Routes to:** each college's financial aid office (for waivers)
- **Eval (rules + playbooks):** 1/3 phrasings pass

<details><summary>Other phrasings</summary>

- `04-b`: The student's father won't complete the noncustodial CSS Profile. Can the family get a waiver?
- `04-c`: The noncustodial parent hasn't filed taxes. What should he put on his CSS Profile?

</details>

### 5. 529 plans

> A grandparent has a 529 for the student. Does it count on the FAFSA?

- **Expected:** Answer from sources, with citations
- **Should cover:** reporting depends on who owns the account; a grandparent-owned 529 is not reported on the FAFSA but some CSS Profile colleges ask about it
- **Eval (rules + playbooks):** 2/4 phrasings pass

<details><summary>Other phrasings</summary>

- `05-b`: How is a 529 reported on the FAFSA and CSS Profile?
- `05-c` (detail given → should answer directly): A family has a custodial UTMA account and a 529 owned by an aunt. Do either of those hurt aid?
- `05-d` (detail given → should answer directly): The 529 is owned by the parents and the student is the beneficiary. Where does it go on the FAFSA?

</details>

### 6. Rental property, LLCs, inherited real estate

> A family owns a rental property. Do they report it on the FAFSA?

- **Expected:** Give the common-case answer, then ask for the missing detail
- **Should ask:** whether the property is the family's primary residence; whose names are on the deed or entity; for a business, how many employees it has
- **Should cover:** FAFSA excludes the primary home but counts other real estate; CSS Profile counts home equity; value is current market value minus debt owed on it
- **Eval (rules + playbooks):** 3/4 phrasings pass

<details><summary>Other phrasings</summary>

- `06-b`: Parents inherited a house with an uncle, both names on the deed. How do they value it for the CSS Profile?
- `06-c`: Dad owns an LLC. Does it count as an asset?
- `06-d` (detail given → should answer directly): A family owns a second home in their own names, it's not where they live. How is that treated on the FAFSA versus the CSS Profile?

</details>

### 7. Trusts, settlements, student-held assets

> A student is the beneficiary of a trust. Does it count as their asset?

- **Expected:** Give the common-case answer, then ask for the missing detail
- **Should ask:** what kind of trust or settlement it is and who controls it or can access it
- **Should cover:** student assets are assessed more heavily than parent assets; money the student cannot access yet can still count
- **Eval (rules + playbooks):** 2/3 phrasings pass

<details><summary>Other phrasings</summary>

- `07-b`: A student has a settlement she can't access until she's 21. Does it count for financial aid?
- `07-c` (detail given → should answer directly): The student has a savings account in her own name. How much does that hurt aid compared to the parents' savings?

</details>

### 8. Merit aid and whether to file

> A family makes too much for need-based aid. Do they still need to file the FAFSA to get merit scholarships?

- **Expected:** Answer from sources, with citations
- **Should cover:** merit aid filing rules vary by college, so check each college's requirements; some colleges require the FAFSA or CSS Profile even for merit scholarships
- **Eval (rules + playbooks):** 1/3 phrasings pass

<details><summary>Other phrasings</summary>

- `08-b`: How should a student answer the Common App question about applying for financial aid if they only want merit aid?
- `08-c`: Is the CSS Profile required for merit scholarships?

</details>

### 9. CSS Profile optional fields and unfiled taxes

> A family hasn't filed their taxes yet. What should they put on the CSS Profile?

- **Expected:** Answer from sources, with citations
- **Should cover:** estimate unfiled taxes (for example with a tax estimator) and update later; answer optional questions when the family has real data for them
- **Routes to:** the CSS Profile guide in the Resource Center, or Paul at College Money Method for the family's case
- **Waiting on CMM content:** should say it doesn't have the fact and route, never guess
- **Eval (rules + playbooks):** 0/3 phrasings pass

<details><summary>Other phrasings</summary>

- `09-b`: Should a parent leave the optional questions blank on the CSS Profile?
- `09-c`: Does leaving items blank on the CSS Profile hurt the student's aid?

</details>

### 10. CSS Profile K-12 tuition and siblings

> What goes in the CSS Profile's K-12 tuition section?

- **Expected:** Answer from sources, with citations
- **Should cover:** the K-12 section covers what parents paid and aid from the school itself, not outside help; point to the CSS Profile guide for sibling years
- **Routes to:** the CSS Profile guide in the Resource Center, or Paul at College Money Method for the family's case
- **Waiting on CMM content:** should say it doesn't have the fact and route, never guess
- **Eval (rules + playbooks):** 0/3 phrasings pass

<details><summary>Other phrasings</summary>

- `10-b`: Grandparents pay part of the younger sibling's private school tuition. Does that go on the CSS Profile?
- `10-c`: How does the CSS Profile handle a sibling who's also in college next year?

</details>

### 11. Special circumstances, income changes, appeals

> A parent was laid off this fall. Does that count as a special circumstance and what should the family do?

- **Expected:** Answer what sources cover, then say who handles the rest
- **Should cover:** what counts as a special circumstance and that it is documented with the college's aid office; families can appeal after receiving an offer
- **Routes to:** a 1:1 session with College Money Method (Paul) for the specific family's case
- **Eval (rules + playbooks):** 1/4 phrasings pass

<details><summary>Other phrasings</summary>

- `11-b`: A family wants to appeal their daughter's aid offer. Where do they start?
- `11-c`: A dad plans to retire next year. How should they handle that on the aid forms?
- `11-d`: A family has huge out-of-pocket medical costs this year. Can they get more aid?

</details>

### 12. Self-employment, gig and retirement income

> A parent is self-employed. How does that income go on the FAFSA?

- **Expected:** Give the common-case answer, then ask for the missing detail
- **Should ask:** whether the income is already earned or only expected
- **Should cover:** where each income type is reported; expected future gains are not reported
- **Eval (rules + playbooks):** 2/4 phrasings pass

<details><summary>Other phrasings</summary>

- `12-b`: Mom does gig work for a delivery app. Where is that reported?
- `12-c` (detail given → should answer directly): A retired parent is drawing from investments. How do they report it?
- `12-d` (detail given → should answer directly): A parent expects to sell stock next year at a big gain. Do they report that now?

</details>

### 13. 1:1 advising: what it is, cost, who

> What does College Money Method's 1:1 advising include?

- **Expected:** Answer from sources, with citations
- **Should cover:** what 1:1 advising is, only from its sources; if the sources lack the current offering or cost, says so and routes to Paul
- **Routes to:** Paul at College Money Method
- **Waiting on CMM content:** should say it doesn't have the fact and route, never guess
- **Eval (rules + playbooks):** 4/4 phrasings pass

<details><summary>Other phrasings</summary>

- `13-b`: I have a family whose situation is beyond what I can help with. Can I refer them for individual advising? How much is it?
- `13-c`: Can our school sponsor 1:1 sessions for some families?
- `13-d`: Who actually does the 1:1 advising sessions?

</details>

### 14. 1:1 booking, billing and availability

> Where does a family book a 1:1 session?

- **Expected:** Answer what sources cover, then say who handles the rest
- **Should cover:** gives a booking link only if a source provides one; otherwise routes to Paul
- **Routes to:** Paul at College Money Method (especially for deadline-sensitive cases)
- **Waiting on CMM content:** should say it doesn't have the fact and route, never guess
- **Eval (rules + playbooks):** 3/3 phrasings pass

<details><summary>Other phrasings</summary>

- `14-b`: A family needs a 1:1 before the Nov 1 early deadline. Is there availability?
- `14-c`: How is a school-sponsored 1:1 session billed versus one the family pays for?

</details>

### 15. What support is included

> Can families email College Money Method directly with FAFSA questions?

- **Expected:** Answer from sources, with citations
- **Should cover:** families use the Resource Center and workshops; counselors can contact CMM with questions
- **Routes to:** Paul at College Money Method
- **Waiting on CMM content:** should say it doesn't have the fact and route, never guess
- **Eval (rules + playbooks):** 1/3 phrasings pass

<details><summary>Other phrasings</summary>

- `15-b`: What support is included in our partnership?
- `15-c`: Can I email you a question about a specific family's CSS Profile?

</details>

### 16. Workshop dates, recordings, slides

> When is our next workshop and who is it for?

- **Expected:** Answer from sources, with citations
- **Should cover:** the school's upcoming workshops with dates and registration links; which grades each workshop fits
- **Eval (rules + playbooks):** 3/4 phrasings pass

<details><summary>Other phrasings</summary>

- `16-b`: Where can families find recordings of past workshops?
- `16-c`: Which workshop should I recommend for sophomore parents?
- `16-d`: Can I get the slides from the FAFSA workshop to share with families?

</details>

### 17. Language access

> We have a lot of Spanish-speaking families. Are the workshops available in Spanish?

- **Expected:** Answer what sources cover, then say who handles the rest
- **Should cover:** what language support exists, only from its sources; routes interpreter funding to Paul
- **Routes to:** Paul at College Money Method (interpreter funding arrangements)
- **Waiting on CMM content:** should say it doesn't have the fact and route, never guess
- **Eval (rules + playbooks):** 2/3 phrasings pass

<details><summary>Other phrasings</summary>

- `17-b`: Can our school pay for an interpreter at a live workshop?
- `17-c`: Do the recordings have captions?

</details>

### 18. Hub access and logins

> How do I add a new counselor to the Hub?

- **Expected:** Hand off; share standing info only if a source gives it
- **Should cover:** the school's Hub admin adds team members; pass the person's name and email to Paul if it is not resolved
- **Routes to:** the school's Hub admin, then Paul at College Money Method
- **Waiting on CMM content:** should say it doesn't have the fact and route, never guess
- **Eval (rules + playbooks):** 3/3 phrasings pass

<details><summary>Other phrasings</summary>

- `18-b`: A colleague can't log in to the Counselor Hub. What should she do?
- `18-c`: I'm new at my school. How do I get Hub access?

</details>

### 19. Resource Center content issues

> The link to the CSS Profile guide in the Resource Center is broken. Who fixes it?

- **Expected:** Answer what sources cover, then say who handles the rest
- **Should cover:** points to an existing resource on the topic if one is in its sources; sends broken or missing resources to the content team
- **Routes to:** the CMM content team (Paul)
- **Eval (rules + playbooks):** 3/3 phrasings pass

<details><summary>Other phrasings</summary>

- `19-b`: Is there a resource on financial aid for divorced parents? I couldn't find one.
- `19-c`: One of the FAFSA guides still says 2024-25. Can that be updated?

</details>

### 20. Contracts, workshop dates, invoices

> When will we get our contract for next year?

- **Expected:** Hand off; share standing info only if a source gives it
- **Should cover:** routes contract, invoice and date-change requests to Paul
- **Routes to:** Paul at College Money Method
- **Waiting on CMM content:** should say it doesn't have the fact and route, never guess
- **Eval (rules + playbooks):** 5/5 phrasings pass

<details><summary>Other phrasings</summary>

- `20-b`: Can you send me the invoice for this year's workshops?
- `20-c`: What's the payment schedule for the partnership?
- `20-d`: We need to move our March workshop to a different date. Who do I talk to?
- `20-e`: Our business office needs a W-9 from you. How do I get one?

</details>

## Routing questions (R1–R5)

### R1. Partnership inquiries

> A counselor at another school asked me about College Money Method. How can they learn more or see pricing?

- **Expected:** Hand off; share standing info only if a source gives it
- **Should cover:** routes to Paul for pricing or a call
- **Routes to:** Paul at College Money Method
- **Waiting on CMM content:** should say it doesn't have the fact and route, never guess
- **Eval (rules + playbooks):** 2/2 phrasings pass

<details><summary>Other phrasings</summary>

- `r1-b`: We talked with you last year about a partnership. How do we restart that conversation?

</details>

### R2. Speaking and school visits

> Could Paul speak at our family weekend in April? What would it cost?

- **Expected:** Hand off; share standing info only if a source gives it
- **Should ask:** the date, audience, location and budget
- **Should cover:** does not quote fees
- **Routes to:** Paul at College Money Method
- **Waiting on CMM content:** should say it doesn't have the fact and route, never guess
- **Eval (rules + playbooks):** 1/2 phrasings pass

<details><summary>Other phrasings</summary>

- `r2-b`: Is Paul available to visit our campus for a parent night?

</details>

### R3. Workshop hosting

> I'm hosting the next webinar. Where's my host link?

- **Expected:** Answer what sources cover, then say who handles the rest
- **Should cover:** shares host steps only if a source has them; routes host-link requests
- **Routes to:** Paul or the CMM workshop team (for host links)
- **Waiting on CMM content:** should say it doesn't have the fact and route, never guess
- **Eval (rules + playbooks):** 2/2 phrasings pass

<details><summary>Other phrasings</summary>

- `r3-b`: What do I need to do to host a workshop for our families?

</details>

### R4. Logo, slides and usage data

> Can I get the College Money Method logo for our newsletter?

- **Expected:** Answer what sources cover, then say who handles the rest
- **Should cover:** reports the school's program figures it has; routes 1:1 usage numbers and logo requests to Paul
- **Routes to:** Paul at College Money Method (usage data and brand assets)
- **Waiting on CMM content:** should say it doesn't have the fact and route, never guess
- **Eval (rules + playbooks):** 3/3 phrasings pass

<details><summary>Other phrasings</summary>

- `r4-b`: How many of our families have used 1:1 sessions this year?
- `r4-c`: Do you have overview slides I can show at back-to-school night?

</details>

### R5. Counselor call requests

> Can I get 15 minutes with Paul to talk through a family's case?

- **Expected:** Hand off; share standing info only if a source gives it
- **Should cover:** routes the call request to Paul
- **Routes to:** Paul at College Money Method
- **Waiting on CMM content:** should say it doesn't have the fact and route, never guess
- **Eval (rules + playbooks):** 2/2 phrasings pass

<details><summary>Other phrasings</summary>

- `r5-b`: I'd like a quick call to debrief after a family's 1:1. How do I set that up?

</details>
