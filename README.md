# Deal Room Assistant

A small, working research assistant for early-stage investment teams. Upload a company's pitch
deck, meeting notes and CRM log; ask questions and get answers where **every claim links to the
exact source sentence**; generate a one-page company brief that flags **missing** and
**conflicting** information instead of hiding it.

> Independent prototype, built for my application to the Associate AI Engineer role at Core VC.
> It was not commissioned by Core. All sample data is synthetic and marked `SYNTHETIC SAMPLE`.

![Q&A with conflict panel](docs/screenshot_main.jpg)

## What it does

1. **Import**: per company, upload decks (text PDF, TXT, MD), meeting notes (TXT, MD, PDF) and CRM
   records (CSV). File name, material type and material date are stored, and data survives restarts.
   Uploading a file with the same name replaces the old version.
2. **Organize**: files are split into chunks that keep their location (PDF page + paragraph,
   TXT/MD paragraph, CSV row) and are indexed for hybrid search. Key metrics are extracted into a
   table, each with a verbatim quote.
3. **Q&A**: pick a company, ask a question. An agent searches that company's materials with tools
   and answers in short claims; each claim can be expanded to show the file, location and quoted
   text.
4. **One-click brief**: "focus on traction and risks" → key metrics, recent communications,
   missing information and conflicts, each line with its source.
5. **Missing and conflicting information** is shown explicitly: "Not found in the provided
   materials", and conflicting values are listed side by side with their sources. The system never
   picks a winner.

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
.venv/bin/python -m pytest                 # offline: FakeLLM, no API calls (124 tests)
.venv/bin/python -m pytest -m live         # calls the real models (7 tests)
.venv/bin/python scripts/verify.py         # the 7 required checks end to end → docs/verification.md
```

## Configuration (`.env`)

| Variable | Default | Purpose |
|---|---|---|
| `ANTHROPIC_API_KEY` | – | Claude API key. Without it, uploads still work, but metric extraction, Q&A and briefs report "API key missing". |
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
| | Unclear basis (no as-of date), missing key metrics, company isolation |
| | Key-metrics table, recent communications, missing/conflict lists in the brief |

**Where data lives**: one SQLite file (`data/deal_room.db`):
`companies`, `documents`, `chunks` (text, location, float32 vector), `metrics`, `crm_records`,
`agent_logs` (every tool call, dropped citation and final answer; viewable at
`GET /api/runs/{run_id}/log`). Deleting or replacing a document removes its chunks, metrics and
CRM rows (cascade). The embedding model is cached in `~/.cache/huggingface`.

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
| POST | `/api/companies/{id}/ask` | Q&A (`{"question": …}`) |
| POST | `/api/companies/{id}/brief` | Brief (`{"requirements": …}`), JSON + Markdown |
| GET | `/api/runs/{run_id}/log` | Agent log for one question or brief |

## Verification

The 7 required checks pass with the real models (`claude-opus-5-5` for extraction,
`claude-sonnet-5-5` for Q&A and briefs); see [docs/verification.md](docs/verification.md) for the
full run, including every answer and citation. The same behaviours are covered by deterministic
FakeLLM tests that run offline on every `pytest`.

## Known limitations

- **Text PDFs only**: no OCR. Tables and multi-column layouts in PDFs lose their structure.
- **Paragraph detection is heuristic** (blank lines; short lines without punctuation are treated as
  headings).
- **One extraction call per document**: very long documents may need splitting.
- **Conflicts need an exact match** of metric, as-of date and unit. "Q2 2026" vs "June 30, 2026"
  are not recognized as the same basis, and units for `other` metrics come from the model.
- **The number parser is English-only** ("sixty", "1.2M"); ranges such as "50–60" are not parsed.
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
  rag.py          embeddings, BM25 tokenizer, hybrid search with RRF
  extract.py      metric extraction + validation
  numbers.py      number normalization
  citations.py    verbatim quote check
  rules.py        conflicts, unclear basis, missing metrics
  agent.py        Q&A tool loop and citation verification
  brief.py        company brief (program parts + verified model sections)
  logs.py         agent_logs
  llm/            provider-neutral interface, Anthropic client, FakeLLM
static/           single-page UI (HTML, CSS, JS)
samples/          SYNTHETIC SAMPLE files + manifest.json
scripts/          make_samples.py, load_samples.py, verify.py
tests/            pytest suite (offline + `-m live`)
docs/             verification, decisions, demo script, walkthrough (Chinese)
```
