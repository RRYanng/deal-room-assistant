# Demo script (about 2 minutes)

## Before the demo

```bash
rm -f data/deal_room.db                       # start clean (optional)
.venv/bin/python scripts/load_samples.py      # Tidewell: deck, notes, CRM · Kitebridge: 3 files
.venv/bin/uvicorn app.main:app --port 8000
```

Open <http://localhost:8000> and select **Tidewell Insurance**. Keep
`samples/company_a/tidewell_update_2026-08.txt` ready to upload.

---

## 0:00 – Setup (15 s)

> "This is a deal-room assistant. For each company, the team uploads a pitch deck, meeting notes
> and the CRM log. Everything here is synthetic sample data. Tidewell is a fictional small-business
> insurance startup. On the left are its three files. On the right are the metrics the system
> extracted, each linked to the sentence it came from."

Point at the **Key metrics** table: registered customers 100, paying customers 60. Hover a
source to show the exact quote.

## 0:15 – Paying customers (20 s)

Click the suggestion **"How many paying customers does the company have?"**

> "The answer is 60, as of June 30, 2026. The deck also says 100, but those are *registered*
> businesses, and the system keeps those two metrics apart."

Click **Show evidence**: file name, paragraph location, and the highlighted sentence in the
meeting notes.

## 0:35 – Last conversation (15 s)

Click **"When was our last conversation and what are the next steps?"**

> "This comes from the CRM log, row 3: the meeting on July 22, and the next step, which is to ask for
> revenue figures and loss ratio data."

## 0:50 – Revenue is missing (15 s)

Click **"What is the company's revenue?"**

> "There is no revenue number in any file, so it says *Not found in the provided materials* and
> lists it as an open item. It doesn't guess. The *Missing key metrics* panel on the right shows the
> same gap. That panel is computed by the program, not the model."

## 1:05 – A conflicting update (25 s)

Upload `tidewell_update_2026-08.txt` (type: Meeting notes, date 2026-08-10).

> "This investor update says 80 paying customers, for the same date, June 30."

The **Conflicts (1)** panel turns red: 60 from the meeting notes and 80 from the update, each with
its quote. Ask the paying-customers question again:

> "Now the answer gives both values with their sources and flags the conflict. It doesn't pick
> one. A person decides which is right."

## 1:30 – Generate Brief (25 s)

Type **"focus on traction and risks"** and click **Generate Brief**.

> "The brief has traction and risk sections written by the model, and every bullet carries a
> source. Each quote was checked word for word against the file before it was shown. Below
> that, the program adds the key metrics, the last three CRM touchpoints, what's missing (revenue)
> and the 60-vs-80 conflict. It can be copied or downloaded as Markdown."

## 1:55 – Close (5 s)

> "The system stores the documents, the agent searches them, and the program checks every
> citation and decides what counts as missing or conflicting."

---

**If something goes wrong:** "API key missing" means `ANTHROPIC_API_KEY` is not set in `.env`.
Uploads still work; extraction can be re-run with the *re-extract* link once the key is set.
