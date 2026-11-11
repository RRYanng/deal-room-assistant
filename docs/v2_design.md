# v2 design: from research assistant to diligence co-pilot

Status: agreed 2026-11-07, built on branch `feature/v2`; features frozen on 2026-11-08 (bug fixes only).
v1.1 behaviour and its tests stay unchanged.
All sample data is synthetic (`SYNTHETIC SAMPLE`).

## 1. Architecture

The program controls the flow; models read and propose evidence. The orchestrator is code (a fixed
sequence), not a model choosing what to do, so every run is predictable and testable with FakeLLM.
Each specialist keeps its instructions, tools and output schema in `skills/<name>/SKILL.md`.

| Specialist | Input | Output | Tools and reuse |
|---|---|---|---|
| Librarian (v1) | company + question | claims with verified citations | `agent.run_agent`, 4 tools, `verify_items` |
| Claim checker | the company's decks | claims (verbatim quote, fixed key, number) + evidence + status | extraction follows `extract.py` (quote must be verbatim, numbers from `numbers.py`); internal evidence through the librarian's tools |
| Market researcher | claims about the market or industry | external evidence: URL, verbatim quote, figure, stance | `search_web` |
| Competitor researcher | a sector description written without the company name | competitor list with source links, compared with the deck's competition claims | `search_web` |
| Monitor | quarterly updates | KPIs per quarter, risk signals | KPI extraction with verbatim quotes; signals computed by code |
| Alignment reader (added 11/8) | the deck | quotes for who pays, how it charges, who benefits; tension points of fixed types | one structured call; notes and questions written by code |
| Regulatory checklist (added 11/8) | all the company's files | per-industry items, addressed or not | no model: keywords from `config/regulatory.toml` |

- Company isolation as in v1: the server injects `company_id`; a `company_id` sent by the model is
  dropped. Every new table has `company_id` and every query filters on it.
- New tables only (claims, evidence, web_sources, reviews, memory_entries, kpi_snapshots); existing
  tables and their constraints are unchanged. Quarterly updates are uploaded as `notes`, as the
  Tidewell update already is.
- Guardrail "no invented numbers": every number in model-written text must appear in a verified
  quote or be marked "(calculated)"; otherwise the item is dropped.
- A full check makes many model calls, so it runs in the background with progress, like batch upload.

## 2. Claim status

The model never outputs a status. It outputs evidence (quote, stance, figure); code derives the
status. Highest rule wins:

| Status | Condition | Decided by |
|---|---|---|
| Contradicted | figures disagree (ranges do not overlap, e.g. 67-82% vs above 90%; arithmetic fails, e.g. units x price != stated TAM), or a verified passage states the opposite | numbers: code; text: model stance + code-verified quote |
| Possibly overstated | claim / benchmark >= 1.5; a projection assumes away a risk the deck itself states; implausible cost structure (below) | ratios and floors: code; risk pairing: model finds, code verifies both quotes |
| Externally supported | at least one verified external source agrees (numbers within 10%) | code |
| Consistent with company data | only the company's own other materials agree (the claim's own sentence does not count) | code |
| Unverified | none of the above; the default. Evidence whose quote fails verification is dropped, so the claim falls back here | code |

The column is labelled "Evidence status" with the note "Not a verdict — needs investor review."

**Implausible cost structure.** Operating cost per full-time employee below a floor is flagged
possibly overstated (implausible). Floor: the yearly employer cost of one worker in the lowest-cost
occupation group. BLS Employer Costs for Employee Compensation, December 2025 (released 2026-03-20),
Table 4, private industry, Service occupations: total compensation $23.39 per hour worked
(https://www.bls.gov/news.release/archives/ecec_03202026.htm); x 2,080 hours = **$48,651** a year.
Operating costs also include rent, software and payer fees, so costs below what the cheapest kind of
worker costs in pay alone are very unlikely to be described correctly. The floor is computed in code
from these two numbers, with this source next to it. (Revised 2026-11-08; it was 50% of the
all-worker average.)

## 3. Web research

- `app/web/`: a `WebSearchProvider` interface (`search(query, include_domains=None) -> [url, title, content]`) with
  `TavilyProvider` and `FakeWebSearch`. `WEB_SEARCH_PROVIDER` in `.env` selects one; offline tests
  use the fake. Another provider (e.g. Exa) is one more class.
- Queries are about the industry and the claim itself ("number of dental practices US"), never the
  company: code rejects a query containing the company name. This also keeps the deal confidential.
- Market queries search government and industry-association sites first (`config/sources.toml`) and
  the whole web only when those return nothing (section 20, added 2026-11-09).
- Every result's text is stored in `web_sources`. A URL from the model must be one this run's
  search returned, and its quote must be verbatim in the stored text; otherwise the evidence is
  dropped. This is the v1 citation rule applied to web pages.
- Competitor research: the model writes a sector description without the company name, searches,
  and returns competitors, each with a source URL and a quote that contains the competitor's name.
  The company itself is removed by code. A deck claim with key `competition.landscape` that says
  there are no or few competitors is compared against the verified list.

## 4. Approvals

- Table `reviews`: company, item type (claim / signal / drift), fingerprint, decision
  (confirmed / dismissed), reason (required to dismiss, optional to confirm since 11/8; section 14), time. Append-only; the latest row wins, so a decision
  can be undone.
- Fingerprint = company + item type + fixed-vocabulary key + normalized number(s) computed by code.
  Without a number, the hash of the verbatim source passage is used. Model wording never enters it,
  so running the same file twice gives the same fingerprints (tested).
- Dismissed: not shown in Issues or alerts again, only under "Dismissed (n)" with the reason. When
  the evidence changes (a new number), the fingerprint changes and the item is raised again.
- The memo builds on the Brief. "Findings" lists only confirmed items with the investor's reason;
  unreviewed items are listed under "Awaiting review (n)", marked as not conclusions.

## 5. Memory

- After each check, every claim is stored in `memory_entries` under its fixed-vocabulary key with
  value, quote, file name and date. Entries survive deleting the file (`ON DELETE SET NULL`).
- Code compares a new value with the latest value of the same key from an earlier-dated file:
  "Changed since last check: 250,000 (Sep 2025 deck) -> 400,000 (Dec 2025 deck)". v1 conflicts only
  compare values with the same as-of date; memory catches drift between deck versions.
- Every memory query filters on `company_id`; the memory tool has no company parameter. Tests check
  through `FakeLLM.calls` that nothing from company A reaches prompts or output for company B.

## 6. Portfolio monitoring (signals computed by code)

| Signal | Rule |
|---|---|
| Cash runway | runway = cash / monthly net burn. Runway < 12 months: **warning**. Also revenue growth vs last quarter < 10%: **high** |
| Key person departure | a founder / C-level / VP / Head present last quarter and absent or stated as departed now |
| Repeated milestone slip | the same milestone's target date moved later >= 2 times, or past its date and not done. Extraction is given last quarter's milestone names so it reuses them; code matches by name |

Four further signals (competitors moving faster, loss of a large customer, regulatory issues, a hard
next round) were considered and not built; section 18 says why.

## 7. Synthetic company ClearClaim AI (`samples/company_c/`)

| File | Planted issue | Eval case |
|---|---|---|
| `clearclaim_deck_2025-09.pdf` (earlier deck) | market: 250,000 practices | E11 drift |
| `clearclaim_deck_2025-12.pdf` (main deck) | gross margin 67-82% on one page, above 90% on another | E1 contradicted (code) |
| | 400,000 practices x $3,600 = "$12B TAM" (really $1.44B) | E2 contradicted (code arithmetic) |
| | 14 full-time employees, annual operating costs $420,000 ($30,000 per FTE) | E3 possibly overstated (implausible) |
| | risk page: payers may change rules; projections assume 0% churn and 98% first-pass acceptance | E4 possibly overstated |
| | "saves front-desk staff 15 hours per week", no company evidence; the notes quote an industry survey (20 hours a week on verification) | E5 unverified: industry data is not evidence about the product |
| | 85 paying practices, also in notes and CRM | E6 consistent with company data (the CRM counts; the founder saying it in the meeting is shown, not counted) |
| | 400,000 independent practices (public data: 205,088 dentists in total, ADA 2025); Americans spend $400 billion a year on dental care (CMS: $189.2 billion in 2024) | E7 contradicted (above a ceiling), E8 possibly overstated, with real links |
| | "no other company offers AI agents for dental claims" | E9 competitor research finds verified competitors |
| `clearclaim_meeting_notes.md`, `clearclaim_crm.csv` | internal evidence for 85 practices and ~70% margin | E1, E6 |
| `clearclaim_update_2026-Q1/Q2/Q3.txt` | runway about 22 -> 11 -> 7 months, revenue growth 15% -> 2%; CTO leaves in Q3; one milestone slips in Q2 and Q3 | E14: Q2 runway warning, Q3 runway high + departure + slip, nothing in Q1 |

More cases: E10 an invented source or altered quote is dropped (the claim falls back to Unverified),
and a query naming the company is refused; E11 drift between deck versions (memory); E12 memory never
crosses companies (all three); E13 approvals (unreviewed not in Findings, dismissed stays quiet,
changed evidence is raised again, reason required); FP the same file checked twice gives the same
fingerprints. E15 (added 11/8): a claim only the founder's own statement backs stays Unverified. The
v1.1 regression check first planned as E15 is the whole-suite line under the evaluation table (274
v1.1 tests, the Tidewell answer is still 60).

## 8. Demo without the network

The v2 demo database is prepared in advance: ClearClaim is loaded and the claim check, research and
monitor run once with the real model and Tavily. Results, web snapshots and links are stored, so the
demo reads stored results and needs no live search. "Re-run" stays available. Built on 11/8 with
`scripts/build_demo_v2.py`; started with `scripts/demo_v2.sh` (section 17).

## 9. Schedule

| Day | Backend | UI |
|---|---|---|
| 11/8 | synthetic data; claim checker (extraction, code checks, statuses, fingerprints) | Diligence tab: claims table, status badges, evidence panel |
| 11/9 | web provider (Tavily + fake), market researcher, competitor researcher | external evidence with links; competitor list; run progress |
| 11/10 | eval suite + live tests; approvals + memo | Confirm / Dismiss with reason (in-page dialog); Dismissed (n); memo view |
| 11/11 | monitor; memory; demo database prepared; README eval table | quarter timeline with signals; "Changed since last check" notes. **Features frozen in the evening** |
| 11/12 | bug fixes only, browser walkthrough, Loom | — |

If behind: drop memory first (11/11 noon), then monitoring (11/11 evening). Claim check, research,
evaluation and approvals are not cut.

What happened: everything above, plus the alignment check, regulatory checklist, questions for the
founder and the memo restructure, was built by 11/8, and features were frozen that evening instead of
11/11. 11/9 to 11/12 are for bug fixes, rehearsal and the Loom.

## 10. Decisions made during the build (2026-11-07 night)

- **Evidence window**: other materials count as evidence only if dated within 60 days of the deck;
  the deck itself, undated files, the earlier deck (drift is memory's job) and later updates do not.
- **Numbers decide numeric claims**: evidence for a claim with a number must state a number; the
  program compares and sets the stance, whatever stance the model gave. A stated range or bound is
  used as written (67-82% and above 90% do not agree); only single values get +/-10%.
- **External comparison**: the model labels a public figure as the same quantity or an upper bound
  (dentists are a ceiling on practices). Same quantity: within 10% supports, at least 1.5x
  overstates, otherwise contradicts (including a claim that is lower). Upper bound (revised
  2026-11-08): above the bound is contradicted by any margin; under it gives no conclusion. The 1.5x
  rule applies only to the same quantity.
- **E3** is attached to both the cost claim and the headcount claim, since either may be wrong.
- **Fingerprint** = company + item type + key + numbers read by the program; a claim with no number
  uses the hash of its verbatim source sentence (document text, not model wording).
- **"Evidence changed"** means the claim's status or its evidence signature (source, stance, number,
  quote of each item) differs from what the investor saw; then a dismissed or confirmed claim goes
  back to Awaiting review. A new number is a new fingerprint, so it is a new item.
- **The company name is masked** ("[the company]") in everything the research models read, in
  addition to refusing queries that contain it.
- **Memo** is built by the program from the claim check only (no model call); the Brief stays as it is.
  Claim flags do not change the v1 Issues count, so v1 behaviour is unchanged.
- **A failed re-run** shows an error and keeps the last good results on screen.
- Skill folders use hyphens: `skills/claim-check`, `skills/market-research`, `skills/competitor-research`.

## 11. Changes on 2026-11-08

- Upper bound: any claim above a public ceiling is contradicted; the 1.5x rule is only for the same quantity.
- Cost floor: BLS Service occupations, $23.39 x 2,080 = $48,651 (section 2).
- The denial-rate claim (vendor sources only) is replaced by "Americans spend $400 billion a year on
  dental care", checked against CMS National Health Expenditures ($189.2 billion in 2024) and the ADA
  Health Policy Institute ($189 billion). New key `market.industry_spend_usd`.
- Industry data is not evidence about a company's own results (keys in `COMPANY_OWN_KEYS`): internal
  evidence needs scope "company", and web sources are never counted for those claims. Trap in the
  notes: an industry survey of 20 hours a week next to the deck's "15 hours per week".
- The unverified YC batch of a real competitor was removed from SOURCES.md.

## 12. Changes on 2026-11-08 (afternoon) and portfolio monitoring

- Industry data that a company cites (scope "industry") is shown with "Company cites an industry
  survey (not independently verified)" and never counts, so it cannot give "Consistent with company data".
- A competitor list is compared only with claims that assert something about competitors (none, only,
  first, faster...). New key `market.status_quo` for how customers work today.
- Competitor names are matched after normalizing; one brand word in the quote is enough. A competitor
  known only from another competitor's site is dropped. Every web page has a source type:
  government/association, news, competitor site, other.
- The 135,333 "ADA" practice count traced to a Census County Business Patterns establishment count
  (2020, NAICS 621210) quoted by a vendor; the Census table needs an API key, so it is not used.
- Each run records tokens and an estimated dollar cost (`app/llm/pricing.py`).
- Portfolio monitoring (`app/monitor.py`, `skills/portfolio-monitor/SKILL.md`): a notes file is a
  quarterly update only if it reports cash, net burn or revenue. The model quotes the figures; the
  program computes runway (warning under 12 months; high with revenue growth under 10%, growth
  calculated from two revenues when both are known), key person departure (founder, C-level, VP, Head
  of; absence counts only when the update lists the team) and milestone slips (target moved later at
  least twice, or passed and not done). Departure is level high; a slip is a warning. Signal
  fingerprints use company, kind, period and the numbers or names, so a second run gives the same ones.
  Signals cannot be confirmed or dismissed yet (added in the evening, section 13).

## 13. Signal reviews and memory (2026-11-08, evening)

- Signals reuse the reviews table (item type "signal"): a reason is required, the latest decision
  wins, and a dismissed signal comes back when its level or its basis (calculation and quotes)
  changes. Confirmed signals are in the memo with the investor's note.
- Memory (`app/memory.py`): each claim check stores the deck's claims in `memory_entries` (kept when
  the deck is deleted). An earlier deck that was never checked is read once, claims only. Changes
  against the most recent earlier deck use the same tolerance as everything else. Market, unit and
  product figures that move are "changed"; traction, team and costs are "updated". Claims keyed
  "other", risks, competition, status quo and projection assumptions are not compared. Drift items
  cannot be confirmed or dismissed yet (added later the same day, section 14).

## 14. Page review round (2026-11-08)

- **Summary box** at the top of the claim check: issues per category (Market, Financials, Competition,
  Team, Regulatory; contradicted, possibly overstated, or changed since the last deck), unverified
  counts beside them, and the three items that matter most. Strength decides the order: a calculation
  or an official source first, then company files, then other web pages. Dismissed items are not counted.
- Key numbers and conclusions are bold; keys are shown as labels ("Gross margin", not
  `unit.gross_margin_pct`); evidence has one headline line each ("CMS: $165.3 billion; deck says $400
  billion"). When a government or association source is present, "other" web pages are folded.
- Reviews: a reason is optional to confirm and required to dismiss (checked by the server and by a
  database CHECK). Claims, changes since the last deck and signals can all be confirmed or dismissed,
  and dismissed items of every kind sit behind one "Show dismissed (n)" toggle.
- The portfolio table scrolls sideways instead of truncating; times are shown in the viewer's time zone.

## 15. Main features (2026-11-08)

- **Questions for the founder** (`app/questions.py`): one per contradicted or possibly overstated claim,
  signal, changed figure and alignment point, from fixed templates filled with verified quotes. Rule,
  enforced in code: every number in a question appears in a quote the question cites (page and file
  references in parentheses are not checked); otherwise the question is not shown. Calculated values
  (1,440,000,000; 11.0 months of runway) are never put to the founder. Shown under each item and
  collected in the memo, without duplicates.
- **Memo** (`app/memo.py`): rebuilt as a VC investment memo: summary line, market, competition,
  verified findings (confirmed by the team, by category), risks (regulatory checklist, alignment, risks
  the deck states), portfolio signals, awaiting review, questions for the founder, dismissed, notes. It
  only reorganizes results; the summary line is counts and the top item.
- **Alignment check** (`app/alignment.py`, `skills/alignment-check/SKILL.md`): one model call returns
  quotes for who pays, how the company charges and who benefits, and tension points of four fixed types
  (share of amounts recovered, price per transaction, payer is not the beneficiary, revenue from a
  party on the other side of the customer). Quotes must be verbatim. The neutral note and the question
  for each type are code text: a common fee model is described as common, never as a problem. The
  ClearClaim deck got a business model page: a monthly subscription per location plus 8% of amounts
  recovered on denied claims.
- **Regulatory checklist** (`app/regulatory.py`, `config/regulatory.toml`): industries and items live in
  the config file, so adding one needs no code change. The industry is the one whose words appear most
  in the company's files (at least 3 hits). An item is "Addressed in materials" with the sentence that
  mentions it, or "Not addressed in materials", which is never called a violation. Not-addressed items
  count in the Summary's Regulatory category and rank below contradictions in the top three. ClearClaim
  mentions none of HIPAA, a BAA or a security assessment (tested).

## 16. What the founder says is not evidence (2026-11-08)

- Internal evidence that supports a claim but only reports what the founder or management said in a
  meeting ("Maya said ...") is stored with scope "founder", shown, and not counted. A claim backed only
  by it stays Unverified, headed "Only supported by the founder's own statement"; on a claim that has
  other evidence it reads "Founder's own statement (not counted)".
- The program decides, not the model: the evidence comes from a notes file and the sentence around the
  quote has a speech verb (said, told, mentioned, claimed, stated, estimated, according to). The whole
  sentence is read, because the model's quote can leave out "Maya said".
- Company records count: the CRM row "85 paying practices" keeps E6 Consistent with company data.
- Only support is affected. A founder statement that contradicts the deck (around 70% margin vs above
  90%) is an inconsistency the team should see, so it still counts.
- Limitation: a meeting note written as a plain statement ("Gross margin is around 70%") with no speech
  verb is counted as company data. Notes that record what was said should say who said it.

## 17. Demo database and browser walkthrough (2026-11-08)

- `scripts/build_demo_v2.py --from ../backups/deal_room_v1.1.db` copies the v1.1 backup (SHA-256 as in
  `docs/STATE_v1.1.md`), uploads the 7 ClearClaim files with metric extraction, and runs one claim check
  and monitoring with the real model and Tavily. The 11/8 build: 22 claims (E1-E9 all as expected),
  4 signals, 0 review records, about $0.93 and 8 search credits. The result, `data/demo_v2.db`, is not
  in the repository (`data/` is ignored).
- `scripts/demo_v2.sh` serves a fresh copy (`data/demo_v2_session.db`) on port 8002 at every start, so
  a rehearsal never changes the prepared database.
- The walkthrough clicked through claim check, evidence, one confirm and one dismiss, changes since the
  last deck, regulatory and alignment, portfolio and the memo. Fixed from it (each with a test that
  failed on the old code):
  - the founder's statement on a Contradicted claim was headed "Only supported by the founder's own
    statement";
  - the memo asked the gross margin question twice, with the figures in opposite order;
  - the review dialog's example reason ("Founder confirmed 70% margin in the meeting") contradicted the
    founder rule; it now reads "Checked the calculation against the deck" (text only, no test).

## 18. Risk signals considered and not built

Monitoring has three signals, all computed by code from figures quoted in the company's own updates.
Four more were considered. Each needs data the updates do not contain, or a judgment the program
cannot check, so each would break the rule that a signal is a calculation anyone can redo from quotes.

| Signal | What it would need | Why not now |
|---|---|---|
| Competitors moving faster | dated competitor events (launches, funding, customer wins) from the web, compared with the company's own milestones | Web pages rarely give reliable dates, marketing pages overstate, and "faster" is a judgment about different products. A wrong alert about a named real company is worse than none. Searches every quarter for every company also cost credits. |
| Loss of a large customer | revenue per customer over time (CRM with account-level revenue, or the update naming lost accounts) | Updates report totals. Without each customer's share of revenue, a rule cannot tell a large loss from a small one; guessing from text would be the model's opinion. |
| Regulatory issues | ongoing monitoring of enforcement actions and rule changes (for example payer or CMS rules), matched to the company's product | Matching a rule change to a product and judging its effect is legal judgment, and false alarms are costly. The diligence checklist (section 15) covers what the files address; ongoing monitoring needs a reliable source and an expert to judge relevance. |
| A hard next round | a forecast from runway, growth, market conditions and investor appetite | It is a forecast, not a measurement. The measurable part, runway with weak growth, is already the high runway signal. Anything more would be a model's opinion presented as a signal. |

What would make each possible: account-level revenue in the CRM (large customer), a curated feed of
competitor events with dates (competitors), and a reviewed source list per industry with a person
judging relevance (regulatory). The next round stays a conversation, using the runway signal.

## 19. Known limitations and open points (updated 2026-11-09)

- **The model does not always cite the best figure a search returned.** In the first 11/9 build the
  searches returned the Census count of offices of dentists (135,665) and the ADA series ending at 202,485
  dentists, and the model quoted "over 200,000" instead. The exact-figure rule (section 21) now tells it
  to prefer exact figures, and the program refuses hedged ones; which sentence it quotes still varies.
- **Live runs do not always list the same claims.** The final build has 21 claims; "Practices get paid
  faster..." (the live E15 example) was not listed.
- **The alignment reader lists a market-size sentence as pricing** ("At $3,600 per practice per year, our
  total addressable market is $12B"). It does state a price, so it is kept.
- **The summary's top three does not show what was confirmed** (a confirmed item reads the same).
- **The desktop app's preview launcher could not read the new demo files** ("Operation not permitted"),
  so the walkthrough server was started from a shell with `scripts/demo_v2.sh`. Started from Terminal,
  the script works.

Fixed on 2026-11-09 (section 20): page and memo counts, the stated risk's category, labels of two claims
from one sentence, the config path on the page, and Confirm/Dismiss on reviewed items.

## 20. Authoritative sources first, walkthrough fixes, and the rebuild (2026-11-09)

Bug fixes only (feature freeze since 11/8). Each change has a test that failed on the old code, except
the Change button, which is page script only and was checked by clicking in the browser.

- **Authoritative sources first** (fixes the source quality of the demo's main example). Market research
  searches the domains in `config/sources.toml` first, with Tavily `include_domains` (government
  statistics: Census, BLS, CMS, CDC, HHS, HRSA, AHRQ, NIH, data.gov; associations: ADA, ADEA, AAPD, NADP,
  AMA, AHA, NCQA, WHO). If they return nothing, the same query searches the whole web. The model may repeat
  a query with `whole_web: true` to widen it, but only after that query was searched on the listed sites;
  a first `whole_web` request still goes to them. One model query is one unit of the 12-search budget,
  even when it takes two searches. Competitor research is unchanged. The same list labels pages
  "government/association". Each result tells the model which was searched.
- **Open-ended figures are not ceilings.** "Over 200,000" has no upper end, so it gives no conclusion
  as an upper bound, and supports no "too high" finding for the same quantity; a same-quantity claim
  below its stated minimum is still contradicted. Found in the rebuild below.
- **Page and memo counts.** One server function (`reviews.review_counts`) gives both the same numbers:
  claims other than "other" (bios, counted apart), and awaiting review = flagged claims (contradicted,
  possibly overstated, unverified) plus changes since the last deck, which is what the memo lists under
  that heading. Both show "18 claims checked (plus 4 other: bios, descriptions) · 21 awaiting review
  (flagged claims and changes since the last deck) · 0 confirmed · 0 dismissed".
- **"Risk the deck states" is Financials**, not Regulatory; the regulatory checklist stays separate.
- **Two claims of one kind from one sentence** get their figure in the label: "Projection assumption: 0%
  customer churn" and "Projection assumption: 98% first-pass claim acceptance". The phrase is the figure
  and the words after it in the quote, up to a conjunction, preposition or punctuation (at most four).
- **The page no longer names the checklist's config file.**
- **Reviewed items** show the decision and reason with a **Change** button; Change brings back Confirm,
  Dismiss and Cancel.

**Rebuild of `data/demo_v2.db` (once, as agreed).** $0.87 in all: metric extraction $0.14 (7 calls);
claim check 10 calls, 7 search credits, $0.65, 107 s; monitoring 4 calls, $0.08, 23 s; 0 review records.
22 claims (5 contradicted, 5 possibly overstated, 1 consistent, 11 unverified), as on 11/8.
- Searches: 3 market queries, all answered by the listed sites (no whole-web fallback); 4 competitor
  queries on the whole web.
- E7: Contradicted, citing the ADA's 2025 dentist workforce PDF, "There are over 200,000 dentists in the
  U.S.", as an upper bound. Authoritative source, wrong reading (see above).
- E8: Possibly overstated, citing CMS ($189.2 billion in 2024) and the ADA ($189 billion in 2024).
- E9: 8 competitors (Toothy AI, Ventus AI, DentOS, Zentist, Overjet, Needletail AI, Pearl, Stratus).
- E1-E6, E11, E14, E15 as expected. The 9 v1 live tests passed the same day (67 s).

## 21. Exact figures only, and the final demo database (2026-11-09)

- **Rule (general, not for E7):** when a page gives both an exact figure and a hedged one ("over",
  "about", "more than", "nearly", "up to", "200,000+"), the researcher cites the exact figure. A hedged
  figure alone gives no conclusion. The rule is in the market-research skill (both the description of what
  the program does and the prompt section) and at the top of the research prompt. The program enforces
  it: `research.is_hedged` checks value_raw and the words just before the figure in the quote (so a hedge
  the model leaves out of value_raw still counts), and such evidence is dropped. Tests cover hedged and
  exact figures, a hedge on another number in the same sentence, the exact figure on a page that also has
  a hedged one, and the prompt text.
- **Final rebuild of `data/demo_v2.db`** (the last one; the database is now frozen). $0.85 in all: metric
  extraction $0.15 (7 calls); claim check 10 calls, 7 search credits, $0.62, 188 s; monitoring 4 calls,
  $0.08, 22 s; 0 review records. 21 claims: 5 contradicted, 5 possibly overstated, 1 consistent, 10
  unverified. 3 market searches, all answered by the listed sites; no hedged evidence was dropped.
  - E1 both gross margin claims Contradicted; E2 $1.44B, not $12B; E3 cost and headcount Possibly
    overstated; E4 both projection assumptions Possibly overstated; E5 15 hours Unverified; E6 85 paying
    practices Consistent (CRM; the founder's statement shown, not counted).
  - E7 Contradicted: ADA Health Policy Institute, *The evolving dental practice model* (2022), "This
    includes 191,654 dentists out of 202,401 practicing dentists in the US." Relation: upper bound.
  - E8 Possibly overstated: ADA, "National dental expenditure was $189 billion in 2024, representing 3.6%
    of total health expenditure."
  - E9 Contradicted; 9 competitors (Ventus AI, Lassie, Zentist, Zuub, Needletail AI, Vyne Dental, Stratus,
    Curve Dental Eligibility+, DentalXChange).
  - E11 both market changes (250,000 -> 400,000; $900M -> $12B); E14 all 4 signals. E15 not listed.
  - Page and memo: 17 claims checked (plus 4 other), 20 awaiting review, 17 founder questions.

## 22. Two parts hidden on the page, and tabs (2026-11-10)

- **Switches, not deletions.** `config/ui.toml` has `[show] changes_since_last_deck = false` and
  `regulatory_checklist = false`. The back end is unchanged: memory and the checklist are still computed
  and stored on every run, and `GET /api/companies/{id}/claims` and `/memo` answer in full by default, so
  every existing test runs as before. The page reads the switches from `GET /api/ui` and passes
  `?hide=changes,regulatory`; `presentation.hide_parts` then leaves out the changes (and memory's run note,
  "Read the earlier deck ...") and the checklist before the counts, the summary and the memo are built.
- **What disappears:** the "Changed since the last deck" and "Regulatory checklist" panels; the Regulatory
  category of the summary box (it only held checklist items, since "Risk the deck states" is Financials);
  changes and unaddressed checklist items in the top three; the questions on changes and checklist items
  (no claim-card question comes from either); in the memo, the market changes, confirmed and awaiting
  changes, the Regulatory risks subsection, their questions and the run note. Awaiting review then counts
  flagged claims only.
- **Final demo database with the switches:** Summary 3 Market, 5 Financials, 1 Competition, 1 Team; 17
  claims checked (plus 4 other), 16 awaiting review; memo with 12 founder questions (17 less 2 change and 3
  checklist questions). Checked by clicking through the page and the memo.
- **Tabs:** Issues (summary, the hidden panels when switched on, and the 10 contradicted or possibly
  overstated claims), All claims (all 17, status counts, the 4 other claims behind a toggle, competitors),
  Alignment. The line with the last check and its cost sits above the tabs.
- Tests: `tests/test_ui_switches.py` (5 tests, failing on the old code): the config file, the full answer
  without `hide`, hidden parts leaving the page data and the memo, the memory note, and unknown names
  ignored. The tabs are page script only and were checked in the browser.

## 23. Founder questions folded on cards (2026-11-10)

On claim cards and portfolio signal cards the question starts folded as a clickable "Ask the founder ▸";
clicking it shows the question ("▾") and it stays open when the page re-renders (switching tabs, after a
review). (Since section 24 the Alignment question and those on changes and checklist items fold too.) The memo's "Questions for the founder" section is unchanged. Page script only, so it was
checked by clicking in the browser; there is no automated test.

## 24. Layout pass (2026-11-10)

Front end only: no back-end change, no new feature, and the demo database was not rebuilt.

- **Claim cards fold to one line**: status, label and a one-sentence conclusion in bold (the counted
  evidence behind the status). Clicking the line opens the quote, the evidence and links, "Ask the founder ▸"
  and the review buttons. Every founder question on the page now starts folded, the Alignment one included.
- **Summary box**: only the issue count per category and the three most important items. The explanation
  text and the "(n unverified)" counts next to the issue counts are gone; unverified claims are counted on
  the All claims tab.
- **Display wording**: evidence headlines are stored as the run wrote them, so the page rewrites them for
  display only. "here" becomes the claim's own page ("67–82% on p.5, above 90% on p.9"), paragraph marks are
  dropped in headlines, amounts of a million or more are shortened ($1,440,000,000 → $1.44B), "x" becomes
  "×" and ranges get an en dash. Quotes are never rewritten. The memo is unchanged (it is the next step).
- **Layout**: the claim check and portfolio content is one column of about 800px with more line height,
  fewer borders and less small grey text. Each status has one colour: contradicted red, possibly overstated
  amber, externally supported green, consistent with company data teal, unverified grey; "To discuss" is blue.
- **Names**: the top-bar "Issues" is now "Data checks" (conflicts and gaps in the files themselves), so it is
  not confused with the Issues tab of the claim check. Only the label changed.
- **Run buttons**: "Run claim check" and "Run monitoring" are secondary buttons (white with a blue border).
  Each opens a confirmation in the page with what the run does and its time and cost, taken from the last
  run (or a typical figure when there is none). Nothing is sent until Run is clicked.
- **Run line**: "Checked Nov 9, 2026 · about $0.62 · 3 min". The file, model calls, tokens, web searches and
  seconds are in the hover text.

Checked by clicking in the browser at desktop and phone widths; Cancel in both confirmations sends no
request. All offline tests pass; there is no automated test of the page script.

## 25. Runway chart and the memo as a document (2026-11-10)

Front end only, like section 24; the demo database was not rebuilt. After this the UI is frozen: bug fixes
only.

- **Portfolio runway chart**: above the quarter table, a small SVG drawn by the page (no new library): runway
  per update (Q1–Q3 2026: 22, 11, 7 months) and a dashed line at the 12-month warning. Each point is labelled
  with its value and, when the runway rule fired, its level ("11 months · Warning", "7 months · High"), and
  hovering a point shows the quarter, the value and the file it was calculated from. The chart only draws
  what the server calculated; the 12 in the page is the same threshold the server's rule uses. On a narrow
  screen the chart scrolls sideways inside its box, like the table.
- **Memo as a document**: the page renders the memo's Markdown as headings, paragraphs and lists instead of a
  monospaced block. Status words keep their status colour; links show the site name and keep the full
  address.
- **Same wording as the page**: the memo text goes through the same display rewriting as the claim check
  ("here" → the claim's page, $1,440,000,000 → $1.44B, "x" → "×", en dash in ranges). Quoted text, bracketed
  citations such as "(clearclaim_deck_2025-12.pdf p.5 ¶2)" and links are left exactly as written. Copy
  Markdown and Download .md use the rewritten text, so what is shared matches what is shown. The back end's
  memo Markdown (`GET /api/companies/{id}/memo`) is unchanged.
- **Print / Save as PDF**: while the memo is open, printing hides everything else (top bar, buttons, other
  panels) and uses black on white even in dark mode, with headings kept with their text. Checked by printing
  to PDF with headless Chrome after clicking Claim check and Memo, in light and dark mode: the first page is
  the memo title, summary and market section, 6 pages in all. In the browser the button was clicked with
  `window.print` replaced by a stub, so no print dialog opened.
