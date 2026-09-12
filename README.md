# emer-GPT

An AI engineering demonstration that answers questions from the supplied
fictional practice knowledge. Typed questions and Live voice use the same
grounded answer pipeline, original-source citations and saved conversations.
The dataset contains 35 records, including overlapping policy versions and
eight synthetic patient records.

The application is an unlisted synthetic-data demo with shared conversation
history. It has no account or login system. Do not enter real patient information
or confidential data.

## Deployment

- **Vercel:** React/Vite frontend and same-origin API proxy.
- **Modal:** persistent FastAPI, retrieval, answer verification, background tasks
  and the Live voice controller.
- **Supabase/Postgres:** durable conversations, source/index bundles, citations
  and telemetry. Local index files are reconstructable caches.
- **Model providers:** OpenRouter for generation/embeddings and direct OpenAI for
  Live voice. Model and retrieval configuration belong to the verified release.

Follow [Vercel setup](docs/VERCEL_SETUP.md) and
[backend initialization and deployment](deployment/README.md). Provider keys and
database credentials belong only in the backend's private runtime environment.

**Release status:** the application snapshot is integrated in this repository.
The Vercel project is connected to GitHub and its local build passes. The current
EMER history is in local Postgres; managed database migration and the Modal
deployment remain to be completed. See [hosting status](docs/HOSTING_STATUS.md).

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

## Knowledge and voice

The explicit manifest admits only the 35 original records. Ingestion preserves
source hashes, metadata and exact chunk offsets in immutable SQLite FTS5/vector
bundles stored durably in Postgres. Each question is scoped and decomposed, then
retrieved using lexical, semantic or hybrid search. The default is hybrid top-8,
32 candidates and a 6,000-token evidence budget. Optional reranking is disabled
because the recorded development comparison reduced required-source recall.

Luna proposes the answer; a distinct Gemini model checks evidence, coverage and
support. Deterministic checks enforce entity/date permissions and exact original
quotes. Recovery and repair are bounded. Saved conversation references guide a
fresh retrieval; generated answers never become knowledge. Direct OpenAI Live
delegates factual questions through the same backend pipeline.

See [architecture](docs/ASSIGNMENT_ARCHITECTURE.md),
[Live implementation](docs/LIVE_IMPLEMENTATION.md) and [evaluations](evals/README.md).
Automated checks do not establish perfect factual coverage, physical microphone
quality or production readiness. This release retains those limits explicitly.

## Checks

With the local Postgres server available, the database tests create and remove
their own isolated databases:

```sh
uv run ruff check apps/api
OPENROUTER_API_KEY='' OPENAI_API_KEY='' uv run pytest -q apps/api/tests tests/workstream_qa
npm --prefix apps/web run types:check
npm --prefix apps/web test
npm --prefix apps/web run build
python3 -m unittest discover -s deployment -p 'test_*.py'
```

Provider evaluations are separate from routine tests and use existing API credit.
Never add evaluation inputs, generated answers, backups or experiments to the
corpus manifest.

## Repository contents

The release preserves the original corpus and supplied data README, application
source, migrations, dependency locks, tests and evaluation inputs. Local databases,
secrets, installed dependencies, model downloads, raw development captures and
obsolete planning history are excluded. Nothing is deleted from the original
development workspace during export.

See [release contents and evidence](docs/RELEASE_CONTENTS.md) for the export
procedure and retained regression fixtures. Evaluation fixtures are never ingested
as product knowledge. Historical local acceptance evidence is candidate-specific
and does not certify later architecture changes or the hosted application.
