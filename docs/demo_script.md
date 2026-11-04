# Demo script (about 2 minutes)

## Before the demo

```bash
rm -f data/deal_room.db                       # start clean: deletes all stored companies and files
.venv/bin/python scripts/load_samples.py      # Tidewell: deck, notes, CRM · Kitebridge: 3 files
.venv/bin/uvicorn app.main:app --port 8000
```

Open <http://localhost:8000>. Keep `samples/company_a/tidewell_update_2026-08.txt` ready to upload.

Model wording changes between runs, so the lines below describe what to expect rather than exact text.

---

## 0:00 – Setup (15 s)

> "This is a deal-room assistant. It's one conversation: you upload files and ask questions here.
> Everything is synthetic sample data. Tidewell is a fictional small-business insurance startup."

Click **Issues**: **Tidewell Insurance · 3 issues**. Click it to show Missing (revenue) and Unclear
basis ($1.2M with no date, for example). Close the panel.

> "The program works these out from the saved files, before anyone asks a question."

## 0:15 – Ask by name (20 s)

Type **"How many paying customers does Tidewell have?"**

> "I didn't select a company. It found the name in the question, and it says who it's answering
> about. The answer is 60, as of June 30. The deck also says 100, but those are *registered*
> businesses, and the system keeps those two metrics apart."

Click **Show evidence**: file name, paragraph, and the highlighted sentence.

## 0:35 – Follow-up (15 s)

Click the shortcut **"When was our last conversation and what are the next steps?"**

> "This question names no company, so it stays on Tidewell, the company now shown in the selector at
> the top. I could also choose a company there myself. The answer comes from the CRM log.
> Both statements come from the same row, so the row is shown once with both parts highlighted."

## 0:50 – Revenue is missing (10 s)

Click **"What is the company's revenue?"**

> "No file has a revenue number, so it says *Not found in the provided materials*. It doesn't guess."

## 1:00 – Upload with a check (30 s)

Switch to **Upload** and choose `tidewell_update_2026-08.txt`.

> "It suggests the company, type and date, and I confirm them. The company came from a name match in
> the program. The model is only called when that finds nothing."

Change **Company** to Kitebridge Finance. A red warning appears: *This file mentions Tidewell
Insurance, but you are adding it to Kitebridge Finance.*

> "If I choose the wrong company, it warns me, but the decision stays with me."

Change it back to Tidewell Insurance. Set the date to 2026-08-10, because the file name only gives
the month. Click **Add to Tidewell Insurance**.

The conversation reports *Tidewell Insurance now has 4 issues (was 3)*. Click **View issues**.

> "The update says 80 paying customers for the same date. Here are both values, each with its file,
> location and quote. It doesn't pick one. A person decides which is right."

Close the panel and click **"How many paying customers does the company have?"**: the answer now
gives both values and flags the conflict.

## 1:30 – Generate Brief (25 s)

Click **Generate Brief** at the top right. Type **"focus on traction and risks"** and click **Generate**.

> "It uses the company in the selector. The model writes the traction and risk sections, and each quote was
> checked word for word against the file. The two conflicting numbers are marked in red. Below that,
> the program adds the key metrics, the last three CRM touchpoints, what's missing and the conflict.
> Open items is a to-do list, starting with which of the two numbers to confirm.
> It can be copied or downloaded as Markdown."

## 1:55 – Close (5 s)

> "The system stores the documents, the agent searches them, and the program checks every
> citation and decides what counts as missing or conflicting."

---

**If there's extra time:**

- *Which company?* Reload the page and click a shortcut: it asks **"Which company do you mean?"** with buttons.
- *One company at a time:* Ask **"Compare Tidewell and Kitebridge"**: it asks which one to answer about.
- *Off-topic:* Ask **"What's the weather?"**: it says it only answers questions about the uploaded materials.
- *Calculation:* With no conflict in the files, ask the brief for "the share of registered businesses that pay": the figure is marked **(calculated)** and shows its working.
- *Library:* Click **Library**, then a company name, to see its key metrics. Each file has a delete
  link, and the Issues count updates after a delete.

**If something goes wrong:** "API key invalid or missing" means `ANTHROPIC_API_KEY` in `.env` is wrong or not set. Fix it and restart the server.
Uploads still work; extraction can be re-run with the *re-extract* link in Library once the key is set.
