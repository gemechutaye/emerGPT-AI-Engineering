# emer-GPT

A web assistant for asking questions about the supplied fictional practice
records, procedures, policies, operational documents and IT/AI guidance. It supports typed questions and two-way Live
voice, retrieves relevant passages, and returns answers with original-source
citations. Conversations and voice transcripts are saved in Postgres.

The application is an unlisted synthetic-data demo with shared conversation
history. It has no account or login system. Do not enter real patient information
or confidential data. It is not intended for medical care.

## What it does

- Searches the supplied knowledge using keyword, vector or hybrid retrieval.
- Answers follow-up questions using conversation context and a fresh source lookup.
- Shows cited passages with document IDs, titles, versions and effective dates.
- Uses policy dates and authority to distinguish current and superseded guidance.
- Asks for clarification or reports missing evidence when a question cannot be answered.
- Saves conversations, editable drafts and voice transcripts. Run details show
  retrieved evidence, model activity, usage, latency and failures.

Try “What has PT-006 completed?”, then “Have they decided to proceed?” Open a
citation to inspect the record. Ask “Compare the cancellation policy in March
and August 2026” to see version handling, or “What exact cancellation fee does
the policy charge?” to test missing evidence. For voice, start Live, allow
microphone access and ask a question. WebRTC requires localhost or HTTPS.

## Dataset

The [original data README](EMER_AI_TakeHome_Data_README.txt) accompanies
[35 JSONL records](EMER_AI_TakeHome_Knowledge_Corpus.jsonl), including eight
fictional patient records. Each record includes `doc_id`, `title`, `category`,
`version`, `effective_date`, `authority` and `text`. Ingestion preserves these
fields and exact passage locations for citations.

The data README names `EMER_AI_TakeHome_Evaluation_Questions.txt`, but that file
was not supplied. The source-derived questions in [evals/](evals/) were created
for this project. Evaluation inputs and generated answers are never ingested.

## Deployment

- **Vercel:** React/Vite frontend and same-origin API proxy.
- **Modal:** persistent FastAPI, retrieval, answer verification, background tasks
  and the Live voice controller.
- **Supabase/Postgres:** durable conversations, source/index bundles, citations
  and telemetry. Local index files are reconstructable caches.
- **Model providers:** OpenRouter for generation/embeddings and direct OpenAI for Live voice.

Follow [Vercel setup](docs/VERCEL_SETUP.md) and
[backend initialization and deployment](deployment/README.md). Provider keys and
database credentials belong only in the backend's private runtime environment.

**Live demo:** [emergpt-ai-engineering.vercel.app](https://emergpt-ai-engineering.vercel.app).
Vercel is connected to this repository. The deployed Modal API uses the isolated
`emer_gpt` schema in Supabase, with the existing conversations and index bundles
migrated and verified. See [hosting status](docs/HOSTING_STATUS.md) for checks and
remaining limits. Hosting and model access depend on available provider credit.

## Run locally

Use Python 3.13, uv 0.9.24, Node 22 and PostgreSQL 16 client/server tools.
From the repository root:

```sh
uv sync --frozen
npm --prefix apps/web ci
bash scripts/postgres.sh
```

The Postgres helper reuses a server already listening on port 55432. If using an
existing database elsewhere, skip that helper and set `DATABASE_URL` to its
SQLAlchemy/psycopg connection URL. Keep credentials in your shell environment or
`~/.config/emer-build/runtime.env` with mode 0600, outside Git. Required model keys
are `OPENROUTER_API_KEY` and `OPENAI_API_KEY`.

| Setting | Purpose |
| --- | --- |
| `DATABASE_URL` | PostgreSQL connection, using `postgresql+psycopg://…`. |
| `OPENROUTER_API_KEY` | Answer generation, verification and embeddings. |
| `OPENAI_API_KEY` | Direct OpenAI Live voice. |
| `APP_ORIGIN` | Exact frontend origin; the local preview script sets this. |
| `COOKIE_SECURE` | Enable for HTTPS; the local preview script disables it. |
| `SHARED_WORKSPACE` | Defaults to `true`; use `false` for separate anonymous browser histories. |
| `EMER_BACKEND_ORIGIN` | Vercel-only setting pointing to the Modal API's HTTPS origin. |

For a **new database**, initialize the schema and supplied corpus explicitly:

```sh
uv run alembic upgrade head
uv run emer ingest --embeddings --manifest config/corpus.json
uv run python deployment/check_index.py
PORT=4197 bash scripts/preview.sh
```

Open `http://localhost:4197`. Ingestion with embeddings makes a bounded provider
request. Preserve and check an existing compatible index instead of rebuilding it
on every start. Before upgrading an existing history database, run
`uv run emer backup .local/backups/before-upgrade.dump`; backups never belong in Git.

## How it works

Ingestion creates checksummed SQLite bundles with FTS5 search, source chunks
and `text-embedding-3-large` vectors. Each question is split into subquestions
and filtered by patient, date and source applicability before retrieval. The default is hybrid top-8,
32 candidates and a 6,000-token evidence budget. Optional reranking is disabled
because the recorded development comparison reduced required-source recall.

Luna proposes the answer; a distinct Gemini model checks evidence, coverage and
support. Deterministic checks enforce entity/date permissions and exact original
quotes. Recovery and repair are bounded. Saved conversation references guide a
fresh retrieval; generated answers never become knowledge. Direct OpenAI Live
delegates factual questions through the same backend pipeline.

This keeps search, applicability rules, generation and verification separate so
retrieval failures can be inspected before judging an answer. Postgres stores
history and immutable index bundles; the API can reconstruct its local search
cache after a restart. The small corpus fits this design. Production would need a managed search index,
durable workers, access controls, retention rules and evaluation on broader data.

See [architecture](docs/ASSIGNMENT_ARCHITECTURE.md),
[Live implementation](docs/LIVE_IMPLEMENTATION.md) and [evaluations](evals/README.md).
Model checks can still miss unsupported or incomplete claims. Automated tests
and synthetic audio fixtures do not establish physical microphone quality or
end-to-end hosted voice acceptance; those checks remain open.

## Checks

With the local Postgres server available, the database tests create and remove
their own isolated databases:

```sh
uv run ruff check apps/api
npm --prefix apps/web ci
npm --prefix apps/web run build
OPENROUTER_API_KEY='' OPENAI_API_KEY='' uv run pytest -q apps/api/tests tests/workstream_qa
npm --prefix apps/web run types:check
npm --prefix apps/web test
python3 -m unittest discover -s deployment -p 'test_*.py'
```

The frontend build is required by the backend's real HTTP share-page tests.
[Verified release checks](https://github.com/gemechutaye/emerGPT-AI-Engineering/actions/runs/34710408186)
pass 1,496 backend tests, 196 frontend tests and nine packaging tests. Container
checks also cover history across restarts, database outages and recovery.
These are regression and integration results, not a percentage of AI answer accuracy.

Provider evaluations run separately and consume API credit; see [evals/README.md](evals/README.md).

## Project files

`apps/api` contains the Python application and migrations; `apps/web` contains
the React UI. `config/corpus.json` defines ingestion, `evals` holds test questions,
and `deployment` contains the image, startup checks and Modal configuration.
Local databases, secrets, caches and development recordings are excluded from Git.
