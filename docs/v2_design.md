# v2 design: from research assistant to diligence co-pilot

Status: agreed 2026-11-07, built on branch `feature/v2`. v1.1 behaviour and its tests stay unchanged.
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
possibly overstated (implausible). Floor: 50% of the average US private-industry employer cost of
one full-time worker. BLS Employer Costs for Employee Compensation, December 2025: $46.15 per hour
worked (https://www.bls.gov/news.release/ecec.htm); x 2,080 hours = about $96,000 a year; 50% =
**$48,000**. Operating costs also include rent, software and payer fees, so a company below half of
the average pay cost alone is very unlikely to be described correctly. The floor is a constant in
code with this source next to it.

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
  (confirmed / dismissed), reason (required), time. Append-only; the latest row wins, so a decision
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

Further signals (competitors moving faster, customer concentration, regulatory issues, next-round
risk) are described in the README but not built.

## 7. Synthetic company ClearClaim AI (`samples/company_c/`)

| File | Planted issue | Eval case |
|---|---|---|
| `clearclaim_deck_2025-09.pdf` (earlier deck) | market: 250,000 practices | E11 drift |
| `clearclaim_deck_2025-12.pdf` (main deck) | gross margin 67-82% on one page, above 90% on another | E1 contradicted (code) |
| | 400,000 practices x $3,600 = "$12B TAM" (really $1.44B) | E2 contradicted (code arithmetic) |
| | 14 full-time employees, annual operating costs $420,000 ($30,000 per FTE) | E3 possibly overstated (implausible) |
| | risk page: payers may change rules; projections assume 0% churn and 98% first-pass acceptance | E4 possibly overstated |
| | "saves front-desk staff 15 hours per week", no evidence anywhere | E5 unverified, never supported |
| | 85 paying practices, also in notes and CRM | E6 consistent with company data |
| | 400,000 independent practices (public data: 205,088 dentists in total, ADA 2025); 40% of claims denied (public: about 15-20%) | E7 contradicted (above a ceiling), E8 possibly overstated, with real links |
| | "no other company offers AI agents for dental claims" | E9 competitor research finds verified competitors |
| `clearclaim_meeting_notes.md`, `clearclaim_crm.csv` | internal evidence for 85 practices and ~70% margin | E1, E6 |
| `clearclaim_update_2026-Q1/Q2/Q3.txt` | runway about 22 -> 11 -> 7 months, revenue growth 15% -> 2%; CTO leaves in Q3; one milestone slips in Q2 and Q3 | E14: Q2 runway warning, Q3 runway high + departure + slip, nothing in Q1 |

More cases: E10 an invented source or altered quote is dropped (the claim falls back to Unverified),
and a query naming the company is refused; E11 drift between deck versions (memory); E12 memory never
crosses companies (all three); E13 approvals (unreviewed not in Findings, dismissed stays quiet,
changed evidence is raised again, reason required); FP the same file checked twice gives the same
fingerprints; E15 v1.1 regression (274 tests, the Tidewell answer is still 60).

## 8. Demo without the network

The v2 demo database is prepared in advance: ClearClaim is loaded and the claim check, research and
monitor run once with the real model and Tavily. Results, web snapshots and links are stored, so the
demo reads stored results and needs no live search. "Re-run" stays available.

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
