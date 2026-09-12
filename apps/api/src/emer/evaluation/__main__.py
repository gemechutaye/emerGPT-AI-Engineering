"""Run via python -m emer.evaluation; each command records actual attempts before dispatch."""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import json
import math
import os
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any

import httpx

from emer.contracts.answer import EvidencePacket, ProviderDraft, SupportCheck
from emer.evaluation.mutations import mutate_packet
from emer.providers.openrouter import ROUTE_CONFIG, OpenRouterClient, ProviderError
from emer.services.answering import (
    ANSWER_PROMPT_ID,
    CHECKER_PROMPT_ID,
    PROMPTS,
    AnsweringService,
    support_payload,
    validate_draft,
)
from emer.services.ingestion import Bundle, canonical, ingest, with_embeddings
from emer.services.retrieval import RetrievalService
from emer.settings import settings

GENERATION_MODELS = [
    "openai/gpt-5.6-luna"
]  # User-selected current path; historical comparisons remain intact.
EMBEDDING_MODELS = ["openai/text-embedding-3-small", "openai/text-embedding-3-large"]


def stamp() -> str:
    return datetime.now(UTC).isoformat()


def summarize(path: Path) -> dict[str, Any]:
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    terminal = {row["attempt_id"]: row for row in rows if row["status"] != "dispatched"}
    dispatched = {row["attempt_id"] for row in rows if row["status"] == "dispatched"}
    groups = {}
    for model in sorted({row["job"]["model"] for row in rows}):
        outcomes = [row for row in terminal.values() if row["job"]["model"] == model]
        usage = [item for row in outcomes for item in row.get("usage", [])]
        latencies = sorted(item["latency_ms"] for item in usage)

        def percentile(fraction, values=latencies):
            return values[max(0, math.ceil(len(values) * fraction) - 1)] if values else None

        groups[model] = {
            "completed": sum(row["status"] == "completed" for row in outcomes),
            "failed": sum(row["status"] == "failed" for row in outcomes),
            "citation_contract_passes": sum(
                row.get("citation_validation", {}).get("status") == "passed" for row in outcomes
            ),
            "citation_contract_failures": sum(
                row.get("citation_validation", {}).get("status") == "failed" for row in outcomes
            ),
            "reported_provider_calls": len(usage),
            "cost_reported_calls": sum(item.get("cost") is not None for item in usage),
            "reported_cost_sum": sum(item["cost"] for item in usage if item.get("cost") is not None),
            "tokens_reported_calls": sum(item.get("total_tokens") is not None for item in usage),
            "reported_total_tokens_sum": sum(
                item["total_tokens"] for item in usage if item.get("total_tokens") is not None
            ),
            "provider_latency_samples": len(latencies),
            "provider_latency_p50_ms": percentile(0.5),
            "provider_latency_p95_ms": percentile(0.95),
        }
    return {
        "source": str(path),
        "captured_at": stamp(),
        "dispatched": len(dispatched),
        "terminal": len(terminal),
        "unresolved_attempt_ids": sorted(dispatched - set(terminal)),
        "models": groups,
        "factual_quality_assessment": "pending; contract success and model checker verdicts are not factual ground truth",
    }


def load_suite(name: str) -> tuple[dict[str, Any], str]:
    raw = Path(f"evals/{name}.json").read_bytes()
    digest = sha256(raw).hexdigest()
    frozen = json.loads(Path("evals/frozen-manifest.json").read_text())
    if digest != frozen["files"][name]["sha256"]:
        raise ValueError("Evaluation suite differs from frozen manifest; declare a new suite version first")
    return json.loads(raw), digest


class Recorder:
    def __init__(self, directory: Path, config: dict[str, Any]):
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / "attempts.jsonl"
        self.handle = self.path.open("a+", buffering=1)
        try:
            fcntl.flock(self.handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self.handle.close()
            raise ValueError("Another evaluator owns this output directory") from exc
        self.seen = set()
        self.handle.seek(0)
        for line in self.handle:
            self.seen.add(json.loads(line)["attempt_id"])
        self.handle.seek(0, os.SEEK_END)
        manifest = directory / "run.json"
        identity = sha256(canonical(config)).hexdigest()
        if manifest.exists() and json.loads(manifest.read_text())["config_sha256"] != identity:
            self.handle.close()
            raise ValueError("Output directory belongs to another experiment configuration")
        if not manifest.exists():
            manifest.write_text(
                json.dumps(
                    {
                        "created_at": stamp(),
                        "config_sha256": identity,
                        "config": config,
                        "reviewer_type": "automated contract/retrieval checks only; factual review pending",
                        "quality_claim": None,
                    },
                    indent=2,
                )
            )
        self.config_id = identity

    def append(self, row: dict[str, Any]) -> None:
        self.handle.write(json.dumps({"at": stamp(), **row}, ensure_ascii=False) + "\n")
        self.handle.flush()
        os.fsync(self.handle.fileno())

    def id_for(self, job: dict[str, Any]) -> str:
        return sha256(canonical({"config": self.config_id, "job": job})).hexdigest()[:24]


def retain_prior_attempts(
    recorder: Recorder, directory: Path, config: dict[str, Any], retry_capacity: bool
) -> None:
    prior = json.loads((directory / "run.json").read_text())
    invariants = [
        "command",
        "suite",
        "suite_sha256",
        "corpus_sha256",
        "index_id",
        "index_checksum",
        "models",
        "endpoints",
        "route_config",
        "case_count",
        "trials",
        "prompt_sha256",
        "checker_prompt_sha256",
        "mutation_fixture_sha256",
        "input",
        "input_sha256",
        "focus_sha256",
    ]
    if any(prior["config"].get(key) != config.get(key) for key in invariants):
        raise ValueError("Resume source differs in experiment inputs; do not combine these comparisons")
    critical_sources = [
        "contracts/answer.py",
        "domain/scope.py",
        "domain/policy.py",
        "services/ingestion.py",
        "services/retrieval.py",
        "services/answering.py",
        "providers/openrouter.py",
    ]
    if any(
        prior["config"].get("source_file_sha256", {}).get(path)
        != config.get("source_file_sha256", {}).get(path)
        for path in critical_sources
    ):
        raise ValueError(
            "Answer/retrieval implementation changed; review affected evidence and run a new comparison"
        )
    rows = [json.loads(line) for line in (directory / "attempts.jsonl").read_text().splitlines()]
    latest = {row["attempt_id"]: row for row in rows}
    for row in latest.values():
        if retry_capacity and row.get("error", {}).get("code") == "provider_capacity_exhausted":
            continue
        identity = recorder.id_for(row["job"])
        if identity not in recorder.seen:
            recorder.append(
                {
                    **row,
                    "attempt_id": identity,
                    "retained_from": {
                        "directory": str(directory),
                        "attempt_id": row["attempt_id"],
                        "config_sha256": prior["config_sha256"],
                    },
                }
            )
            recorder.seen.add(identity)


async def run_jobs(jobs: list[dict[str, Any]], recorder: Recorder, operation, concurrency: int) -> None:
    semaphore = asyncio.Semaphore(concurrency)
    unavailable: set[str] = set()
    failures: dict[str, int] = {}
    capacity_exhausted = False

    async def one(job):
        nonlocal capacity_exhausted
        identity = recorder.id_for(job)
        if identity in recorder.seen:
            return  # Never automatically rebill an uncertain or completed attempt.
        async with semaphore:
            if capacity_exhausted or job["model"] in unavailable:
                return
            recorder.seen.add(identity)
            recorder.append({"attempt_id": identity, "status": "dispatched", "job": job})
            try:
                result = await operation(job)
                recorder.append({"attempt_id": identity, "status": "completed", "job": job, **result})
                print(
                    json.dumps(
                        {
                            "attempt_id": identity,
                            "status": "completed",
                            "model": job["model"],
                            "case_id": job.get("case_id"),
                        }
                    ),
                    flush=True,
                )
            except ProviderError as exc:
                failures[job["model"]] = failures.get(job["model"], 0) + 1
                if failures[job["model"]] >= 3:
                    unavailable.add(job["model"])
                usage = exc.attempt_usage or ([exc.usage.model_dump()] if exc.usage else [])
                recorder.append(
                    {
                        "attempt_id": identity,
                        "status": "failed",
                        "job": job,
                        "error": {"code": exc.code, "message": str(exc), "retryable": exc.retryable},
                        "provider_response_diagnostics": exc.response_diagnostics,
                        "usage": usage,
                        "provider_responses": getattr(exc, "evaluation_trace", []),
                        "evidence": getattr(exc, "evaluation_evidence", None),
                    }
                )
                if exc.code == "provider_capacity_exhausted":
                    capacity_exhausted = True
                if exc.code in {
                    "provider_authentication",
                    "provider_model_unavailable",
                    "provider_forbidden",
                    "model_unconfigured",
                    "provider_unconfigured",
                }:
                    unavailable.add(job["model"])
                print(
                    json.dumps(
                        {"attempt_id": identity, "status": "failed", "model": job["model"], "code": exc.code}
                    ),
                    flush=True,
                )
            except (ValueError, TypeError, KeyError, IndexError, OSError) as exc:
                recorder.append(
                    {
                        "attempt_id": identity,
                        "status": "failed",
                        "job": job,
                        "error": {
                            "code": "local_contract_failure",
                            "type": type(exc).__name__,
                            "message": str(exc),
                        },
                    }
                )
                print(
                    json.dumps({"attempt_id": identity, "status": "failed", "type": type(exc).__name__}),
                    flush=True,
                )

    await asyncio.gather(*(one(job) for job in jobs))
    recorder.handle.close()


def provider(model: str, endpoints: dict[str, list[str]]) -> OpenRouterClient:
    return OpenRouterClient(settings.openrouter_api_key, model, endpoints.get(model))


class EvaluationClient:
    """Preserve actual parsed provider replies before publication or validation failure."""

    def __init__(self, client: OpenRouterClient):
        self.client = client
        self.responses = []

    async def structured(self, system, payload, schema, operation="generation", max_tokens=4500):
        response = await self.client.structured(
            system, payload, schema, operation=operation, max_tokens=max_tokens
        )
        self.responses.append(
            {
                "operation": operation,
                "prompt_sha256": sha256(system.encode()).hexdigest(),
                "input_sha256": sha256(canonical(payload)).hexdigest(),
                "schema": schema.__name__,
                "parsed_response": response.value.model_dump(mode="json"),
                "usage": response.usage.model_dump(mode="json"),
            }
        )
        return response


def retrieval_coverage(packet: EvidencePacket, expected: list[str]) -> dict[str, Any]:
    required = set(expected)
    raw = set(packet.retrieval["raw_top_k_ids"])
    selected = {s.doc_id for s in packet.sources}
    return {
        "required_source_count": len(required),
        "raw_required_found": len(raw & required),
        "expanded_required_found": len(selected & required),
        "raw_missing": sorted(required - raw),
        "expanded_missing": sorted(required - selected),
        "meaning": "Source-ID retrieval coverage only; not factual answer quality",
    }


async def main(args) -> None:
    if args.command == "summarize":
        if not args.input:
            raise ValueError("Summary requires --input attempts.jsonl")
        print(json.dumps(summarize(Path(args.input)), indent=2))
        return
    suite, suite_hash = load_suite(args.suite)
    bundle = ingest(args.manifest)
    cases = suite["cases"]
    if args.limit:
        if args.command == "final":
            raise ValueError("Final trials cannot reduce the frozen 20-scenario suite")
        cases = cases[: args.limit]
    if args.command == "inventory":
        all_sources = {s for case in suite["cases"] for s in case.get("required_sources", [])}
        print(
            json.dumps(
                {
                    "suite": args.suite,
                    "cases": len(suite["cases"]),
                    "suite_sha256": suite_hash,
                    "source_coverage": len(all_sources),
                    "corpus_count": len(bundle.documents),
                    "index_id": bundle.id,
                    "index_checksum": bundle.checksum,
                    "missing_sources": sorted({d.doc_id for d in bundle.documents} - all_sources),
                },
                indent=2,
            )
        )
        return
    if not settings.openrouter_api_key:
        raise ValueError("OpenRouter is unconfigured; no live evaluation was dispatched")
    if args.retry_capacity_failures and not args.resume_from:
        raise ValueError("Retrying a rejected capacity attempt requires --resume-from")
    output = Path(args.out)
    endpoints = json.loads(Path(args.endpoints).read_text()) if args.endpoints else {}
    models = args.models or (
        EMBEDDING_MODELS
        if args.command == "embeddings"
        else GENERATION_MODELS
        if args.command == "generations"
        else [settings.openrouter_model]
    )
    if args.command == "final" and (
        args.suite != "holdout-v2" or models != GENERATION_MODELS or args.trials != 3
    ):
        raise ValueError(
            "Final acceptance requires the replacement --suite holdout-v2, the user-selected Luna model, and --trials 3"
        )
    if args.focus and args.command != "checkers":
        raise ValueError("A saved-draft focus applies only to the checker development batch")
    prompt = (PROMPTS / f"{ANSWER_PROMPT_ID}.txt").read_text()
    checker_prompt = (PROMPTS / f"{CHECKER_PROMPT_ID}.txt").read_text()
    mutation_suite, mutation_hash = load_suite("mutations") if args.command == "mutations" else ({}, None)
    config = {
        "command": args.command,
        "suite": args.suite,
        "suite_sha256": suite_hash,
        "corpus_sha256": bundle.corpus_checksum,
        "index_id": bundle.id,
        "index_checksum": bundle.checksum,
        "models": models,
        "route_config": ROUTE_CONFIG,
        "source_file_sha256": {
            str(path.relative_to(Path(__file__).parents[1])): sha256(path.read_bytes()).hexdigest()
            for path in sorted(Path(__file__).parents[1].rglob("*.py"))
            if "__pycache__" not in path.parts
        },
        "endpoints": endpoints,
        "case_count": len(cases),
        "trials": args.trials,
        "prompt_sha256": sha256(prompt.encode()).hexdigest(),
        "prompt_id": ANSWER_PROMPT_ID,
        "checker_prompt_id": CHECKER_PROMPT_ID,
        "checker_prompt_sha256": sha256(checker_prompt.encode()).hexdigest(),
        "mutation_fixture_sha256": mutation_hash,
        "input": args.input,
        "input_sha256": sha256(Path(args.input).read_bytes()).hexdigest()
        if args.input and Path(args.input).is_file()
        else None,
        "focus_sha256": sha256(Path(args.focus).read_bytes()).hexdigest() if args.focus else None,
        "selection": "User selected Luna with full permitted context; historical alternatives are not final acceptance requirements",
        "resume_from": args.resume_from,
        "retry_capacity_failures": args.retry_capacity_failures,
        "fallbacks": False,
        "automatic_retries": 0,
        "concurrency": args.concurrency,
    }
    recorder = Recorder(output, config)
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.get(
            "https://openrouter.ai/api/v1/credits",
            headers={"Authorization": "Bearer " + settings.openrouter_api_key},
        )
        response.raise_for_status()
        balance = response.json()["data"]
    (output / "credit-before.json").write_text(json.dumps({"at": stamp(), **balance}, indent=2) + "\n")
    if balance["total_credits"] <= balance["total_usage"]:
        recorder.handle.close()
        raise ValueError("OpenRouter credit is not positive; no paid evaluation was dispatched")
    if args.resume_from:
        if Path(args.resume_from).resolve() == output.resolve():
            raise ValueError("A resume ledger must use a new output directory to preserve the original")
        retain_prior_attempts(recorder, Path(args.resume_from), config, args.retry_capacity_failures)
    by_id = {case["id"]: case for case in cases}
    retriever = RetrievalService(bundle)
    if args.command == "embeddings":
        jobs = [{"model": model, "kind": kind} for model in models for kind in ("corpus", "queries")]

        async def operation(job):
            texts = (
                [d.title + "\n" + d.text for d in bundle.documents]
                if job["kind"] == "corpus"
                else [c["question"] for c in cases]
            )
            result = await provider(job["model"], endpoints).embed(
                texts,
                job["model"],
                input_type=("search_document" if job["kind"] == "corpus" else "search_query")
                if job["model"].startswith("voyageai/")
                else None,
            )
            name = job["model"].replace("/", "-")
            if job["kind"] == "corpus":
                built = with_embeddings(
                    bundle,
                    result.model,
                    {d.doc_id: v for d, v in zip(bundle.documents, result.vectors, strict=True)},
                )
                (output / (name + ".sqlite")).write_bytes(built.data)
                extra = {
                    "index_id": built.id,
                    "index_checksum": built.checksum,
                    "bundle_file": name + ".sqlite",
                    "dimensions": built.config["embedding"]["dimensions"],
                }
            else:
                file = name + "-queries.json"
                (output / file).write_text(
                    json.dumps(
                        {
                            "model": result.model,
                            "suite_sha256": suite_hash,
                            "vectors": {c["id"]: v for c, v in zip(cases, result.vectors, strict=True)},
                        }
                    )
                )
                extra = {"query_file": file, "vectors_count": len(result.vectors)}
            return {"usage": [result.usage.model_dump()], **extra}
    elif args.command in {"generations", "final", "retrieval", "mutations", "regressions"}:
        spaces = {}
        if args.command == "retrieval":
            if not args.input:
                raise ValueError("Retrieval comparison needs --input pointing to embedding artifacts")
            for path in Path(args.input).glob("*.sqlite"):
                index = Bundle.from_bytes(path.read_bytes())
                query_file = path.with_name(path.stem + "-queries.json")
                queries = json.loads(query_file.read_text())
                if queries["suite_sha256"] != suite_hash:
                    raise ValueError("Query embeddings belong to another suite")
                spaces[index.config["embedding"]["model"]] = (RetrievalService(index), queries["vectors"])
            if len(spaces) != 2:
                raise ValueError("Retrieval comparison requires both embedding spaces")
        variants = (
            [("full", None)]
            if args.command != "retrieval"
            else [("full", None), ("lexical", None)]
            + [(mode, space) for space in sorted(spaces) for mode in ("semantic", "hybrid")]
        )
        jobs = [
            {
                "model": model,
                "case_id": case["id"],
                "trial": trial,
                "retrieval_mode": mode,
                "embedding_model": space,
            }
            for model in models
            for case in cases
            for mode, space in variants
            for trial in range(1, args.trials + 1)
        ]
        mutations = {item["id"]: item for item in mutation_suite.get("cases", [])}
        if args.command == "mutations":
            jobs = [
                {
                    "model": model,
                    "case_id": fixture["parent_case"],
                    "mutation_id": identifier,
                    "trial": trial,
                    "retrieval_mode": "full",
                    "embedding_model": None,
                }
                for model in models
                for identifier, fixture in mutations.items()
                for trial in range(1, args.trials + 1)
            ]

        async def operation(job):
            case = by_id[job["case_id"]]
            scope = case["scope"]
            kwargs = {}
            current = retriever
            if job["embedding_model"]:
                current, vectors = spaces[job["embedding_model"]]
                kwargs = {"query_vector": vectors[case["id"]], "query_model": job["embedding_model"]}
            packet = current.evidence(
                case["question"],
                patient_id=scope["patient_id"],
                as_of=scope["as_of"],
                mode=job["retrieval_mode"],
                **kwargs,
            )
            if args.command == "mutations":
                packet = mutate_packet(packet, mutations[job["mutation_id"]])
            client = provider(job["model"], endpoints)
            if args.command in {"final", "mutations", "regressions"}:
                traced = EvaluationClient(client)
                try:
                    answer = await AnsweringService(traced).answer(packet, case["question"])
                except ProviderError as exc:
                    exc.evaluation_trace = traced.responses
                    exc.evaluation_evidence = packet.model_dump()
                    raise
                return {
                    "answer": answer.model_dump(),
                    "usage": [u.model_dump() for u in answer.usage],
                    "evidence": packet.model_dump(),
                    "retrieval_coverage": retrieval_coverage(packet, case["required_sources"]),
                    "factual_review": None,
                    "provider_responses": traced.responses,
                }
            response = await client.structured(
                prompt, {"question": case["question"], "evidence": packet.model_dump()}, ProviderDraft
            )
            draft = ProviderDraft.model_validate(response.value)
            try:
                validate_draft(packet, draft)
                validation = {"status": "passed"}
            except ProviderError as exc:
                validation = {"status": "failed", "code": exc.code, "message": str(exc)}
            return {
                "draft": draft.model_dump(),
                "evidence": packet.model_dump(),
                "citation_validation": validation,
                "usage": [response.usage.model_dump()],
                "retrieval_coverage": retrieval_coverage(packet, case["required_sources"]),
                "factual_review": None,
            }
    elif args.command == "checkers":
        if not args.input:
            raise ValueError("Checker comparison needs saved attempts via --input")
        records = [json.loads(line) for line in Path(args.input).read_text().splitlines()]
        drafts = {r["attempt_id"]: r for r in records if r["status"] == "completed" and "draft" in r}
        if args.focus:
            focus = json.loads(Path(args.focus).read_text())
            wanted = {case["draft_sha256"] for case in focus["cases"]}
            drafts = {
                identifier: row
                for identifier, row in drafts.items()
                if sha256(canonical(row["draft"])).hexdigest() in wanted
            }
            if len(drafts) != len(wanted):
                raise ValueError("Saved-draft focus does not exactly match original input drafts")
        jobs = [{"model": model, "saved_attempt": identifier} for model in models for identifier in drafts]

        async def operation(job):
            saved = drafts[job["saved_attempt"]]
            result = await provider(job["model"], endpoints).structured(
                checker_prompt,
                support_payload(
                    EvidencePacket.model_validate(saved["evidence"]),
                    "\n".join(dict.fromkeys(scope["question"] for scope in saved["evidence"]["scopes"])),
                    ProviderDraft.model_validate(saved["draft"]),
                ),
                SupportCheck,
                operation="support_check",
                max_tokens=4000,
            )
            check = SupportCheck.model_validate(result.value)
            expected = {
                (kind, index)
                for kind, field in [("statement", "statements"), ("gap", "gaps"), ("next_step", "next_steps")]
                for index in range(len(saved["draft"][field]))
            }
            received = {(v.kind, v.index) for v in check.verdicts}
            return {
                "saved_attempt": job["saved_attempt"],
                "baseline_no_checker": saved["citation_validation"],
                "checker": check.model_dump(),
                "complete_verdict_coverage": received == expected and len(received) == len(check.verdicts),
                "usage": [result.usage.model_dump()],
                "factual_review": None,
                "warning": "Checker decisions are not independent factual ground truth",
            }
    else:
        raise ValueError("Unknown command")
    await run_jobs(jobs, recorder, operation, args.concurrency)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=[
            "inventory",
            "summarize",
            "embeddings",
            "generations",
            "retrieval",
            "checkers",
            "mutations",
            "final",
            "regressions",
        ],
    )
    parser.add_argument(
        "--suite",
        choices=[
            "development",
            "development-v2",
            "development-v3",
            "development-interval-v6",
            "holdout",
            "holdout-v2",
        ],
        default="development",
    )
    parser.add_argument("--manifest", default="config/corpus.json")
    parser.add_argument(
        "--out", default="artifacts/verification/evaluation-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    )
    parser.add_argument("--models", nargs="+")
    parser.add_argument("--endpoints", help="JSON mapping of verified model IDs to allowed endpoint slugs")
    parser.add_argument(
        "--input", help="Embedding artifact directory, or saved generation attempts for checker comparison"
    )
    parser.add_argument("--trials", type=int, default=1)
    parser.add_argument("--concurrency", type=int, choices=[1, 2], default=2)
    parser.add_argument("--limit", type=int)
    parser.add_argument(
        "--focus", help="Frozen saved-draft focus JSON for a bounded developmental checker batch"
    )
    parser.add_argument(
        "--resume-from",
        help="Retain earlier identical experiment outcomes without rebilling; use a new output directory",
    )
    parser.add_argument(
        "--retry-capacity-failures",
        action="store_true",
        help="Retry only prior HTTP402-rejected jobs after verifying positive account credit",
    )
    args = parser.parse_args()
    if not 1 <= args.trials <= 3:
        parser.error("Trials must be from1 to3")
    asyncio.run(main(args))
