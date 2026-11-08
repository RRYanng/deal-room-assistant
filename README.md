# Deal Room Assistant

**A research assistant for early-stage investment teams that answers questions about a company's files, with every claim linked to its exact source sentence.**

![The conversation, with the company named above each answer and its evidence open](docs/screenshot_main.jpg)

Upload a company's pitch deck, meeting notes and CRM log, ask questions, and generate a one-page
brief that flags **missing** and **conflicting** information instead of hiding it.

> Independent prototype, not affiliated with or commissioned by any firm. All sample data is synthetic
> and marked `SYNTHETIC SAMPLE`.

## What it does

1. **Import**: upload decks (text PDF, TXT, MD), meeting notes (TXT, MD, PDF) and CRM records (CSV).
   The system suggests the company, material type and date, and you confirm them. It warns if the
   file seems to belong to another company. Choosing several files opens a table with the same
   suggestions and checks on every row. Data survives restarts, and uploading a file with the
   same name replaces the old version, keeping its material date unless you give a new one.
2. **Organize**: files are split into chunks that keep their location (PDF page + paragraph,
   TXT/MD paragraph, CSV row) and are indexed for hybrid search. Key metrics are extracted into a
   table, each with a verbatim quote.
3. **Q&A**: ask a question that names a company, or follow up on the last one. An agent searches
   that company's materials with tools and answers in short claims; each claim can be expanded to show the file, location and quoted
   text. Claims that cite the same passage share one evidence panel, so it is shown only once.
4. **One-click brief**: "focus on traction and risks" → key metrics, recent communications,
   missing information and conflicts, each line with its source.
5. **Missing and conflicting information** is shown explicitly: "Not found in the provided
   materials", and conflicting values are listed side by side with their sources. The system never
   picks a winner. The **Issues** panel lists these per company and updates after every upload or delete.

The screen is one conversation, with an Upload / Ask switch above the input. The top bar has
**Library** (files and key metrics per company), **Issues**, an optional **Company** selector and
**Generate Brief**.

## v2 (in progress): claim check

Branch `feature/v2`. Design: [docs/v2_design.md](docs/v2_design.md).

6. **Claim check**: lists a deck's key claims and the evidence for or against each. Each claim gets
   an evidence status: Contradicted, Possibly overstated, Externally supported, Consistent with
   company data, or Unverified. The program sets the status from the evidence; the model only
   finds evidence and never says whether a claim is true.
7. **Web research**: market figures are checked against public sources, and competitors are listed
   with source links (Tavily, behind a replaceable interface). The company's name is never sent to
   the search engine, and every quote must appear in a page the search returned.
8. **Review**: an investor confirms or dismisses each flag or portfolio signal, with a reason. The memo lists only
   confirmed items as findings. A dismissed flag stays quiet unless its evidence changes.
9. **Portfolio monitoring**: reads a company's quarterly updates and computes three signals by
   rule: cash runway (under 12 months is a warning; high when revenue also grew under 10%), key
   person departure, and a milestone that keeps slipping. The model only quotes the figures.
   Competitors moving faster, customer concentration, regulatory issues and next-round risk are
   further signals worth adding; they are not built.
10. **Memory**: every checked deck's claims are kept per company. A figure that differs from the
   previous deck is shown as "Changed since the last deck" (market figures) or "Updated" (traction,
   team, costs), on the page and in the memo.
11. **Questions for the founder**: every contradicted or possibly overstated claim, portfolio signal and
   changed figure gets one question, written by the program from the verified quotes. A question may
   only contain numbers that appear in the quotes it cites.
12. **Alignment check**: who pays, how the company charges and who benefits, each with a quote, and
   points to discuss (for example a fee tied to amounts recovered), written neutrally as questions.
13. **Regulatory checklist**: per industry, in `config/regulatory.toml` (healthcare: HIPAA, a Business
   Associate Agreement, a security assessment). An item the files do not mention is "Not addressed in
   materials", never a breach; these count in the summary's Regulatory category.
14. **Memo**: summary line, market, competition, verified findings by category, risks (regulatory,
   alignment, risks the deck states), portfolio signals, awaiting review, questions for the founder.

### Evaluation

Cases planted in the synthetic company ClearClaim AI (`samples/company_c/`; public figures in
`samples/company_c/SOURCES.md`). Run with `python scripts/eval_report.py`. These results are
**offline**: the model is FakeLLM scripted to behave like a careful model, and web search is
FakeWebSearch. They test the program's checks and rules, not the real model's judgment.

| Case | What is checked | Result (offline) |
|---|---|---|
| E1 | Gross margin stated two ways in one deck (67-82% vs above 90%) is Contradicted | pass |
| E2 | Market size arithmetic that does not add up ($12B vs 400,000 x $3,600) is Contradicted | pass |
| E3 | Operating cost per employee below the BLS-based floor ($48,651) is Possibly overstated | pass |
| E4 | A projection that assumes away a risk the deck states is Possibly overstated | pass |
| E5 | A product claim with no company evidence (15 hours saved per week) stays Unverified, even next to an industry figure | pass |
| E6 | A claim backed by notes and CRM is Consistent with company data | pass |
| E7 | 400,000 practices vs public data (205,088 dentists, a ceiling) is Contradicted, with a real link | pass |
| E8 | $400B US dental spending vs CMS ($189.2B) is Possibly overstated, with a real link | pass |
| E9 | Competitor research lists competitors with links and contradicts "no other company" | pass |
| E10 | Invented sources and altered quotes are dropped; queries naming the company are refused | pass |
| E11 | Memory: 250,000 practices in the September deck -> 400,000 in December is flagged as changed | pass |
| E12 | Memory never crosses companies (ClearClaim, Tidewell, Kitebridge) | pass |
| E13 | Flags are not findings until confirmed; dismissed stays quiet unless evidence changes; a reason is required | pass |
| E14 | Portfolio signals in the right quarter: Q2 runway warning; Q3 runway high, CTO departure, milestone slip; none in Q1 | pass |
| FP | The same file checked twice gives identical fingerprints | pass |

Whole offline suite at the last run: 451 passed (the 274 v1.1 tests included).

**Live run** (real Claude Opus 5.5 and Tavily, ClearClaim deck, 2026-11-08): E1-E9 matched the
expected statuses in both runs. In the second run 18 of 18 claims found in both runs had the same
fingerprint and status; it cost 8 model calls, about $0.78 and 8 search credits in 78 seconds.
A third run, with memory, flagged 250,000 -> 400,000 practices as changed (E11 live) and gave the
same fingerprint and status for all 20 claims as the second run. Portfolio monitoring with the real
model (E14 live): the expected signal in every quarter and every cash, burn and revenue figure as in
the updates, for 4 model calls and about $0.08. With prompt caching in the research loops, a
fourth run cost about $0.65 instead of $0.78 (87,915 tokens written to cache, 57,145 read back,
10,832 uncached), with the same 20 fingerprints and statuses as the third run. After the business model
page was added, a sixth run still matched E1-E9, found the fee tied to recovered amounts as a point to
discuss, and showed HIPAA, BAA and a security assessment as not addressed, for about $0.61 and 7 search
credits.

## Quick start

Requires Python 3.11+.

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
cp .env.example .env            # then set ANTHROPIC_API_KEY in .env
.venv/bin/python scripts/load_samples.py      # optional: load the synthetic demo companies
.venv/bin/uvicorn app.main:app --port 8000
```

Open <http://localhost:8000>. The embedding model (`all-MiniLM-L6-v2`, about 90 MB) downloads
on first use and is cached by Hugging Face afterwards.

Tests:

```bash
.venv/bin/python -m pytest                 # offline: FakeLLM, no API calls (274 tests)
.venv/bin/python -m pytest -m live         # calls the real models (9 tests)
.venv/bin/python scripts/verify.py         # the 7 required checks end to end → docs/verification.md
```

## Configuration (`.env`)

| Variable | Default | Purpose |
|---|---|---|
| `ANTHROPIC_API_KEY` | – | Claude API key. Without a valid key, uploads still work, but metric extraction, Q&A and briefs report "Model call failed: API key invalid or missing. Check .env and restart the server." The server reads `.env` only at startup. |
| `LLM_PROVIDER` | `anthropic` | `anthropic` or `fake` (development only). |
| `LLM_EXTRACT_MODEL` | `claude-opus-5-5` | Model for metric extraction. |
| `LLM_CHAT_MODEL` | `claude-sonnet-5-5` | Model for Q&A and briefs. |
| `LLM_EFFORT` | `medium` | Reasoning effort (`low` … `max`). |
| `LLM_FALLBACKS` | `default` | Server-side fallback if a model declines a request; empty disables it. |
| `EMBEDDING_MODEL` | `sentence-transformers/all-MiniLM-L6-v2` | Local embedding model. Changing it re-embeds chunks automatically. |
| `DB_PATH` | `data/deal_room.db` | SQLite file (relative paths are relative to the project). |
| `AGENT_MAX_STEPS` | `6` | Max model calls per question or brief. |

All model access goes through `app/llm/` (`get_llm("extract" | "chat")`), so switching provider
or model is a configuration change plus, for a new provider, one adapter class.

## Supported formats

| Material | Formats | Location kept per chunk |
|---|---|---|
| Pitch deck | `.pdf` (text-based), `.txt`, `.md` | PDF: page + paragraph (`deck.pdf p.3 ¶2`); text: paragraph (`notes.md ¶3`) |
| Meeting notes | `.txt`, `.md`, `.pdf` | same as above |
| CRM records | `.csv` with columns `date, contact, summary, next_steps` (extra columns allowed; dates `YYYY-MM-DD`) | row (`crm.csv row 3`) |

Unsupported formats, scanned PDFs without a text layer, non-UTF-8 text, missing CRM columns and
invalid dates are rejected with a message that says what to fix.

## Architecture

```
            ┌──────────── UPLOAD ─────────────────────────────────────────────────────┐
 file ──▶ validate ──▶ parse + chunk (with location) ──▶ embed (local model) ──▶ SQLite
                                                             │
                                                             └─▶ extract metrics (Claude, 1 call/doc)
                                                                   └─▶ program checks quotes, normalizes
                                                                       numbers, fixes units/dates ──▶ SQLite

            ┌──────────── QUESTION / BRIEF ───────────────────────────────────────────┐
 question ──▶ agent loop (Claude tool use, ≤ 6 calls)
                 tools (company_id injected by the server, never chosen by the model):
                   search_documents  → hybrid search: WHERE company_id = ? → BM25 + vectors → RRF
                   get_metrics       → metrics table (value, as-of date, chunk_id, quote)
                   get_conflicts     → program-detected conflicts + unclear-basis metrics
                   get_crm_history   → CRM rows by date
              ──▶ final JSON: claims [{text, citations [{chunk_id, quote}]}], open items
              ──▶ program: keep only quotes found verbatim in a chunk of this company,
                  drop unsupported claims, attach conflicts touching cited chunks
              ──▶ UI: claims with expandable evidence, conflicts, open items
```

**Division of labour**: the *system stores* (files, chunks, vectors, metrics, logs); the
*agent looks things up* through tools and writes the answer; the *program judges* everything that
has a clear rule.

| Done by the model | Done by the program |
|---|---|
| Reading a document and proposing metrics (`value_raw`, quote, as-of date) | Checking every quote is verbatim in the cited chunk; dropping and logging failures |
| Choosing which tools to call and writing claims | Normalizing numbers (`$1.2M` → 1,200,000; `sixty` → 60) and units |
| Writing brief sections that follow the user's requirements | Conflicts (same company + metric + as-of date + unit, different values) |
| Simple one-step calculations in a brief, with the working shown and flagged | "(calculated)" mark, also added for a percentage or multiple not in the cited passage |
| Deciding whether a question is unrelated to company materials | The fixed reply to unrelated questions, with no open items |
| | Unclear basis (no as-of date), missing key metrics, company isolation |
| | Key-metrics table, recent communications, missing/conflict lists in the brief; a model section on communications is dropped |
| Open items in the brief, written as to-dos | One "Confirm which … is correct" to-do per conflict; model items on a missing or conflicting metric are dropped |

**Where data lives**: one SQLite file (`data/deal_room.db`):
`companies`, `documents`, `chunks` (text, location, float32 vector), `metrics`, `crm_records`,
`agent_logs` (every tool call, dropped citation and final answer; viewable at
`GET /api/runs/{run_id}/log`). Deleting or replacing a document removes its chunks, metrics and
CRM rows (cascade). The embedding model is cached in `~/.cache/huggingface`.

## How the company is chosen

The server decides, always. The model never chooses a `company_id`.

**Questions** (`POST /api/ask`), by name matching in the program:

| The question… | Result |
|---|---|
| names one company ("tidewel" also matches) | Answers about it and shows "Answering about: <company>" |
| names no company | Uses the company in the Company selector, which follows the last company used |
| names no company, and the selector is on Auto-detect | "Which company do you mean?" with company buttons |
| names several companies | Says it answers one company at a time and asks which |

The match uses the non-generic words of the name, so "Tidewell" matches Tidewell Insurance but
"insurance" alone matches nothing. A question naming another company still switches to it, and
the selector updates to show that.

**Uploads** (`POST /api/uploads/analyze`): the program checks the file name, then the content.
Only if neither names a known company does the model suggest a name, and that name counts only
if it literally appears in the file. Material type and date come from program rules. Nothing is
stored until you confirm. The confirmation warns, without blocking, if:

- the chosen company differs from the suggestion;
- the file mentions another existing company, or several companies;
- a file with that name would be replaced;
- a new company name resembles an existing one.

**Batch upload** (`POST /api/uploads/batch`): choosing several files opens one table instead of the
dialog, with the same suggestions and warnings on each row. One file still opens the dialog.

![The batch table: Set all to Tidewell, with a warning on the Kitebridge row](docs/screenshot_batch_confirm.jpg)

- **Set all to** puts every row on one company. A company selected at the top of the page is the default.
- A row whose company cannot be recognized must be given one before **Upload all** is enabled. The
  server also refuses the whole batch, storing nothing, if a row has no company.
- Files are stored first, so they can be asked about straight away. Metrics are then extracted in
  the background, one file at a time, and each row shows its progress.
- A failure stays on its own row with the reason. If only extraction failed, the file stays stored,
  and the *re-extract* link in Library can run it again.
- The Issues count updates as rows finish.

Progress is kept in memory. If the server restarts during a batch, the files stored so far stay
stored, but files still waiting are not processed.

## Why hybrid retrieval

Investment questions mix exact tokens and meaning. BM25 is good at exact terms (numbers,
dates, names: "410", "2026-06-30", "Kitebridge"), where embedding models are weak. Embeddings are
good at paraphrases ("last call" ↔ "Meeting with investment team"), where BM25 finds nothing.
The two rankings are merged with **Reciprocal Rank Fusion**: `score = Σ 1/(60 + rank)`. RRF uses
ranks only, so BM25 scores (unbounded) and cosine scores (−1…1) never need to be normalized
against each other. Example from the sample data: for "registered customers", the deck sentence
"100 small businesses have registered" is BM25 #2 but only vector #6; fused, it ranks #3.
Every search filters `WHERE company_id = ?` first, so another company's chunks are never candidates.

At this scale (hundreds of chunks per company), vectors live in SQLite and similarity is one
numpy matrix product; BM25 is rebuilt per query. Both are simple to explain and fast enough.

## API

| Method | Path | Purpose |
|---|---|---|
| GET / POST | `/api/companies` | List / create companies |
| GET / POST | `/api/companies/{id}/documents` | List / upload documents (multipart: `file`, `doc_type`, `doc_date`) |
| DELETE | `/api/documents/{id}` | Remove a document and everything derived from it |
| POST | `/api/documents/{id}/extract` | Re-run metric extraction |
| GET | `/api/documents/{id}/chunks` | Chunks with locations |
| GET | `/api/companies/{id}/search?q=…&k=6` | Debug view of hybrid search with both ranks |
| GET | `/api/companies/{id}/metrics` | Extracted metrics with sources |
| GET | `/api/companies/{id}/checks` | Conflicts, unclear basis, missing key metrics |
| POST | `/api/uploads/analyze` | Suggest company, type and date for a file; stores nothing |
| POST | `/api/uploads/warnings` | Consistency warnings for the company chosen in the confirmation |
| POST | `/api/uploads/batch` | Store several files in the background (multipart: `files`, plus `items` as JSON with `company_id` or `new_company`, `doc_type`, `doc_date`) |
| GET | `/api/uploads/batch/{id}` | Progress per file: `waiting`, `uploading`, `stored`, `analyzing`, `done`, `failed` |
| GET | `/api/issues` | All companies with their issue counts, computed live |
| POST | `/api/ask` | Q&A that works out the company (`{"question": …, "last_company_id": …}`) |
| POST | `/api/companies/{id}/ask` | Q&A for a company the user picked (`{"question": …}`) |
| POST | `/api/companies/{id}/brief` | Brief (`{"requirements": …}`), JSON + Markdown |
| GET | `/api/runs/{run_id}/log` | Agent log for one question or brief |
| POST | `/api/companies/{id}/claim-check` | v2: check the newest deck (or `{"document_id": …}`) in the background |
| GET | `/api/companies/{id}/claim-runs/{run_id}` | v2: progress of a claim check |
| GET | `/api/companies/{id}/claims` | v2: latest claims with evidence, statuses and review state |
| POST | `/api/companies/{id}/reviews` | v2: confirm or dismiss (`{"fingerprint", "decision", "reason"}`) |
| GET | `/api/companies/{id}/memo` | v2: memo, JSON + Markdown |
| POST | `/api/companies/{id}/monitor` | v2: read the company's updates and compute signals in the background |
| GET | `/api/companies/{id}/monitor-runs/{run_id}` | v2: progress of a monitoring run |
| GET | `/api/companies/{id}/monitor` | v2: figures per period and signals |

## Verification

The 7 required checks pass with the real models (`claude-opus-5-5` for extraction,
`claude-sonnet-5-5` for Q&A and briefs); see [docs/verification.md](docs/verification.md) for the
full run, including every answer and citation. The same behaviours are covered by deterministic
FakeLLM tests that run offline on every `pytest`.

What went wrong along the way, and how each problem was found and fixed: [docs/lessons_learned.md](docs/lessons_learned.md).

## Known limitations

- **Text PDFs only**: no OCR. Tables and multi-column layouts in PDFs lose their structure.
- **Paragraph detection is heuristic** (blank lines; short lines without punctuation are treated as
  headings).
- **One extraction call per document**: very long documents may need splitting.
- **Conflicts need an exact match** of metric, as-of date and unit. "Q2 2026" vs "June 30, 2026"
  are not recognized as the same basis, and units for `other` metrics come from the model.
- **Company matching only knows names.** It finds exact names and small typos of companies already in
  the system, not abbreviations or nicknames. A company whose name is a common word can match by accident.
- **Calculations are marked, not re-computed.** "(calculated)" tells the reader to check the working
  shown in the bullet; the program does not redo the arithmetic.
- **Date suggestions are heuristics.** A file name with only a month gives the 1st of that month, so
  check the date in the confirmation.
- **The number parser is English-only** ("sixty", "1.2M"). v1 metrics do not parse ranges such as
  "50–60"; the v2 claim check does ("67-82%", "above 90%").
- **Model output varies between runs.** Citations are always verified, but wording and which
  facts the model chooses to mention can differ.
- **Single user, no authentication**, local SQLite; chat history is not persisted; no streaming.
- **BM25 is rebuilt per query**, which is fine for one company's deal room but not for large corpora.

## Next steps

- Persist Q&A history and briefs per company, with a "what changed since the last brief" diff.
- Period-aware conflict matching (quarters, fiscal years) and an "accept / reject value" workflow
  so a human decision is recorded with its reason.
- More sources: email threads, data-room folders, spreadsheet KPIs (with cell-level locations).
- Evaluation set from real (anonymized) deal materials to tune extraction and retrieval.
- Streaming answers and per-claim confidence signals in the UI.

## Project layout

```
app/
  main.py         FastAPI routes
  config.py       settings from environment
  db.py           SQLite schema
  ingest.py       parsing + chunking with locations, upload/replace
  batch.py        batch upload: stores, then extracts metrics, in the background
  resolve.py      company name matching; which company a question is about
  intake.py       upload suggestions (company, type, date) and consistency warnings
  rag.py          embeddings, BM25 tokenizer, hybrid search with RRF
  extract.py      metric extraction + validation
  numbers.py      number normalization
  citations.py    verbatim quote check
  rules.py        conflicts, unclear basis, missing metrics
  agent.py        Q&A tool loop and citation verification
  brief.py        company brief (program parts + verified model sections)
  logs.py         agent_logs
  llm/            provider-neutral interface, Anthropic client, FakeLLM
static/           conversation UI (HTML, CSS, JS)
samples/          SYNTHETIC SAMPLE files + manifest.json
scripts/          make_samples.py, load_samples.py, verify.py
tests/            pytest suite (offline + `-m live`)
docs/             verification, decisions, lessons learned, demo script, screenshots
```
