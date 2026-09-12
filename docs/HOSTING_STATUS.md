# Hosting preparation status

September 12, 2026. Release preparation and hosted acceptance are separate.

| Component | Verified | Remaining |
| --- | --- | --- |
| GitHub | Clean source snapshot integrated; personal Git identity configured for the requested private repository. | Remote CI and deployment result after the initial push. |
| Vercel | `emergpt-ai-engineering` exists and is connected to the requested GitHub repo on `main`; Vite, `apps/web`, Node 22 and build settings are configured. Local Vercel build, generated types, 196 frontend tests and production build pass. | Real Modal origin and hosted integration. |
| Modal | Personal CLI profile authenticated; persistent Server and separate initialization App import under Modal 1.5.5. | Runtime Secret, image, initialization and hosted acceptance. |
| Database | Actual EMER history is in local Postgres `emer` at port 55432: 224 conversations and 263 runs at the pre-release checkpoint. A private application backup succeeded. | Move preserved history and index bundles into an authorized managed database and verify the connection from Modal. |
| Packaging | Nine startup/index checks, shell syntax and offline tokenizer cache pass. Existing local Postgres is reused without creating another cluster. | Final container and hosted checks. |

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
