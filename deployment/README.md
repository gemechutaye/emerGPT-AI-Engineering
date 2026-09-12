# Deploying EMER

The intended topology is a Vercel frontend, one persistent Modal Server, and
Supabase Postgres. The API owns answer workers, conversation metadata jobs and
Live provider connections. Run one Uvicorn worker and one backend instance until
controller ownership has been adapted and tested for multiple instances.

Vercel's Python, WebSocket and container capabilities do not by themselves prove
that the application's background-worker lifecycle is compatible with Functions.
The persistent backend preserves the current architecture. The container also
includes the built frontend for local verification and a same-origin fallback.

## Modal deployment

Use Modal CLI 1.5.5 and the authorized personal profile. From the release root:

```sh
modal app list --profile emer-personal
modal secret create emer-runtime --env main --profile emer-personal --from-dotenv /absolute/private/deployment.env
modal run --env main --profile emer-personal deployment/modal_app.py::initialize
modal deploy --env main --profile emer-personal --strategy recreate deployment/modal_app.py::app
```

Replace the private file path with a mode-0600 file outside Git containing the
configuration below. The runtime Secret is scoped to this backend; Vercel receives
only its public HTTPS origin. `initialize` belongs to a separate Modal App, so
running migrations and ingestion cannot start the public API. The job has a
five-minute timeout and no automatic retries. Do not use `--force` on an existing
Secret without checking which deployment uses it.

The Server reserves 0.25 CPU and 1 GiB RAM, permits one container, and remains
running between HTTP requests. This is required by the current background answer
tasks and Live controller. A Functions wrapper or scale-to-zero setting would
need lifecycle changes and separate acceptance. Use `recreate` only after active
conversations and Live sessions finish; the current controller cannot safely
route traffic between old and new instances during a rolling deployment.

Continuous operation consumes Modal credit even when no browser is open. The
personal Starter workspace showed $1 usable credit on September 12, 2026; the
additional $29 requires a payment method, which was not added. Account rates were
$0.04730/CPU-hour and $0.008/GiB-hour: the configured reservation is approximately
$0.019825/hour, excluding image builds and jobs. Recheck actual available credit
before a presentation. Existing free credit is not perpetual free hosting.

```sh
modal billing summary --json --profile emer-personal
modal app stop emer-gpt --env main --profile emer-personal
```

Stopping the App removes compute availability but leaves durable state in
Supabase. Redeploy deliberately when needed. No keep-alive automation is used.

## Runtime configuration

Configure these values privately on the backend host:

| Variable | Purpose |
| --- | --- |
| `DATABASE_URL` | Managed PostgreSQL SQLAlchemy/psycopg URL; require TLS for hosted access. |
| `OPENROUTER_API_KEY` | Generation, support checks and embeddings. |
| `OPENAI_API_KEY` | Direct OpenAI Live voice. |
| `APP_ORIGIN` | Exact public frontend origin, without a trailing slash. |
| `COOKIE_SECURE` | `true` for HTTPS deployment. |
| `SHARED_WORKSPACE` | `true` preserves the intentionally shared synthetic demo history. |
| `RETRIEVAL_MODE` | `hybrid` for the intended application. |
| `RETRIEVAL_TOP_K` | `8`, unless the verified retrieval configuration specifies otherwise. |
| `EMBEDDING_MODEL` | Must match the initialized index's model. |

For a persistent server use Supabase's direct TLS connection when reachable, or
its session pooler on port 5432. Validate the actual connection before migrations.
Do not substitute a transaction-pooler URL without separately checking psycopg
prepared-statement compatibility. Supabase's browser Data API and service-role
keys are not needed: the Python service connects to Postgres directly.

The provider model and routing defaults are versioned in the application.
Override models only with a verified deployment configuration. Never put provider
keys, database URLs, local backups or session exports in Vite variables or Git.
The original source corpus and manifest belong in the repository; generated
history and retrieval caches do not.

## Initialize the database once

Use the release checkout and privately configured target `DATABASE_URL`.
The following commands apply migrations and create the real embedding index:

```sh
uv sync --frozen
uv run alembic upgrade head
uv run emer ingest --embeddings --manifest config/corpus.json
uv run python deployment/check_index.py
```

The embedding initialization makes bounded provider calls. Run it deliberately
once for a fresh database. Restarts never rebuild the index or call an embedding
provider. A compatible existing index can be preserved by omitting ingestion and
running the checker. An explicit reindex atomically changes the active index;
historical indexes remain available for saved citations.

`check_index.py` verifies the original source files, active source identity,
record content and configured embedding model using the application's own bundle
reader and retrieval integrity checks. Startup refuses a missing or incompatible
index instead of silently serving lexical retrieval as a hybrid deployment.

The image caches the required tokenizer under `/app/.local/tokenizers` during
build. The Modal image also invokes `emer prepare-retrieval` to prepare pinned
optional reranker assets under `/app/.local/models`. Reranking remains disabled
unless the verified application configuration enables it. When enabled, the
startup checker requires local assets; it does not download weights. The final
image still needs hosted memory and provider acceptance on the final deployment.

## Build and local checks

Run from the repository root, with the intended Docker context selected:

```sh
python3 -m unittest discover -s deployment -p 'test_*.py'
bash -n deployment/start.sh
docker build --platform linux/amd64 -f deployment/Dockerfile -t emer:local .
python3 deployment/verify_image.py --image emer:local --evidence .local/verification/container-shared
python3 deployment/verify_image.py --image emer:local --history-scope browser --evidence .local/verification/container-browser
```

Use fresh evidence directories. The smoke script creates only uniquely named
disposable Docker resources, initializes an explicit lexical test index without
provider credentials, checks API/source/history behavior, restarts the app,
interrupts its database connection, and cleans up its resources. The app container
is limited to 512 MiB. This establishes container mechanics only; hybrid ranking,
reranker memory, provider calls, browser behavior and spoken correctness need
separate acceptance on the final image.

The image runs as a non-root user. `deployment/start.sh` applies serialized
migrations, checks the persisted index and starts one Uvicorn process on `PORT`
(default `8017`). Use `/api/v1/health/ready` as the backend readiness endpoint.

## Frontend and final hosting checks

Import the Git repository into Vercel using `apps/web` as its frontend root.
Use `npm ci`, `npm run build`, and `dist`. Production routing must proxy `/api/*`
to the backend, preserve request bodies and response headers, and serve the SPA
for `/share/*`. The Vite development proxy does not configure production routing.
Do not cache API or shared-conversation responses.

Verify the exact public origin, security headers, API proxy, streamed events,
history, original sources, refresh/reconnection and Live through the hosted URL.
Free backend/database availability, cold wake, model memory and provider failures
must be measured on the selected accounts. No keep-alive traffic or paid resource
is implied by this deployment plan.

For backups, run the application's documented `emer backup` command with a private
target connection; keep its dump under ignored `.local/`. Test restoration into a
new isolated database. Never copy the local Postgres data directory into the image
or overwrite the working database during a restore drill.

References: [Modal Servers](https://modal.com/docs/guide/servers),
[Supabase connections](https://supabase.com/docs/guides/database/connecting-to-postgres),
and [Supabase Free project limits](https://supabase.com/docs/guides/platform/billing-on-supabase).
