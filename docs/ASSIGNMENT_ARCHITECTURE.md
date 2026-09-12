# Application architecture

This describes the source snapshot in this release. Historical evaluation results
apply only to their recorded candidate; they do not certify later changes.

## Question to answer

1. The API establishes the configured shared synthetic workspace or anonymous
   browser ownership and persists the question with its conversation revision.
2. Structured dialogue references resolve follow-ups and corrections. Scope rules
   retain unknown IDs and date distinctions instead of substituting a similar record.
3. `QuestionPlan` decomposes the request within server-owned entity/date permissions.
4. Each part searches exact source chunks using FTS5/BM25, semantic cosine ranking
   or weighted hybrid fusion. Source metadata determines applicability, revision
   relationships and unresolved conflicts independently of relevance scores.
5. A token budget selects evidence passages. A distinct verifier inventories
   supported facts and missing requests. The workflow permits one plan correction
   and one additional evidence search while retaining already-supported passages.
6. Luna generates a structured draft. Original-span validation, source-sentence
   coverage auditing and the distinct verifier check claims, citations and gaps.
   Repair is bounded; an operational failure is not labeled a knowledge absence.
7. A fenced transaction publishes one outcome with its evidence/index identity,
   provider receipts and timing. The UI reads saved results and resolves citations
   against the exact original source version.

The default configuration is hybrid top-8, 32 candidates and 6,000 evidence tokens.
All permitted documents are not automatically placed in the generation context.
The cross-encoder is packaged as an option and disabled by default because its
recorded development run reduced required-source recall. Configuration lives in
[Settings](../apps/api/src/emer/settings.py), not in frontend defaults.

## Storage and ingestion

[The corpus manifest](../config/corpus.json) admits the supplied JSONL only.
Ingestion verifies unique records and original hashes, then creates stable chunks
with exact offsets and source metadata. Immutable bundles contain original text,
FTS5 indexes and the identified embedding space. Postgres stores bundle bytes and
atomically switches the active pointer. Historical answers pin retained bundles;
local SQLite files and model downloads are reconstructable caches.

Postgres also owns conversation context, runs, events, drafts, shares, Live state
and provider receipts. Source documents are immutable. Generated conversation
text, evaluations and research never become retrieval evidence. Application-only
backups include history and the bundles needed to resolve historical citations.

## Boundaries and lifecycle

React/TypeScript/Vite uses generated FastAPI contracts. Domain scope, chunking,
metadata and citation rules are independent of HTTP and provider adapters.
`IntelligenceService` coordinates the bounded retrieval and answering workflow.
HTTP routes and the Live controller use the same service.

Modal hosts one persistent server process. Durable leases, fencing and idempotency
prevent obsolete execution from publishing and avoid automatic rebilling of
uncertain attempts. Background answers and Live controllers currently need that
process to remain available; deployment therefore does not use a request-scoped
Functions wrapper or multiple backend replicas. Vercel proxies API traffic from
the frontend origin; OpenAI carries browser WebRTC media directly.

## Known limits

The corpus is small and exact vector search is sufficient; no ANN throughput or
large-corpus claim is made. Scope and quote validation do not mathematically prove
semantic entailment. Model omissions and conservative abstention still require
source-derived evaluation, including repeated unseen questions.

The default shared demo history is intentional and is not identity-based access
control. Only fictional data is supported. Production use would require authenticated
source permissions, retention controls, a measured larger-corpus retrieval design,
worker/controller routing and stronger operational availability. Physical voice
and hosted acceptance remain separate from mocked and synthetic-media checks.

[Hosting status](HOSTING_STATUS.md) and [evaluation guidance](../evals/README.md)
record the boundaries of the available evidence.
