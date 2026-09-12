"""Offline retrieval coverage: which modes/top-k place every required source in the evidence packet.

Uses an existing embedding bundle and pre-computed query vectors (no provider calls). Coverage is
retrieval evidence only; it says nothing about answer quality.

Usage: uv run python scripts/retrieval_coverage.py --bundle artifacts/verification/embeddings-large-20260911/openai-text-embedding-3-large.sqlite \
    --queries artifacts/verification/embeddings-large-20260911/openai-text-embedding-3-large-queries.json --suite evals/development.json
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from emer.services.ingestion import Bundle
from emer.services.retrieval import RetrievalService


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bundle", required=True)
    parser.add_argument("--queries", required=True)
    parser.add_argument("--suite", required=True)
    parser.add_argument("--out")
    parser.add_argument("--embed", action="store_true", help="embed questions lacking a saved vector (one bounded paid call)")
    args = parser.parse_args()
    bundle = Bundle.from_bytes(Path(args.bundle).read_bytes())
    retriever = RetrievalService(bundle)
    queries = json.loads(Path(args.queries).read_text()) if Path(args.queries).exists() else {"vectors": {}}
    suite = json.loads(Path(args.suite).read_text())
    model = bundle.config["embedding"]["model"]
    for case in suite["cases"]:
        # Probe suites annotate must_cite instead of required_sources.
        case.setdefault("required_sources", case.get("must_cite", []))
        case.setdefault("scope", {"patient_id": (case.get("context") or {}).get("patient_id"), "as_of": (case.get("context") or {}).get("as_of")})
    missing = [case for case in suite["cases"] if case["id"] not in queries["vectors"]]
    if missing and args.embed:
        import asyncio

        from emer.providers.openrouter import OpenRouterClient
        from emer.settings import settings

        result = asyncio.run(OpenRouterClient(settings.openrouter_api_key, model).embed([c["question"] for c in missing], model))
        for case, vector in zip(missing, result.vectors, strict=True):
            queries["vectors"][case["id"]] = vector
        Path(args.queries).write_text(json.dumps({"model": model, "vectors": queries["vectors"]}))
        print("embedded", len(missing), "questions; usage:", result.usage.model_dump(exclude_none=True))
    suite["cases"] = [case for case in suite["cases"] if case["id"] in queries["vectors"]]
    rows = []
    families = {p["doc_id"] for p in bundle.config["policies"]}
    for case in suite["cases"]:
        vector = queries["vectors"].get(case["id"])
        required = set(case["required_sources"])
        for mode in ("lexical", "semantic", "hybrid"):
            for top_k in (3, 5, 8, 12):
                packet = retriever.evidence(
                    case["question"],
                    patient_id=case["scope"].get("patient_id"),
                    as_of=case["scope"].get("as_of"),
                    mode=mode,
                    top_k=top_k,
                    query_vector=vector if mode != "lexical" else None,
                    query_model=model if mode != "lexical" else None,
                )
                selected = {source.doc_id for source in packet.sources}
                raw = set(packet.retrieval["raw_top_k_ids"])
                rows.append(
                    {
                        "case": case["id"],
                        "mode": mode,
                        "top_k": top_k,
                        "required": sorted(required),
                        "covered": required <= selected,
                        "raw_rank_covered": (required - families) <= raw,
                        "missing": sorted(required - selected),
                        "packet_size": len(packet.sources),
                    }
                )
    summary = {}
    for mode in ("lexical", "semantic", "hybrid"):
        for top_k in (3, 5, 8, 12):
            cell = [r for r in rows if r["mode"] == mode and r["top_k"] == top_k]
            summary[f"{mode}@{top_k}"] = {
                "cases": len(cell),
                "covered_after_expansion": sum(r["covered"] for r in cell),
                "covered_by_raw_rank": sum(r["raw_rank_covered"] for r in cell),
                "mean_packet_size": round(sum(r["packet_size"] for r in cell) / len(cell), 1),
                "missing_cases": sorted({r["case"] for r in cell if not r["covered"]}),
            }
    report = {
        "bundle_index_id": bundle.id,
        "embedding_model": model,
        "suite": args.suite,
        "corpus_documents": len(bundle.documents),
        "classification": "offline retrieval coverage; not answer quality",
        "summary": summary,
        "rows": rows,
    }
    print(f"{'mode@k':<14}{'covered':>9}{'raw-rank':>10}{'packet':>8}  missing")
    for key, value in summary.items():
        print(
            f"{key:<14}{value['covered_after_expansion']:>4}/{value['cases']:<4}{value['covered_by_raw_rank']:>6}/{value['cases']:<4}{value['mean_packet_size']:>7}  {','.join(value['missing_cases'])}"
        )
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(report, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
