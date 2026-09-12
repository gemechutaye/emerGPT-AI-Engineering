# Evaluation suites

The JSON suites preserve source-derived expectations and earlier failures.
Evaluation data is never admitted by `config/corpus.json`. A suite used to guide a
fix is regression coverage afterward, even if its filename contains "holdout".
Freeze new expectations before dispatch and record the exact code, configuration,
index, prompts, attempts and provider receipts for each real run.

Routine CI uses provider doubles and isolated Postgres. It makes no model-quality
claim. Real-provider evaluations require configured API keys and consume credit.

## Current pipeline

Run the full planner, chunk retrieval, evidence inventory, generation and distinct
verification service against a compatible saved bundle:

```sh
uv run python scripts/evaluate_intelligence_architecture.py \
  --suite evals/intelligence-legacy-regression-20260912.json \
  --bundle /absolute/path/to/compatible-index.sqlite \
  --out .local/evaluation/new-run
```

Use a fresh output directory. `--ids` can select a bounded subset. `--rerank`
enables the optional cross-encoder for an explicit comparison; prepare its pinned
assets first with `uv run emer prepare-retrieval`. The default application does
not enable it. CLI help describes the accepted inputs without making model calls.

Validate mutation fixtures without making provider calls or activating an index:

```sh
uv run python scripts/intelligence_mutations.py \
  --suite evals/intelligence-source-mutations-20260912.json \
  --base-bundle apps/api/tests/fixtures/recorded-retrieval/fts-v2-large.sqlite \
  --out .local/evaluation/mutation-preview --validate-only
```

The older evaluation modules and retrieval-comparison script are retained for
reproducibility. Some historical comparisons require recorded query embeddings
from the privately retained development archive; they are not advertised as a
clean-clone command. See [release contents](../docs/RELEASE_CONTENTS.md) for the
portable fixture mapping and archive inventory.

## Reading results

Inspect required facts, source membership, exact citation spans and semantic
support separately. Include failures and unanswerable cases in the denominator.
Record expected behavior for patient/date collisions, historical policy versions,
missing facts, false premises, conversation corrections and repeated paraphrases.
Ranking recall is measured before generation; answer quality is measured afterward.

Historical compact local evidence is in `docs/evidence/local-acceptance-20260912`.
The `/api/v1/evaluations` endpoint labels retained results as historical and adds
the actual runtime configuration. Earlier 60-trial results are regression evidence,
not independent acceptance of this release. Hosted and physical Live checks remain
separate gates. No perfect-coverage or production-readiness claim is made.
