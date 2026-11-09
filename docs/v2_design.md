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

- `app/web/`: a `WebSearchProvider` interface (`search(query) -> [url, title, content]`) with
  `TavilyProvider` and `FakeWebSearch`. `WEB_SEARCH_PROVIDER` in `.env` selects one; offline tests
  use the fake. Another provider (e.g. Exa) is one more class.
- Queries are about the industry and the claim itself ("number of dental practices US"), never the
  company: code rejects a query containing the company name. This also keeps the deal confidential.
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

## 19. Known limitations and open points (2026-11-08)

- **Search results vary between runs.** In the demo database build, the search did not return the ADA
  page; E7 is Contradicted from three secondary pages (a vendor blog quoting 135,333 practice
  establishments, a mailing-list site quoting 135,665, a dentist's blog quoting 202,485 dentists). The
  status held; the source quality did not. The first question to the founder names the vendor page's
  title ("Dental Industry Statistics reports 135,333...") rather than a publisher.
- **The page and the memo count differently.** The page counts all 22 claims (20 awaiting review); the
  memo leaves out claims keyed "other" (18 checked, 19 awaiting review including the 4 changes). Both
  are correct by their own rule, but the numbers differ side by side.
- **"Risks the deck states" is in the Regulatory category**, so a business risk ("payers may change claim
  submission rules") shows as a Regulatory unverified item. It is plausible for this deck, not in general.
- **One sentence can give two claims** ("0% customer churn and 98% first-pass claim acceptance"); the memo
  shows them as two identical lines.
- **The alignment reader lists a market-size sentence as pricing** ("At $3,600 per practice per year, our
  total addressable market is $12B"). It does state a price, so it is kept.
- **The page says where the checklist lives** ("kept in config/regulatory.toml"), which is for
  developers, not investors.
- **The desktop app's preview launcher could not read the new demo files** ("Operation not permitted"),
  so the walkthrough server was started from a shell with `scripts/demo_v2.sh`. Started from Terminal,
  the script works.
