# Release and hosting status

September 12, 2026. The clean repository is published; the hosted application is
not live because its managed database and backend origin are not connected.

## Verified release

- Repository: [gemechutaye/emerGPT-AI-Engineering](https://github.com/gemechutaye/emerGPT-AI-Engineering), private, branch `main`.
- Tested code: `ae3605cde60dc22d40348fb254b9f7d8ff6b79f9`.
- Personal commit identity: `GemechuTaye <geme.t07@gmail.com>`.
- [Final GitHub CI](https://github.com/gemechutaye/emerGPT-AI-Engineering/actions/runs/34710408186): **passed**. 1,496 backend tests, 196 frontend tests, nine packaging checks, generated types, lint and production builds.
- Six recorded mutation fixtures validate without provider calls; their frozen expectations are unchanged.
- All 354 tracked release files were checked against actual runtime credentials: zero matches. Original corpus and supplied README hashes match.
- Local container acceptance: **27/27 shared-history checks and 28/28 browser-ownership checks**. All disposable resources were removed.
- Corrected Modal image built: `im-MnkUOl9HeudTdpqCGlZWgl`. The initialization/build App exists with zero running tasks; no public API server is running.

The container checks use the actual image, HTTP server and isolated Postgres.
They cover all 35 sources/eight patients, compiled assets, conversation persistence
across restart, a real database outage returning retryable 503, readiness/liveness,
reconnection without app restart, sharing/reset semantics and cleanup. Shared-mode
startup/restart measured 17.486/17.652 seconds in local amd64 emulation; these are
not Modal latency measurements. The fixtures explicitly use lexical retrieval
without model keys and do not establish hybrid/LLM or physical audio quality.

## What failed and changed

1. Initial combined regression run: 1,484 passed, 12 failed. Older QA fixtures used
   superseded repair and voice-reference contracts, inherited shared-demo mode in
   browser-isolation tests, and expected a selected patient to restrict explicit
   discovery. Updated fixtures retain rejection, ownership and cancellation
   assertions. The full rerun passed all 1,496 tests.
2. Initial GitHub API job: 1,495 passed, one failed because the actual share-page
   HTTP test required compiled frontend assets. CI now builds them before API tests.
3. Initial Vercel deployment rejected a dynamic proxy expression during config
   parsing. Routes now use an explicit deployment environment reference, with
   origin validation inside the build. Compiled routes and seven validation cases
   pass; Vercel now reaches the build and reports the missing backend origin clearly.
4. Docker Desktop smoke attempts timed out; their resources were removed. The
   runner now names and tracks its initializer so client timeouts cannot leave
   untracked containers. Tests continued in the isolated Docker context.
5. The real container rejected fresh ingestion because its index checker removed
   `embedding` from only one side of the comparison. Both sides are now normalized;
   source, policy, chunk and embedding-space validation remain enforced. The test
   fixture now includes the real ingestion shape, including `embedding: null`.
6. The smoke client incorrectly decoded binary assets as UTF-8. It now respects
   content type. Both complete container scenarios subsequently passed.

## Database and hosting boundary

The app's actual history is in **local Postgres `emer`, port 55432**: 224
conversations and 263 runs at the pre-release backup checkpoint. A private
2,586,213-byte application backup was created with SHA-256
`852ab626739f1d38f483256ad015fbe06918c17eeaae9c9b449aa6ab83bbe809`.
The original workspace, corpus, history and raw evidence remain intact. They were
not deleted, reset or used as disposable test databases.

The accessible Supabase `practice-assistant-demo` contains another application's
`pa_*` tables; it does not contain this app's history. New Free project creation
was rejected because both active Free slots are occupied. No Supabase project was
paused, deleted, upgraded or modified. A managed database target must be approved
before transferring the backed-up history and index bundles.

Vercel project `emergpt-ai-engineering` is connected to this repository on `main`,
using `apps/web`, Vite and Node 22. Automatic deployment from Git is proven.
Its verified domain is `emergpt-ai-engineering.vercel.app`. The current build
intentionally fails on missing `EMER_BACKEND_ORIGIN`; no placeholder origin is
configured or deployed. The existing Vercel plan is Pro; no plan or add-on was
purchased.

The private Modal deployment environment is prepared outside Git with mode 0600.
It contains provider configuration and the verified frontend origin, but still
requires the managed `DATABASE_URL`. Setting it to localhost cannot connect Modal
to this Mac. After migration, initialize/check the index, create the Modal Secret,
start the API, configure its public origin on Vercel, and verify the real hosted
path. No account reset or credits purchase was performed.

Historical intelligence scores remain candidate-specific. Hosted retrieval,
provider calls, streaming, source/citation lookup, saved history and Live still
require end-to-end acceptance. Physical microphone/listening checks remain distinct
from synthetic audio fixtures. See [deployment instructions](../deployment/README.md)
and [Vercel setup](VERCEL_SETUP.md).
