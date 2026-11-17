# Public demo on Railway

The public demo is the normal app started with `DEMO_MODE=true`. Local development is unchanged: without
`DEMO_MODE` none of this applies.

## What the demo does

| | |
|---|---|
| Data | `data/demo_public.db`: a copy of `data/demo_v2.db` without the logs of past questions, briefs and extractions (ClearClaim AI with Deck check and Portfolio results, plus Kitebridge and Tidewell). On every start, and every day at 00:00 UTC, the working copy (`DB_PATH`) is overwritten with a clean copy. The source file is only read. |
| Turned off | Upload, new company, delete, re-extract, Run deck check, Run monitoring. The buttons are greyed out with "Turned off in the demo"; the server answers 403 if they are called directly. |
| Kept | Every page, Confirm / Dismiss, Memo and memo notes. They cost nothing and are wiped by the daily reset. |
| Limited | Questions and Generate Brief call the model: 5 questions and 1 brief per IP per day, and 100 of both together per day for the whole site. Over a limit the page shows "Demo limit reached. Watch the walkthrough video instead." |
| Banner | "Demo with synthetic data. Upload and Run are turned off." above the page. |

How the limits count:
- A question or brief counts once when it is sent, however many model calls it makes inside.
- Days are UTC days.
- The counts are kept in memory, so a restart or redeploy starts them again.
- The visitor's address is Railway's `X-Real-IP` header. The edge sets this header and replaces any copy the visitor sends.

## Environment variables

Keys are read only from environment variables. Nothing secret is in the code, the repo or the image: `.env` is in
`.gitignore` and `.dockerignore`.

| Variable | Set on Railway? | Value |
|---|---|---|
| `ANTHROPIC_API_KEY` | **Yes, required** | Your Anthropic API key |
| `LLM_CHAT_MODEL` | Optional | Q&A and brief model, default `claude-sonnet-5-5` |
| `LLM_EFFORT` | Optional | Default `medium` |
| `DEMO_ASKS_PER_IP` | Optional | Default `5` |
| `DEMO_BRIEFS_PER_IP` | Optional | Default `1` |
| `DEMO_MODEL_CALLS_PER_DAY` | Optional | Default `100` |
| `TAVILY_API_KEY` | **No** | Not needed: the runs are turned off |
| `PORT` | No | Railway sets it |

The Dockerfile already sets these, and you can leave them:
- `DEMO_MODE=true`
- `DEMO_SOURCE_DB=data/demo_public.db`
- `DB_PATH=data/demo_session.db`
- `WEB_SEARCH_PROVIDER=` (empty)
- `HF_HUB_OFFLINE=1`

## Memory and cost

Measured on 2026-11-17 in the Docker image (linux/amd64, one uvicorn process):

| Moment | Memory |
|---|---|
| Started, idle | 73 MiB |
| After the first question (embedding model loaded) | 503 MiB |
| After two questions and a brief | 507 MiB (peak 508 MiB) |

The embedding model stays loaded, so plan for about 0.5 GB all the time. Set the service's memory limit to 1 GB
to leave headroom for requests that arrive at the same time. The **Free plan (0.5 GB per service) is too small**.

Railway pricing (railway.com/pricing, read 2026-11-17):
- **Hobby** costs $5 a month and includes $5 of usage.
- RAM is $10 per GB per month and CPU is $20 per vCPU per month, billed by the second for what is used.

| | Per month |
|---|---|
| RAM: about 0.5 GB × $10 | about $5 |
| CPU: idle most of the time | well under $1 |
| Railway total on Hobby | **about $5–6** (the $5 fee covers the first $5 of usage) |

The model calls are billed by Anthropic separately. The site total of 100 questions and briefs per day caps them.
Also set a monthly spend limit in the Anthropic Console as a second guard.

## Steps (you do these; nothing here logs in to Railway)

1. Merge `feature/demo-mode` into `main` and push. Railway builds from GitHub: `data/demo_public.db` is the one
   database in the repo (`.gitignore` lets this file through); every other database stays ignored.
2. On railway.com: **New Project → Deploy from GitHub repo**, choose this repository, branch `main`. Railway reads
   `railway.json` and builds the `Dockerfile`; the first build takes a few minutes because it installs PyTorch and
   downloads the embedding model.
3. Open the service, then **Variables**: add `ANTHROPIC_API_KEY`. Add any optional variables from the table above.
4. Open **Settings**:
   - **Networking → Generate Domain**, to get a public URL.
   - **Resource limits**: memory 1 GB.
   - **Replicas**: 1. The limits are counted in memory, so a second replica would double them.
5. Wait for the deploy to turn green. The health check is `/api/health`.
6. Check the live site:
   - The banner shows.
   - Upload is greyed out.
   - Library, Data checks, Deck check and Portfolio open.
   - Ask 6 questions: the 6th shows "Demo limit reached. Watch the walkthrough video instead."

## Local check before deploying

From the project folder, with your key in the shell's environment:

```bash
DEMO_MODE=true DB_PATH=data/demo_mode_session.db .venv/bin/uvicorn app.main:app --port 8006
```

To check the image itself:

```bash
docker build --platform linux/amd64 -t deal-room-demo .
docker run --rm -p 8007:8000 -e ANTHROPIC_API_KEY deal-room-demo
```
