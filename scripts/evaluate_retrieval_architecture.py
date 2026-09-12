"""Offline retrieval comparison on explicitly named development/regression suites.

Never opens a holdout, activates an index, or makes an API request. Original-query
ranking is compared separately from scoped evidence assembly; this is not an
end-to-end test of the model planner or answer generator.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
from collections import defaultdict
from datetime import UTC, date, datetime
from hashlib import sha256
from pathlib import Path
from time import perf_counter

import numpy as np
from emer.contracts.knowledge import QuestionPlan, SearchPart
from emer.domain.scope import ScopeError, resolve_scopes
from emer.providers.reranker import MODEL_FILE, MODEL_ID, MODEL_REVISION, get_reranker
from emer.services.ingestion import Bundle, ingest
from emer.services.knowledge_search import KnowledgeSearch, get_chunk_index
from emer.services.retrieval import reciprocal_rank_fusion
from emer.storage.bundles import build_embeddings

ROOT = Path(__file__).resolve().parents[1]
SUITES = (
    "evals/development.json",
    "evals/intelligence-legacy-regression-20260912.json",
)
QUERY_CACHES = (
    "artifacts/verification/embeddings-large-20260911/openai-text-embedding-3-large-queries.json",
    "artifacts/verification/intelligence-20260912/legacy-embedding/embedding-batch-0.json",
)
VECTOR_BUNDLE = "artifacts/verification/intelligence-20260912/retrieval-audit/fts-v2-large.sqlite"
SOURCE_FILES = (
    "config/corpus.json",
    "apps/api/src/emer/services/knowledge_search.py",
    "apps/api/src/emer/services/retrieval.py",
    "apps/api/src/emer/providers/reranker.py",
    "apps/api/src/emer/domain/scope.py",
    "apps/api/src/emer/domain/policy.py",
    "apps/api/src/emer/domain/source_metadata.py",
    "apps/api/src/emer/domain/chunking.py",
    "apps/api/src/emer/services/ingestion.py",
    "apps/api/src/emer/storage/bundles.py",
)


def checksum(path):
    return sha256(Path(path).read_bytes()).hexdigest()


def save(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")


def percentile(values, q):
    return float(np.percentile(values, q)) if values else None


def timing(values):
    return {"n": len(values), "p50_ms": percentile(values, 50), "p95_ms": percentile(values, 95)}


def source_ids(index, chunk_ids):
    return list(dict.fromkeys(index.chunks[key].doc_id for key in chunk_ids))


def measure(expected, ranked):
    expected = set(expected)
    positions = [i + 1 for i, identifier in enumerate(ranked) if identifier in expected]
    return {
        "required_found": len(expected & set(ranked)),
        "required_total": len(expected),
        "all_required": expected.issubset(ranked),
        "reciprocal_rank": 1 / min(positions) if positions else 0.0,
        "missing": sorted(expected - set(ranked)),
    }


def aggregate(rows):
    scored = [row for row in rows if row["required_total"]]
    numerator = sum(row["required_found"] for row in scored)
    denominator = sum(row["required_total"] for row in scored)
    return {
        "scored_cases": len(scored),
        "unscored_no_source_cases": len(rows) - len(scored),
        "required_found": numerator,
        "required_total": denominator,
        "source_recall": numerator / denominator if denominator else None,
        "all_required_cases": sum(row["all_required"] for row in scored),
        "mrr": statistics.mean(row["reciprocal_rank"] for row in scored) if scored else None,
    }


class NoProviderCalls:
    calls = 0

    async def embed(self, *_args, **_kwargs):
        self.calls += 1
        raise RuntimeError("Offline evaluation cannot embed missing corpus inputs")


class CachedScores:
    """Cache exact query/passage scores for fair ranking/context ablations.

    This is an evaluation optimization only. Actual un-cached warm production
    latency is measured separately, so cached assembly is never reported as end-to-end.
    """

    def __init__(self, reranker):
        self.reranker = reranker
        self.identity = reranker.identity
        self.values = {}
        self.inference = []

    def score(self, query, passages):
        missing = list(dict.fromkeys(text for text in passages if (query, text) not in self.values))
        if missing:
            started = perf_counter()
            values = self.reranker.score(query, missing)
            self.inference.append(
                {"query": query, "passages": len(missing), "latency_ms": (perf_counter() - started) * 1000}
            )
            self.values.update({(query, text): value for text, value in zip(missing, values, strict=True)})
        return [self.values[query, text] for text in passages]


def load_inputs():
    suites, cases = {}, []
    for name in SUITES:
        path = ROOT / name
        value = json.loads(path.read_text())
        suites[checksum(path)] = value
        for item in value["cases"]:
            if item.get("turns"):
                raise ValueError("This component comparison accepts standalone development cases only")
            cases.append(
                {
                    **item,
                    "suite": name,
                    "evaluation_id": Path(name).stem + ":" + item["id"],
                    "expected": item.get("expected_sources", item.get("required_sources", [])),
                    "behavior": item.get("expected_behavior", item.get("expected_answerability")),
                }
            )
    query_vectors, provenance, models = {}, [], set()
    for name in QUERY_CACHES:
        path = ROOT / name
        value = json.loads(path.read_text())
        model = value.get("model", value.get("usage", {}).get("model"))
        if not value.get("model"):
            dispatch = json.loads(path.with_name("embedding-dispatch.json").read_text())
            declared = dispatch["model"]
            if model not in {declared, declared.split("/", 1)[-1]} or dispatch["status"] != "completed":
                raise ValueError("Query receipt and completed dispatch disagree about the model")
            model = declared
        if not model:
            raise ValueError("Query cache lacks a verified embedding model identity")
        models.add(model)
        if value.get("suite_sha256"):
            suite = suites.get(value["suite_sha256"])
            if suite is None:
                raise ValueError("Query cache does not match the exact declared suite bytes")
            pairs = [(item["question"], value["vectors"][item["id"]]) for item in suite["cases"]]
        else:
            if value.get("status") != "completed":
                raise ValueError("Query embedding batch did not complete")
            pairs = list(value["vectors"].items())
        for text, vector in pairs:
            if text in query_vectors and query_vectors[text] != vector:
                raise ValueError("Two cached vectors disagree for an identical query/model")
            query_vectors[text] = vector
        provenance.append({"path": name, "sha256": checksum(path), "model": model, "queries": len(pairs)})
    if len(models) != 1:
        raise ValueError("The query caches mix embedding spaces")
    return cases, query_vectors, models.pop(), provenance


def scoped_plan(case, bundle):
    scope = case.get("scope") or {}
    scopes = resolve_scopes(
        case["question"],
        bundle.documents,
        scope.get("patient_id"),
        scope.get("as_of"),
        today=date(2026, 9, 12),
    )
    return QuestionPlan(
        original_question=case["question"],
        planner="offline-deterministic-scopes-no-model-planner",
        parts=[
            SearchPart(
                part_id=item.part_id, query=item.question, requested_information=[item.question], scope=item
            )
            for item in scopes
        ],
    )


async def main(args):
    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    if (out / "report.json").exists():
        raise ValueError("Refusing to overwrite an existing measured report; select a new output directory")
    before = {name: checksum(ROOT / name) for name in SOURCE_FILES}
    for name in SOURCE_FILES:
        target = out / "source-snapshot" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((ROOT / name).read_bytes())
    cases, query_vectors, model, cache_provenance = load_inputs()
    previous = Bundle.from_bytes((ROOT / args.vector_bundle).read_bytes())
    original = ingest(ROOT / "config/corpus.json")
    no_calls = NoProviderCalls()
    bundle = await build_embeddings(original, model, no_calls, previous=previous)
    (out / "evaluation-index.sqlite").write_bytes(bundle.data)
    index = get_chunk_index(bundle)
    cold = perf_counter()
    reranker = get_reranker(str(ROOT / args.model_cache))
    initialization_ms = (perf_counter() - cold) * 1000
    # Prime tokenizer/runtime allocation outside the measured warm samples.
    reranker.score("retrieval warmup", ["A source-grounded knowledge system retrieves source passages."])
    cached = CachedScores(reranker)
    raw_rows, packet_rows, failures, missing_queries, warm = [], [], [], {}, []
    score_labels = []
    for number, case in enumerate(cases, 1):
        query = case["question"]
        vector = query_vectors.get(query)
        lexical = index.lexical(query)
        semantic = index.semantic(vector, model) if vector is not None else []
        modes = {"lexical": lexical}
        if semantic:
            modes.update(semantic=semantic, hybrid=reciprocal_rank_fusion(lexical, semantic))
        else:
            missing_queries[query] = "original query"
        pool = list(
            dict.fromkeys(key for ranking in modes.values() for key, _ in ranking[: args.candidate_k])
        )
        texts = [
            index.documents[index.chunks[key].doc_id].title + "\n" + index.chunks[key].text for key in pool
        ]
        logits = dict(zip(pool, cached.score(query, texts), strict=True))
        score_labels.append(
            {
                "id": case["evaluation_id"],
                "behavior": case["behavior"],
                "has_expected_sources": bool(case["expected"]),
                "max_logit": max(logits.values(), default=None),
            }
        )
        for mode, ranking in modes.items():
            candidate_ids = [key for key, _ in ranking[: args.candidate_k]]
            reordered = sorted(candidate_ids, key=lambda key: (-logits[key], candidate_ids.index(key)))
            for variant, identifiers in (("raw", candidate_ids), ("reranked", reordered)):
                for k in (4, 8):
                    selected = source_ids(index, identifiers[:k])
                    raw_rows.append(
                        {
                            "id": case["evaluation_id"],
                            "suite": case["suite"],
                            "mode": mode,
                            "variant": variant,
                            "k": k,
                            "selected_ids": selected,
                            **measure(case["expected"], selected),
                        }
                    )
        try:
            plan = scoped_plan(case, bundle)
            scoped_vectors = {
                part.part_id: query_vectors[part.query] for part in plan.parts if part.query in query_vectors
            }
            for part in plan.parts:
                if part.query not in query_vectors:
                    missing_queries[part.query] = (
                        "exact scoped query; original vector deliberately not reused"
                    )
            available_modes = (
                ("lexical", "semantic", "hybrid") if len(scoped_vectors) == len(plan.parts) else ("lexical",)
            )
            for mode in available_modes:
                for reranked in (False, True):
                    search = KnowledgeSearch(bundle, reranker=cached if reranked else None)
                    for k in (4, 8):
                        started = perf_counter()
                        packet = search.search(
                            plan,
                            vectors=scoped_vectors,
                            model=model,
                            mode=mode,
                            candidate_k=args.candidate_k,
                            top_k=k,
                            token_budget=args.token_budget,
                        )
                        duration = (perf_counter() - started) * 1000
                        for passage in packet.passages:
                            source = index.documents[passage.doc_id]
                            assert passage.source_sha256 == source.sha256
                            assert source.text[passage.start : passage.end] == passage.text
                        assert packet.retrieval["context_tokens"] <= args.token_budget
                        ids = [source.doc_id for source in packet.sources]
                        packet_rows.append(
                            {
                                "id": case["evaluation_id"],
                                "suite": case["suite"],
                                "mode": mode,
                                "variant": "reranked" if reranked else "unreranked",
                                "k": k,
                                "selected_ids": ids,
                                "assembly_with_cached_scores_ms": duration,
                                "retrieval": packet.retrieval,
                                **measure(case["expected"], ids),
                            }
                        )
            if len(warm) < args.latency_samples and len(scoped_vectors) == len(plan.parts):
                search = KnowledgeSearch(bundle, reranker=reranker)
                started = perf_counter()
                packet = search.search(
                    plan,
                    vectors=scoped_vectors,
                    model=model,
                    mode="hybrid",
                    candidate_k=args.candidate_k,
                    top_k=8,
                    token_budget=args.token_budget,
                )
                warm.append(
                    {
                        "id": case["evaluation_id"],
                        "parts": len(plan.parts),
                        "latency_ms": (perf_counter() - started) * 1000,
                        "candidates": sum(
                            item["candidate_count"] for item in packet.retrieval["scoped_selection"]
                        ),
                    }
                )
        except (ValueError, ScopeError, AssertionError, KeyError, StopIteration) as exc:
            failures.append(
                {
                    "id": case["evaluation_id"],
                    "question": query,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                }
            )
        if number % 10 == 0:
            print(
                json.dumps({"completed_cases": number, "total_cases": len(cases), "failures": len(failures)}),
                flush=True,
            )
    grouped_raw, grouped_packets = defaultdict(list), defaultdict(list)
    for row in raw_rows:
        grouped_raw[f"{row['mode']}/{row['variant']}/k{row['k']}"].append(row)
    for row in packet_rows:
        grouped_packets[f"{row['mode']}/{row['variant']}/k{row['k']}"].append(row)
    after = {name: checksum(ROOT / name) for name in SOURCE_FILES}
    snapshot = (
        ROOT / args.model_cache / ("models--" + MODEL_ID.replace("/", "--")) / "snapshots" / MODEL_REVISION
    )
    report = {
        "created_at_utc": datetime.now(UTC).isoformat(),
        "method": {
            "cases": len(cases),
            "suites": [{"path": name, "sha256": checksum(ROOT / name)} for name in SUITES],
            "raw_ranking": "Identical original query, no scope or policy filter: candidate recall and cross-encoder reordering at fixed k. Chunk ranks deduplicate to original source IDs for recall/MRR.",
            "scoped_packets": "Deterministically parsed scope.question inputs through current KnowledgeSearch. Exact matching cached query vectors only. Does not execute or validate model query planning; uncached scoped queries receive lexical-only comparison.",
            "expected_sources": "Frozen development/regression source-ID labels, including gap-support anchors; not a semantic factual-quality grade.",
            "expansion": "Scoped packet rows include source addressing, patient, version/family expansion and applicability filtering. Raw and expanded metrics are separate.",
            "mrr": "Reciprocal rank of first expected source for original-query rankings; packet MRR is not interpreted because final sources are sorted for publication.",
            "warm_latency": "Actual uncached-score KnowledgeSearch hybrid+rereanker k8 calls, warm model, cached corpus/query vectors; excludes query embedding, model planning/generation and cold model load.",
            "ablation_latency": "Ablation packet assembly reuses exact query/passage logits and is not end-to-end inference latency.",
            "context_budget": args.token_budget,
            "candidate_k": args.candidate_k,
            "source_chunk_span_checks": "Every selected passage checked against exact original Unicode offsets and source hash.",
            "paid_api_calls": no_calls.calls,
            "runtime_activation": False,
            "holdout_opened": False,
        },
        "index": {
            "id": bundle.id,
            "checksum": bundle.checksum,
            "documents": len(bundle.documents),
            "chunks": len(bundle.chunks),
            "config": bundle.config,
            "previous_bundle": args.vector_bundle,
            "previous_checksum": previous.checksum,
        },
        "query_cache": cache_provenance,
        "reranker": {
            **reranker.identity,
            "model_sha256": checksum(snapshot / MODEL_FILE),
            "tokenizer_sha256": checksum(snapshot / "tokenizer.json"),
            "initialization_ms": initialization_ms,
        },
        "script_sha256": checksum(__file__),
        "source_files_before": before,
        "source_files_after": after,
        "source_files_changed_during_run": [name for name in before if before[name] != after[name]],
        "raw_metrics": {key: aggregate(rows) for key, rows in sorted(grouped_raw.items())},
        "scoped_packet_metrics": {
            key: {metric: value for metric, value in aggregate(rows).items() if metric != "mrr"}
            for key, rows in sorted(grouped_packets.items())
        },
        "warm_latency": timing([row["latency_ms"] for row in warm]),
        "reranker_batch_latency": timing([row["latency_ms"] for row in cached.inference]),
        "maximum_context_tokens": max((row["retrieval"]["context_tokens"] for row in packet_rows), default=0),
        "failures": failures,
        "missing_exact_query_vectors": len(missing_queries),
        "calibration": {
            "expansion_trigger": None,
            "reason": "These suites have no independently labeled unrelated-query set; the only no-source case is an unknown patient. This cannot calibrate a general relevance expansion or abstention threshold.",
            "hard_abstention_from_similarity": False,
        },
    }
    save(out / "raw-rankings.json", raw_rows)
    save(out / "scoped-packets.json", packet_rows)
    save(out / "warm-latency.json", warm)
    save(out / "reranker-score-distributions.json", score_labels)
    save(
        out / "missing-query-embeddings.json",
        {"model": model, "queries": missing_queries, "automatic_dispatch": False},
    )
    save(out / "report.json", report)
    lines = [
        "# Offline retrieval architecture development comparison",
        "",
        f"Cases: {len(cases)}. API calls: {no_calls.calls}. Runtime activation: none. Holdout: unopened.",
        "",
        "## Original-query source recall",
        "",
        "| Mode | Required sources found | All-required cases | MRR |",
        "|---|---:|---:|---:|",
    ]
    for key, row in report["raw_metrics"].items():
        lines.append(
            f"| {key} | {row['required_found']}/{row['required_total']} | {row['all_required_cases']}/{row['scored_cases']} | {row['mrr']:.4f} |"
        )
    lines += [
        "",
        "## Scoped evidence packets (including expansion/filtering)",
        "",
        "| Mode | Required sources found | All-required cases |",
        "|---|---:|---:|",
    ]
    for key, row in report["scoped_packet_metrics"].items():
        lines.append(
            f"| {key} | {row['required_found']}/{row['required_total']} | {row['all_required_cases']}/{row['scored_cases']} |"
        )
    lines += [
        "",
        f"Actual warm hybrid+reranker k8 latency: {report['warm_latency']}; maximum evidence tokens: {report['maximum_context_tokens']}/{args.token_budget}.",
        f"Failures: {len(failures)}. Missing exact scoped-query vectors: {len(missing_queries)}.",
        "",
        "## Interpretation",
        "",
        report["method"]["raw_ranking"],
        report["method"]["scoped_packets"],
        report["method"]["warm_latency"],
        report["calibration"]["reason"],
        "Source recall is not answer correctness or citation entailment. This run does not assess model planning, generation, independent verification or spoken output.",
        f"Source files changed during execution: {report['source_files_changed_during_run']}.",
        f"Pinned model: {MODEL_ID}@{MODEL_REVISION}; ONNX SHA256 {report['reranker']['model_sha256']}.",
    ]
    (out / "REPORT.md").write_text("\n".join(lines) + "\n")
    print(
        json.dumps(
            {
                "report": str(out / "report.json"),
                "raw_metrics": report["raw_metrics"],
                "scoped_packet_metrics": report["scoped_packet_metrics"],
                "warm_latency": report["warm_latency"],
                "failures": failures,
                "changed_files": report["source_files_changed_during_run"],
            }
        ),
        flush=True,
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out", default=str(ROOT / "artifacts/verification/architecture-20260912/retrieval-development")
    )
    parser.add_argument("--vector-bundle", default=VECTOR_BUNDLE)
    parser.add_argument("--model-cache", default=".local/models")
    parser.add_argument("--candidate-k", type=int, default=32)
    parser.add_argument("--token-budget", type=int, default=6000)
    parser.add_argument("--latency-samples", type=int, default=12)
    asyncio.run(main(parser.parse_args()))
