# Release contents and provenance

This repository is a clean application export. The original corpus, original TXT README, frozen evaluation inputs and copied evidence retain their exact bytes. Code was exported from a recorded source snapshot, then release configuration, test compatibility and documentation were updated here. Preparing this export does not delete or rewrite the original checkout or its Git history.

## Included

- Python API/domain/provider code, versioned prompts and database migrations.
- React/TypeScript source, generated API types, browser assets and their existing provenance/license notices.
- Locked Python/npm dependencies, build configuration and the explicit corpus manifest.
- The supplied fictional corpus and original TXT README, verified against their known SHA-256 hashes.
- Automated tests, author-created evaluation suites/freeze records and repeatable verification scripts. Evaluations never become application knowledge.
- A compact set of historical local acceptance results under `docs/evidence/local-acceptance-20260912/`, with preserved failures and limitations. These results certify only their recorded candidate; release changes require new validation.
- A file-level export manifest and a portable hash inventory of raw development evidence retained locally. No local database, provider credential, receipt dump directory or raw microphone recording is exported.

The exporter preserves all public application assets. It does not decide whether individual font/media licenses permit distribution; their notices remain available for the release review.

## Kept outside this repository

The original checkout retains raw provider experiments, ranking matrices, browser recordings, screenshots, intermediate candidate snapshots, historical plans, local database state, backups, caches and installed dependencies. No deletion is part of this operation. The generated `docs/evidence/development-archive-manifest.json` lists original relative paths, sizes and hashes without requiring the original Mac's absolute path. Resolve those paths against the privately retained original development checkout.

Source `.git`, `.env*`, `runtime.env`, `.local`, `.venv`, `node_modules`, generated `dist`, Python caches, logs, database dumps and build caches are excluded. Deployment files, root README, Git/hosting configuration, AGENTS/build instructions and stale design/planning reports are not imported. Release-owned configuration is prepared separately.

## Preserved regression dependencies

Four existing recorded-data checks depend on three historical files. They are preserved deliberately rather than allowing a clean clone to silently skip them:

| Original evidence | Release fixture | Checks |
|---|---|---|
| `intelligence-20260912/final-blind-http/FBA-002.T1/run.json` | `apps/api/tests/fixtures/recorded-retrieval/compound-question-run.json` | Compound-query raw-rank regression |
| `intelligence-20260912/retrieval-audit/fts-v2-large.sqlite` | `apps/api/tests/fixtures/recorded-retrieval/fts-v2-large.sqlite` | Reference expansion in semantic and hybrid modes |
| `embeddings-large-20260911/openai-text-embedding-3-large.sqlite` | `apps/api/tests/fixtures/recorded-retrieval/legacy-large.sqlite` | Historical bundle compatibility |

Original paths above are relative to `artifacts/verification/`. These SQLite files contain supplied fictional knowledge and recorded embeddings, not application conversations or credentials. The mutation suite's pre-dispatch `source-mutations/freeze.json` is preserved as `evals/intelligence-source-mutations-20260912.freeze.json`.

The fixture relocation patch has been applied only in this release clone. The mutation runner accepts `--base-bundle` for its relocated recorded input while still enforcing the original frozen checksum. Copied originals' checksums remain in the export manifest; release edits are recorded by Git.

## Reproducible preparation

Use Python 3.12 or newer; the exporter uses only the standard library. Freeze a coherent source snapshot and run release checks after integration. The following command only records a plan and writes a proposed relocation patch; it copies no application files:

```sh
python3 scripts/prepare-release.py \
  --source /path/to/original-development-checkout \
  --manifest .local/release-export-plan.json \
  --patch-out .local/recorded-fixture-paths.patch
```

Add `--preserve apps/web/vite.config.ts`, for example, if that file is already owned by release preparation. A preserved path may name a directory. Source deployment files are always outside the allowlist. Existing differing destination files cause a stop; the exporter never overwrites them. Review the plan's exact source/destination paths, sizes and hashes before copying.

At the confirmed stable checkpoint, use the same frozen plan:

```sh
python3 scripts/prepare-release.py \
  --source /path/to/original-development-checkout \
  --manifest .local/release-export-plan.json --apply
git apply --check .local/recorded-fixture-paths.patch
git apply .local/recorded-fixture-paths.patch
```

Any source/list/hash change invalidates the plan. Generate a new filename for a new plan and patch; do not overwrite an old record to conceal a change. The exporter stages verified bytes, rechecks the source and then creates destination files exclusively. Identical existing files are left alone. Symlinks, unsafe paths and changed supplied originals are rejected. It never commits, pushes, purchases resources, runs providers or deletes application data.

After integration, update stale evaluation README statements and portable documentation links, run the release tests/build, verify environment examples contain names only, and validate the actual hosted frontend/backend/database/Live path. Vercel deployment compatibility and all hosted acceptance checks remain separate engineering work; a successful export is not a hosting-readiness verdict.
