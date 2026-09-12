# Hosting preparation status

September 12, 2026. Release preparation and hosted acceptance are separate.

| Component | Verified | Remaining |
| --- | --- | --- |
| GitHub | Clean source snapshot integrated; personal Git identity configured for the requested private repository. | `main` pushed; corrected GitHub CI passes at `9963480`. |
| Vercel | `emergpt-ai-engineering` exists and is connected to the requested GitHub repo on `main`; Vite, `apps/web`, Node 22 and build settings are configured. Local Vercel build, generated types, 196 frontend tests and production build pass. | Real Modal origin and hosted integration. |
| Modal | Personal CLI profile authenticated; persistent Server and separate initialization App import under Modal 1.5.5. | Runtime Secret, managed database initialization and hosted acceptance. Corrected image `im-MnkUOl9HeudTdpqCGlZWgl` built successfully on Modal. |
| Database | Actual EMER history is in local Postgres `emer` at port 55432: 224 conversations and 263 runs at the pre-release checkpoint. A private application backup succeeded. | Move preserved history and index bundles into an authorized managed database and verify the connection from Modal. |
| Packaging | Nine startup/index checks, shell syntax and offline tokenizer cache pass. Existing local Postgres is reused without creating another cluster. | Shared-history container passes 27 checks; hosted checks still require the remote database. |

No existing Supabase project was paused, deleted or changed. The accessible
`practice-assistant-demo` has an older, different application's `pa_*` tables;
it does not hold this app's history. A new Free project was rejected because the
account already has two active Free projects. No paid resource was purchased.
Setting a cloud environment variable to `localhost` cannot connect Modal to this Mac.

Provider, database and CLI credentials stay outside Git. The original workspace,
corpus, raw evidence and history remain intact. Backup size: 2,586,213 bytes;
SHA-256: `852ab626739f1d38f483256ad015fbe06918c17eeaae9c9b449aa6ab83bbe809`.
The dump is private and is deliberately excluded from the release.

The personal Vercel workspace already uses Pro. No subscription or add-on was
purchased. Git commit email remains `geme.t07@gmail.com` regardless of the
Vercel login email. Local configuration compilation used a nonresolving test
origin only; that origin was never configured remotely or deployed. Generated
static assets contained none of the actual build credential.

The initial combined backend regression run passed 1,484 tests and failed 12.
The failures were older QA fixtures using the superseded repair/voice contracts,
browser-ownership tests inheriting shared-demo mode, and an old selected-patient
expectation for explicit discovery. Fixtures now exercise current contracts and
retain the original rejection, ownership and cancellation assertions. The focused
21-case QA suite passes. The full rerun passes **1,496/1,496 tests** in 153.35 seconds. All six relocated mutation fixtures also validate with zero provider calls. No answer-generation or retrieval implementation was
changed for release preparation. Frozen evaluation expectations remain unchanged.

Historical intelligence scores are candidate-specific. They do not establish
hosted acceptance for this release. Final acceptance must exercise the real
Vercel proxy → Modal → managed Postgres → provider path, streamed answers, exact
citations, refresh/history, source reconstruction, Live and sanitized failures.
Physical microphone/listening acceptance remains separate from synthetic fixtures.

See [deployment instructions](../deployment/README.md) and
[Vercel setup](VERCEL_SETUP.md) for configuration and lifecycle requirements.

Initial GitHub CI passed all 196 frontend tests and failed one of 1,496 backend
tests because its backend job had not built the frontend needed by the real
share-page HTTP test. The workflow now builds those assets before API tests.
Vercel detected the GitHub push automatically but rejected the initial dynamic
proxy expression during configuration parsing. Routing now uses an explicit
deployment environment reference; origin validation runs inside the build.
The corrected local Vercel build and seven origin-validation cases pass. A real
`EMER_BACKEND_ORIGIN` is still required; no placeholder origin is deployed.

The local amd64 image built successfully (258,068,855 bytes). Two smoke attempts
on Docker Desktop timed out during initialization; their temporary databases and
networks were removed. The smoke runner now names and tracks its initializer so
a client timeout cannot leave an untracked container. An isolated Docker context
is being used for the repeat; the timeout is not reported as a pass.

The corrected GitHub CI run [34710078553](https://github.com/gemechutaye/emerGPT-AI-Engineering/actions/runs/34710078553)
passes: 1,496 backend tests, 196 frontend tests, nine packaging tests and both
production builds. Vercel now accepts routing and runs Node 22.23.2; its build
stops at the explicit missing `EMER_BACKEND_ORIGIN` gate. The verified intended
frontend domain is `emergpt-ai-engineering.vercel.app`; it is not a working app yet.

The actual container test then exposed an index-checker bug: unembedded ingestion
includes `embedding: null`, but the checker stripped that field from only one
side of the comparison. Both configurations are now normalized consistently;
source, policy, chunk and embedding-space checks remain enforced. Nine regression
checks and real fresh ingestion pass. The smoke client was also corrected to
handle binary assets without decoding them as UTF-8.

The corrected image passes all **27 shared-history container checks**, including
all 35 sources/eight patients, compiled assets, persisted conversations across
restart, retryable 503/readiness failure during a real database outage, reconnect
recovery, sharing/reset semantics and complete cleanup of its own test resources.
The test uses declared lexical fixtures without model credentials; it does not
certify hybrid/LLM quality or hosted voice. Original history and other apps were
never used by these disposable container tests.
