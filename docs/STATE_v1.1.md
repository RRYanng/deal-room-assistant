# State at v1.1

The last state before v2 work starts: the full feature set, how it was tested, what the demo
database holds, and how to get back to exactly this point (code and data).

- Git tag: `v1.1` (this file is part of the tagged commit). The earlier rollback point is `v1-demo`.
- Date: 2026-11-07.
- All sample data is synthetic and marked `SYNTHETIC SAMPLE`.

## Features

**Upload**
- Decks (text PDF, TXT, MD), meeting notes (TXT, MD, PDF) and CRM records (CSV).
- The program suggests company, material type and material date; the user confirms them. The
  company comes from name matching first (file name, then content). The model is called only when
  that finds nothing, and its answer counts only if the name is really in the file.
- Warnings that never block: the file looks like another company's, mentions another company,
  mentions several companies, would replace a file with the same name, or the new company name
  resembles an existing one. A failed model call is reported as such, not as "no company found".
- A file with the same name for the same company replaces the old version. With no new material
  date, the replacement keeps the old date, and the confirmation starts from it.
- **Batch upload**: choosing several files opens a table with one row per file (file name,
  company, material type, material date, status), "Set all to", the company selected at the top as
  the default, the same checks per row, and a check for two files with the same name in one batch.
  A row with no company blocks the upload. Files are stored first, then metrics are extracted one
  file at a time in the background. Each row shows its status; one failure does not affect the
  others, and the Issues count updates when the batch is done.
- Deleting a file asks for confirmation in the page. Data survives restarts.

**Organize**
- Chunks keep their location: PDF page and paragraph, TXT/MD paragraph, CSV row.
- Hybrid search: BM25 plus local embeddings (`all-MiniLM-L6-v2`).
- Key metrics are extracted by the model into a table, each with a verbatim quote that the program
  checks against the file. Values are shown through one formatting function.

**Ask**
- One conversation. A question that names a company goes to that company; a follow-up stays on
  the company in the selector; otherwise the app asks "Which company do you mean?".
- An agent searches the company's materials with tools and answers in short claims. Every claim
  has citations whose quotes are checked word for word; claims that share a passage share one
  evidence panel.
- "Not found in the provided materials" when nothing supports an answer. Conflicting values are
  shown side by side and never resolved by the system. Off-topic questions are declined.

**Brief**
- Generate Brief with free-text requirements. The model writes the requested sections; the program
  adds key metrics, the last three CRM touchpoints, missing information, conflicts and open items.
  Calculated figures are marked "(calculated)". Copy as Markdown or download `.md`.

**Issues and Library**
- Issues per company = conflicts + missing key metrics + values with an unclear basis.
- Library lists files and key metrics per company, with delete and re-extract.

**Robustness**
- A red notice if the page fails to start; static files are linked by content hash.
- API errors say what failed (for example a bad API key) instead of a normal-looking reply.

Design reasons for all of the above are in `docs/decisions.md` (entries 1–79).

## Tests

| Suite | Command | Result at v1.1 |
|---|---|---|
| Offline (FakeLLM, no API calls) | `.venv/bin/python -m pytest` | 274 passed |
| Live (real models) | `.venv/bin/python -m pytest -m live` | 9 passed |

Also checked by hand in the browser before tagging: the batch upload walkthrough in
`docs/demo_script.md`, the single-file demo flow (fourth file creating the 60 vs 80 conflict),
and replacing `tidewell_deck.pdf` with an empty date (it keeps 2026-07-15) in both upload paths.
`scripts/verify.py` was not rerun for v1.1; `docs/verification.md` is from an earlier run.

## Demo database

The demo database `data/deal_room.db` is not in git (`data/*` is ignored). A copy is kept outside
the repository, next to the project folder: `../backups/deal_room_v1.1.db` (relative to the
repository root).

| | Tidewell Insurance | Kitebridge Finance |
|---|---|---|
| Files | 3: `tidewell_deck.pdf` (deck, 2026-07-15), `tidewell_meeting_notes.md` (notes, 2026-07-22), `tidewell_crm.csv` (CRM, 2026-07-22) | 3: `kitebridge_deck.pdf` (deck, 2026-04-10), `kitebridge_meeting_notes.md` (notes, 2026-04-20), `kitebridge_crm.csv` (CRM, 2026-04-20) |
| Issues | 3: revenue missing; $1.2M funding raised and the 4-minute quote time have no as-of date | 1: revenue missing |
| Conflicts | none (the 60 vs 80 conflict appears only after the demo uploads `tidewell_update_2026-08.txt`) | none |

Totals: 6 files, 35 chunks, 7 metrics, 5 CRM records; the Issues badge shows 4.

Backup SHA-256: `840da62feb744d6d1f5e269c945a93372cc3372ea29b20be9da89731050de7ec`.
The live database's hash changes whenever a question is asked, because each run is logged, so
compare against the backup, not against a running copy.

## Restoring v1.1

### 1. Code

In a fresh folder:

```bash
git clone https://github.com/RRYanng/deal-room-assistant.git
cd deal-room-assistant
git switch --detach v1.1
```

In an existing checkout, to look at v1.1 without changing any branch:

```bash
git fetch --tags
git switch --detach v1.1
```

To start new work from v1.1, make a branch from it: `git switch -c <name> v1.1`.

Making `main` itself v1.1 again discards every commit after the tag, on GitHub too. Do it only on
purpose:

```bash
git switch main
git reset --hard v1.1
git push --force-with-lease origin main
```

### 2. Environment

Python 3.11+ (v1.1 was tested with 3.12).

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
cp .env.example .env
```

Then set `ANTHROPIC_API_KEY` in `.env`.

### 3. Database

Stop the server first, then copy the backup into place:

```bash
cp ../backups/deal_room_v1.1.db data/deal_room.db
shasum -a 256 data/deal_room.db
```

The hash must match the one above. If the backup is lost, the same six files can be loaded again
with `rm -f data/deal_room.db` and `.venv/bin/python scripts/load_samples.py`. That re-extracts
metrics with the model, so check that the Issues count is still 4.

### 4. Start and check

```bash
.venv/bin/uvicorn app.main:app --port 8000
```

- `http://localhost:8000/api/companies` lists Kitebridge Finance and Tidewell Insurance with
  3 documents each.
- The Issues badge shows 4: Tidewell Insurance 3, Kitebridge Finance 1.
- "How many paying customers does Tidewell have?" answers 60, as of June 30, 2026, citing
  `tidewell_meeting_notes.md`.
