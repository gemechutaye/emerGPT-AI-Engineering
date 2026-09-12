"""Source-derived evaluation: freeze expectations, inspect ranking, trace real provider calls.

Never supplies rubric/expected answers to application models. Separate saved outcomes are
reviewed against the frozen rubric; contract checks are not semantic quality scores.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import time
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

import httpx
from emer.contracts.answer import AnswerGap, PublishedAnswer
from emer.domain.conversation_context import discovered_patient
from emer.domain.scope import patient_ids
from emer.evaluation.__main__ import EvaluationClient, Recorder
from emer.providers.openrouter import OpenRouterClient, ProviderError
from emer.services.answering import ANSWER_PROMPT_ID, CHECKER_PROMPT_ID, PROMPTS, AnsweringService
from emer.services.ingestion import Bundle, ingest
from emer.services.provider_activity import NO_RECEIPT_FAILURES
from emer.services.retrieval import RetrievalService
from emer.services.text_intent import resolve_text_intent, self_contained
from emer.settings import settings


def stamp():
    return datetime.now(UTC).isoformat()


def save(path, value):
    Path(path).write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")


def flatten(cases):
    return [
        {**turn, "id": f"{case['id']}.T{i + 1}", "sequence_id": case["id"]}
        for case in cases
        for i, turn in enumerate(case.get("turns") or [case])
    ]


async def capacity(out):
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.get(
            "https://openrouter.ai/api/v1/credits",
            headers={"Authorization": "Bearer " + settings.openrouter_api_key},
        )
        response.raise_for_status()
        value = response.json()["data"]
    save(out / "credit-before.json", {"at": stamp(), **value})
    if value["total_credits"] <= value["total_usage"]:
        raise RuntimeError("Provider capacity exhausted; no dispatch")
    return value


def citations(answer, bundle):
    docs = {d.doc_id: d for d in bundle.documents}
    errors, count = [], 0
    for item in [*answer.statements, *answer.next_steps]:
        for c in item.citations:
            count += 1
            d = docs.get(c.doc_id)
            if (
                not d
                or c.index_id != bundle.id
                or c.source_sha256 != d.sha256
                or d.text[c.start : c.end] != c.quote
                or not 0 <= c.start < c.end <= len(d.text)
                or any(
                    getattr(c, k) != getattr(d, k)
                    for k in ["title", "version", "effective_date", "authority"]
                )
            ):
                errors.append(c.model_dump())
    return {
        "count": count,
        "exact_provenance_errors": errors,
        "semantic_support": "requires independent review",
    }


async def main(args):
    suite_raw = Path(args.suite).read_bytes()
    suite = json.loads(suite_raw)
    cases = suite["cases"]
    if args.ids:
        ids = set(args.ids.split(","))
        cases = [c for c in cases if c["id"] in ids]
        if len(cases) != len(ids):
            raise ValueError("Unknown selected IDs")
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    bundle = (
        Bundle.from_bytes(Path(args.bundle).read_bytes()) if args.bundle else ingest("config/corpus.json")
    )
    retriever = RetrievalService(bundle)
    rows = flatten(cases)
    queries = (
        json.loads(Path(args.queries).read_text())
        if args.queries and Path(args.queries).exists()
        else {"vectors": {}, "questions": {}}
    )
    model = (bundle.config.get("embedding") or {}).get("model")
    if args.command == "embed":
        await capacity(out)
        if not model:
            raise ValueError("Use an embedding-enabled bundle")
        if Path(args.queries).exists():
            raise ValueError("Refusing to rebill existing query artifact")
        texts = list(dict.fromkeys(row["question"] for row in rows))
        receipt = out / "embedding-dispatch.json"
        if receipt.exists():
            raise ValueError("Embedding attempt already dispatched; inspect it before another call")
        save(receipt, {"at": stamp(), "count": len(texts), "model": model, "status": "dispatched"})
        vectors, usage = {}, []
        for batch_start in range(0, len(texts), 48):
            batch = texts[batch_start : batch_start + 48]
            batch_path = out / f"embedding-batch-{batch_start}.json"
            save(batch_path, {"at": stamp(), "status": "dispatched", "count": len(batch)})
            result = await OpenRouterClient(settings.openrouter_api_key, settings.openrouter_model).embed(
                batch, model
            )
            vectors.update(zip(batch, result.vectors, strict=True))
            usage.append(result.usage.model_dump())
            save(
                batch_path,
                {
                    "at": stamp(),
                    "status": "completed",
                    "usage": result.usage.model_dump(),
                    "vectors": dict(zip(batch, result.vectors, strict=True)),
                },
            )
        save(
            args.queries,
            {
                "model": model,
                "suite_sha256": sha256(suite_raw).hexdigest(),
                "vectors": {row["id"]: vectors[row["question"]] for row in rows},
                "questions": {row["id"]: row["question"] for row in rows},
            },
        )
        save(
            receipt,
            {
                "at": stamp(),
                "count": len(texts),
                "model": model,
                "status": "completed",
                "usage": usage,
            },
        )
        print(json.dumps({"count": len(texts), "usage": usage}))
        return
    if args.command in {"rank", "run"} and args.queries:
        if queries.get("suite_sha256") != sha256(suite_raw).hexdigest() or queries.get("model") != model:
            raise ValueError("Query embeddings do not match this frozen suite/model")
        if any(queries.get("questions", {}).get(r["id"]) != r["question"] for r in rows):
            raise ValueError("Query vectors do not match exact questions")
    if args.command == "rank":
        results = []
        for row in rows:
            # Standalone ranking of conversational fragments is diagnostic only, not scored recall.
            vector = queries["vectors"].get(row["id"])
            expected = set(row.get("expected_sources", []))
            for mode in ["lexical", "semantic", "hybrid"]:
                if mode != "lexical" and vector is None:
                    raise ValueError("Missing query vector: " + row["id"])
                for k in [1, 3, 5, 8, 12]:
                    try:
                        packet = retriever.evidence(
                            row["question"],
                            mode=mode,
                            top_k=k,
                            query_vector=vector if mode != "lexical" else None,
                            query_model=model if mode != "lexical" else None,
                        )
                        expected = set(row.get("expected_sources", []))
                        raw = packet.retrieval["raw_top_k_ids"]
                        chosen = [d.doc_id for d in packet.sources]
                        results.append(
                            {
                                "id": row["id"],
                                "mode": mode,
                                "k": k,
                                "expected": sorted(expected),
                                "raw_found": len(expected.intersection(raw)),
                                "selected_found": len(expected.intersection(chosen)),
                                "raw_complete": expected.issubset(raw),
                                "selected_complete": expected.issubset(chosen),
                                "selected_ids": chosen,
                                "scope": [s.model_dump() for s in packet.scopes],
                                "retrieval": packet.retrieval,
                            }
                        )
                    except ValueError as exc:
                        results.append(
                            {
                                "id": row["id"],
                                "mode": mode,
                                "k": k,
                                "error": str(exc),
                                "expected": sorted(expected),
                                "raw_complete": False,
                                "selected_complete": False,
                                "selected_ids": [],
                            }
                        )
        save(
            out / "ranking.json",
            {
                "suite_sha256": sha256(suite_raw).hexdigest(),
                "index_id": bundle.id,
                "classification": "Retrieval only. Empty expected sets and conversational fragments excluded from summary.",
                "rows": results,
            },
        )
        summary = {}
        sequence_ids = {c["id"] for c in cases if c.get("turns")}
        for mode in ["lexical", "semantic", "hybrid"]:
            for k in [1, 3, 5, 8, 12]:
                cell = [
                    r
                    for r in results
                    if r["mode"] == mode
                    and r["k"] == k
                    and r.get("expected")
                    and r["id"].split(".T")[0] not in sequence_ids
                ]
                summary[f"{mode}@{k}"] = {
                    "n": len(cell),
                    "raw_complete": sum(r["raw_complete"] for r in cell),
                    "selected_complete": sum(r["selected_complete"] for r in cell),
                    "mean_packet": round(sum(len(r["selected_ids"]) for r in cell) / len(cell), 2)
                    if cell
                    else None,
                    "missing": [r["id"] for r in cell if not r["selected_complete"]],
                }
        save(out / "ranking-summary.json", summary)
        print(json.dumps(summary, indent=2))
        return
    await capacity(out)
    config = {
        "suite": args.suite,
        "suite_sha256": sha256(suite_raw).hexdigest(),
        "index_id": bundle.id,
        "index_checksum": bundle.checksum,
        "mode": args.mode,
        "k": args.k,
        "trials": args.trials,
        "ids": args.ids,
        "model": settings.openrouter_model,
        "answer_prompt": ANSWER_PROMPT_ID,
        "checker_prompt": CHECKER_PROMPT_ID,
        "code": {
            str(p.relative_to(PROMPTS.parent)): sha256(p.read_bytes()).hexdigest()
            for p in sorted(PROMPTS.parent.rglob("*"))
            if p.suffix in {".py", ".txt"}
        },
        "path": "Application intent/retrieval/answer services with real providers; separate HTTP tests verify persistence/wiring",
        "latency_method": "Service latency; cached exact-question vectors omit embedding network latency. HTTP checks measure full path.",
        "legacy_context": args.legacy_context,
    }
    config["runner_sha256"] = sha256(Path(__file__).read_bytes()).hexdigest()
    recorder = Recorder(out, config)
    (out / "runner.py").write_bytes(Path(__file__).read_bytes())
    sem = asyncio.Semaphore(args.concurrency)
    failures = 0
    stopped = False

    async def sequence(case, trial):
        nonlocal failures, stopped
        async with sem:
            if stopped:
                return
            history = []
            previous_answer = previous_packet = None
            for turn_index, turn in enumerate(case.get("turns") or [case], 1):
                job = {
                    "case_id": case["id"],
                    "turn": turn_index,
                    "trial": trial,
                    "model": settings.openrouter_model,
                }
                identity = recorder.id_for(job)
                if identity in recorder.seen:
                    raise ValueError(
                        "Do not resume a sequence without restoring captured context; use new output"
                    )
                if stopped:
                    return
                recorder.seen.add(identity)
                recorder.append({"attempt_id": identity, "status": "dispatched", "job": job})
                traced = EvaluationClient(
                    OpenRouterClient(settings.openrouter_api_key, settings.openrouter_model)
                )
                started = time.perf_counter()
                packet = None
                try:
                    configured_scope = turn.get("scope", case.get("scope", {}))
                    patient = configured_scope.get("patient_id")
                    as_of = configured_scope.get("as_of")
                    if history:
                        ids = patient_ids(history[-1])
                        candidate = (
                            ids[0]
                            if len(ids) == 1
                            else (
                                discovered_patient(previous_answer, previous_packet)
                                if previous_answer
                                else None
                            )
                        )
                        follows = not self_contained(turn["question"])
                        if not args.legacy_context:
                            from emer.services.text_intent import general_lookup, record_topic_followup

                            source = next(
                                (
                                    d
                                    for d in bundle.documents
                                    if d.doc_id == candidate and d.category == "synthetic_patient"
                                ),
                                None,
                            )
                            follows = (follows and not general_lookup(turn["question"])) or (
                                source is not None and record_topic_followup(turn["question"], source.text)
                            )
                        if candidate and (args.legacy_context or follows):
                            patient = candidate
                    intent = await resolve_text_intent(traced, turn["question"], history[-6:], patient, as_of)
                    if intent.clarification:
                        answer = PublishedAnswer(
                            status="clarification",
                            statements=[],
                            gaps=[AnswerGap(part_id="intent", text=intent.clarification)],
                            next_steps=[],
                            scopes=[],
                            index_id=bundle.id,
                            index_checksum=bundle.checksum,
                            usage=[],
                            diagnostics={"intent": "clarification_required"},
                        )
                    else:
                        options = {}
                        if args.mode in ["hybrid", "semantic"]:
                            key = f"{case['id']}.T{turn_index}"
                            if queries.get("questions", {}).get(key) == intent.question:
                                vector = queries["vectors"][key]
                            else:
                                embedded = await traced.client.embed([intent.question], model)
                                vector = embedded.vectors[0]
                                traced.responses.append(
                                    {
                                        "operation": "embedding",
                                        "usage": embedded.usage.model_dump(),
                                        "query": intent.question,
                                    }
                                )
                            options = {"query_vector": vector, "query_model": model}
                        packet = retriever.evidence(
                            intent.question,
                            patient_id=patient,
                            as_of=as_of,
                            mode=args.mode,
                            top_k=args.k,
                            **options,
                        )
                        answer = await AnsweringService(traced).answer(packet, intent.question)
                        history.append(intent.question)
                        previous_answer, previous_packet = answer, packet
                    recorder.append(
                        {
                            "attempt_id": identity,
                            "status": "completed",
                            "job": job,
                            "question": turn["question"],
                            "resolved_question": intent.question,
                            "answer": answer.model_dump(),
                            "evidence": packet.model_dump() if packet else None,
                            "provider_responses": traced.responses,
                            "usage": [r["usage"] for r in traced.responses],
                            "latency_ms": round((time.perf_counter() - started) * 1000, 3),
                            "citation_validation": citations(answer, bundle),
                        }
                    )
                    print(
                        json.dumps(
                            {**job, "status": answer.status, "citations": citations(answer, bundle)["count"]}
                        ),
                        flush=True,
                    )
                except ProviderError as exc:
                    failures += 1
                    if failures >= 3 or exc.code in [
                        "provider_capacity_exhausted",
                        "provider_authentication",
                        "provider_unconfigured",
                    ]:
                        stopped = True
                    usage = [r["usage"] for r in traced.responses]
                    if exc.usage and exc.usage.model_dump() not in usage:
                        usage.append(exc.usage.model_dump())
                    recorder.append(
                        {
                            "attempt_id": identity,
                            "status": "failed",
                            "job": job,
                            "question": turn["question"],
                            "error": {"code": exc.code, "message": str(exc)},
                            "usage": usage,
                            "provider_responses": traced.responses,
                            "unreceipted_failed_dispatch": exc.code.startswith("provider_")
                            and exc.code not in NO_RECEIPT_FAILURES
                            and exc.usage is None,
                            "provider_response_diagnostics": exc.response_diagnostics,
                            "evidence": packet.model_dump() if packet else None,
                        }
                    )
                    print(json.dumps({**job, "error": exc.code}), flush=True)
                    break

    await asyncio.gather(*(sequence(c, t) for c in cases for t in range(1, args.trials + 1)))
    recorder.handle.close()
    latest = {
        r["attempt_id"]: r for r in [json.loads(x) for x in (out / "attempts.jsonl").read_text().splitlines()]
    }
    usage = [u for r in latest.values() for u in r.get("usage", [])]
    values = sorted(r["latency_ms"] for r in latest.values() if "latency_ms" in r)
    save(
        out / "technical-summary.json",
        {
            "attempted": len(latest),
            "completed": sum(r["status"] == "completed" for r in latest.values()),
            "failed": sum(r["status"] == "failed" for r in latest.values()),
            "stopped": stopped,
            "calls": len(usage),
            "reported_cost": sum(u["cost"] for u in usage if u.get("cost") is not None),
            "unknown_cost_calls": sum(u.get("cost") is None for u in usage),
            "unreceipted_failed_dispatches": sum(
                r.get("unreceipted_failed_dispatch", False) for r in latest.values()
            ),
            "p50_ms": values[math.ceil(len(values) * 0.5) - 1] if values else None,
            "p95_ms": values[math.ceil(len(values) * 0.95) - 1] if values else None,
            "factual_review": "pending; no semantic quality inferred from provider checker",
        },
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["embed", "rank", "run"])
    parser.add_argument("--suite", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--bundle")
    parser.add_argument("--queries")
    parser.add_argument("--ids")
    parser.add_argument("--mode", default="full", choices=["full", "lexical", "semantic", "hybrid"])
    parser.add_argument("--k", type=int, default=8)
    parser.add_argument("--trials", type=int, default=1)
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument(
        "--legacy-context",
        action="store_true",
        help="Match frozen baseline run admission for baseline evidence",
    )
    asyncio.run(main(parser.parse_args()))
