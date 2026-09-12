"""Run frozen source mutations through real lexical retrieval and the selected answer service.

Offline preparation: uv run python scripts/intelligence_mutations.py --suite
evals/intelligence-source-mutations-20260912.json --out .local/mutation-preview --validate-only
Omit --validate-only for the bounded real-provider run. Nothing is activated or written to Postgres.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import json
import os
import sqlite3
import time
from hashlib import sha256
from pathlib import Path

from emer.contracts.answer import SourceDocument
from emer.evaluation.__main__ import EvaluationClient, Recorder, summarize
from emer.providers.openrouter import ROUTE_CONFIG, OpenRouterClient, ProviderError
from emer.services.answering import ANSWER_PROMPT_ID, CHECKER_PROMPT_ID, PROMPTS, AnsweringService
from emer.services.ingestion import Bundle, canonical
from emer.services.provider_activity import NO_RECEIPT_FAILURES
from emer.services.retrieval import RetrievalService
from emer.settings import settings
from intelligence_eval import capacity, citations, save, stamp

ROOT = Path(__file__).resolve().parents[1]
FREEZE = ROOT / "evals/intelligence-source-mutations-20260912.freeze.json"
SELECTED_MODEL = "openai/gpt-5.6-luna"


def construct_fixture(base: Bundle, case: dict, suite_sha: str) -> Bundle:
    """Apply exact frozen replacements; rebuild a private SQLite bundle entirely in memory."""
    docs = {doc.doc_id: doc.model_dump() for doc in base.documents}
    config = copy.deepcopy(base.config)
    for mutation in case["mutations"]:
        doc = docs.pop(mutation["doc_id"])
        for change in mutation["replacements"]:
            field, old, new = change["field"], change["old"], change["new"]
            if doc[field].count(old) != 1:
                raise ValueError(f"{case['id']}: replacement is not exact and unique: {field}")
            doc[field] = doc[field].replace(old, new)
        doc["sha256"] = sha256(doc["text"].encode()).hexdigest()
        if doc["doc_id"] in docs:
            raise ValueError("Mutation would duplicate a source identifier")
        docs[doc["doc_id"]] = doc
    for mutation in case["policy_annotation_replacements"]:
        policy = next(p for p in config["policies"] if p["doc_id"] == mutation["doc_id"])
        for change in mutation["replacements"]:
            if policy[change["field"]] != change["old"]:
                raise ValueError("Policy annotation differs from the frozen replacement")
            policy[change["field"]] = change["new"]
    for policy in config["policies"]:
        source = docs[policy["doc_id"]]
        if (
            policy["valid_from"] != source["effective_date"]
            or policy["provenance_quote"] not in source["text"]
            or (policy.get("supersession_quote") and policy["supersession_quote"] not in source["text"])
            or (policy["valid_through"] and policy["valid_through"] < policy["valid_from"])
            or any(
                p not in docs or p not in policy.get("supersession_quote", "") for p in policy["supersedes"]
            )
        ):
            raise ValueError("Mutated policy annotations are not source-backed")
    for evidence in case["expected_evidence"]:
        if evidence["quote"] not in docs[evidence["doc_id"]]["text"]:
            raise ValueError("Frozen expected quotation is absent from its mutated source")
    records = sorted(docs.values(), key=lambda doc: doc["doc_id"])
    raw = b"".join(canonical({k: v for k, v in doc.items() if k != "sha256"}) + b"\n" for doc in records)
    corpus_sha = sha256(raw).hexdigest()
    if len(records) != len(base.documents) or corpus_sha == base.corpus_checksum:
        raise ValueError("A mutation must preserve record count and change corpus identity")
    config.update(embedding=None, evaluation_fixture={"id": case["id"], "suite_sha256": suite_sha})
    identity = sha256(canonical({"corpus_checksum": corpus_sha, "config": config})).hexdigest()[:24]
    db = sqlite3.connect(":memory:")
    try:
        db.deserialize(base.data)
        db.execute("DELETE FROM documents")
        db.execute("DELETE FROM search")
        db.execute("DROP TABLE IF EXISTS embeddings")
        for record in records:
            doc = SourceDocument.model_validate(record)
            db.execute("INSERT INTO documents VALUES (?, ?)", (doc.doc_id, doc.model_dump_json()))
            db.execute("INSERT INTO search VALUES (?, ?, ?)", (doc.doc_id, doc.title, doc.text))
        for key, value in (
            ("id", identity),
            ("corpus_checksum", corpus_sha),
            ("config", canonical(config).decode()),
        ):
            db.execute("UPDATE metadata SET value=? WHERE key=?", (value, key))
        db.execute("INSERT INTO search(search) VALUES ('optimize')")
        db.commit()
        db.execute("VACUUM")
        return Bundle.from_bytes(db.serialize())
    finally:
        db.close()


def journal(path: Path, row: dict) -> None:
    with path.open("a") as handle:
        handle.write(json.dumps({"at": stamp(), **row}, ensure_ascii=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


class MutationClient(EvaluationClient):
    """Durably record each dispatch/receipt without changing the application payload."""

    def __init__(self, client, path: Path, attempt_id: str):
        super().__init__(client)
        self.path, self.attempt_id, self.operations = path, attempt_id, []

    async def structured(self, system, payload, schema, operation="generation", max_tokens=4500):
        limits = {"generation": 1, "repair": 1, "support_check": 2}
        if operation not in limits or self.operations.count(operation) >= limits[operation]:
            raise ValueError("Unexpected answer-service call beyond generation/check and one repair")
        self.operations.append(operation)
        call = {"attempt_id": self.attempt_id, "call": len(self.operations), "operation": operation}
        journal(
            self.path,
            {
                **call,
                "status": "dispatched",
                "prompt_sha256": sha256(system.encode()).hexdigest(),
                "input_sha256": sha256(canonical(payload)).hexdigest(),
                "payload": payload,
            },
        )
        try:
            result = await super().structured(system, payload, schema, operation, max_tokens)
        except ProviderError as exc:
            journal(
                self.path,
                {
                    **call,
                    "status": "failed",
                    "code": exc.code,
                    "usage": exc.usage.model_dump() if exc.usage else None,
                    "response_diagnostics": exc.response_diagnostics,
                },
            )
            raise
        journal(
            self.path,
            {
                **call,
                "status": "completed",
                **self.responses[-1],
                "rate_limit_retries": result.rate_limit_retries,
            },
        )
        return result


async def main(args) -> None:
    if not 1 <= args.trials <= 3:
        raise ValueError("Use one to three explicitly requested trials")
    suite_path = Path(args.suite).resolve()
    raw = suite_path.read_bytes()
    suite_sha, suite = sha256(raw).hexdigest(), json.loads(raw)
    frozen = json.loads(Path(args.freeze).read_text())
    if suite_sha != frozen["sha256"] or len(suite["cases"]) != frozen["cases"]:
        raise ValueError("Mutation suite differs from its independently frozen case file")
    selected = set(args.ids.split(",")) if args.ids else {case["id"] for case in suite["cases"]}
    cases = [case for case in suite["cases"] if case["id"] in selected]
    if {case["id"] for case in cases} != selected:
        raise ValueError("Unknown selected mutation IDs")
    base_path = Path(args.base_bundle) if args.base_bundle else ROOT / suite["base_bundle"]["path"]
    base = Bundle.from_bytes(base_path.read_bytes(), suite["base_bundle"]["sha256"])
    if (
        base.id != frozen["base_bundle_index_id"]
        or base.corpus_checksum != suite["base_bundle"]["corpus_sha256"]
    ):
        raise ValueError("Mutation base differs from the frozen corpus/index")
    fixtures = {case["id"]: construct_fixture(base, case, suite_sha) for case in cases}
    packets = {
        case["id"]: RetrievalService(fixtures[case["id"]]).evidence(case["question"], mode="lexical", top_k=8)
        for case in cases
    }
    out = Path(args.out).resolve()
    config = {
        "command": "source-mutation",
        "suite": str(suite_path),
        "suite_sha256": suite_sha,
        "model": SELECTED_MODEL,
        "route_config": ROUTE_CONFIG,
        "trials": args.trials,
        "ids": sorted(selected),
        "mode": "lexical",
        "top_k": 8,
        "validate_only": args.validate_only,
        "base_index_id": base.id,
        "base_index_checksum": base.checksum,
        "fixtures": {
            key: {
                "index_id": bundle.id,
                "checksum": bundle.checksum,
                "corpus_checksum": bundle.corpus_checksum,
            }
            for key, bundle in fixtures.items()
        },
        "answer_prompt": ANSWER_PROMPT_ID,
        "checker_prompt": CHECKER_PROMPT_ID,
        "source_sha256": {
            str(p.relative_to(PROMPTS.parent)): sha256(p.read_bytes()).hexdigest()
            for p in sorted(PROMPTS.parent.rglob("*"))
            if p.suffix in {".py", ".txt"}
        },
        "runner_sha256": sha256(Path(__file__).read_bytes()).hexdigest(),
        "helper_sha256": sha256((ROOT / "scripts/intelligence_eval.py").read_bytes()).hexdigest(),
        "bounds": "Sequential trials; two ordinary calls, at most one repair/check pair; three cumulative failures stop the run. Existing provider rejects-only 429 backoff retained. No uncertain attempt is automatically repeated.",
        "scope": "Direct actual retrieval/AnsweringService; no intent, HTTP, persistence or live-index activation. Expected rubric stays outside provider inputs.",
    }
    recorder = Recorder(out, config)
    try:
        save(out / "fixture-evidence.json", {key: packet.model_dump() for key, packet in packets.items()})
        if args.validate_only:
            save(
                out / "validation.json",
                {
                    "suite_sha256": suite_sha,
                    "cases": len(cases),
                    "provider_calls": 0,
                    "fixtures": config["fixtures"],
                    "expected_source_retrieval": {
                        case["id"]: set(case["required_sources"]).issubset(
                            {source.doc_id for source in packets[case["id"]].sources}
                        )
                        for case in cases
                    },
                },
            )
            print(json.dumps({"validated": len(cases), "provider_calls": 0, "out": str(out)}))
            return
        if not settings.openrouter_api_key or settings.openrouter_model != SELECTED_MODEL:
            raise ValueError("Configure the existing selected Luna route before dispatch")
        previous = [json.loads(line) for line in recorder.path.read_text().splitlines()]
        latest = {row["attempt_id"]: row for row in previous}
        failures = sum(row["status"] == "failed" for row in latest.values())
        jobs = [
            {"case_id": case["id"], "trial": trial, "model": SELECTED_MODEL}
            for case in cases
            for trial in range(1, args.trials + 1)
        ]
        pending = [job for job in jobs if recorder.id_for(job) not in recorder.seen]
        if pending and failures < 3:
            capacity_out = out
            if (out / "credit-before.json").exists():
                capacity_out = out / ("capacity-" + stamp().replace(":", "-"))
                capacity_out.mkdir()
            await capacity(capacity_out)
        by_id = {case["id"]: case for case in cases}
        for job in pending:
            if failures >= 3:
                break
            identity = recorder.id_for(job)
            case, bundle, packet = by_id[job["case_id"]], fixtures[job["case_id"]], packets[job["case_id"]]
            recorder.seen.add(identity)
            recorder.append({"attempt_id": identity, "status": "dispatched", "job": job})
            traced = MutationClient(
                OpenRouterClient(settings.openrouter_api_key, SELECTED_MODEL),
                out / "provider-calls.jsonl",
                identity,
            )
            started = time.perf_counter()
            record = {
                "attempt_id": identity,
                "job": job,
                "question": case["question"],
                "evidence": packet.model_dump(),
                "fixture": config["fixtures"][case["id"]],
            }
            try:
                answer = await AnsweringService(traced).answer(packet, case["question"])
                provenance = citations(answer, bundle)
                if answer.index_id != bundle.id or answer.index_checksum != bundle.checksum:
                    provenance["exact_provenance_errors"].append({"answer_index_identity": "mismatch"})
                provenance["status"] = "failed" if provenance["exact_provenance_errors"] else "passed"
                record.update(status="completed", answer=answer.model_dump(), citation_validation=provenance)
                if provenance["status"] == "failed":
                    failures += 1
                    record.update(status="failed", error={"code": "citation_provenance_failure"})
            except (ProviderError, ValueError, TypeError, KeyError, IndexError) as exc:
                failures += 1
                record.update(
                    status="failed",
                    error={
                        "code": getattr(exc, "code", "local_contract_failure"),
                        "message": str(exc),
                        "type": type(exc).__name__,
                    },
                )
                if isinstance(exc, ProviderError):
                    record["provider_response_diagnostics"] = exc.response_diagnostics
                    if exc.usage:
                        record["failed_call_usage"] = exc.usage.model_dump()
                    record["unreceipted_failed_dispatch"] = (
                        exc.code.startswith("provider_")
                        and exc.code not in NO_RECEIPT_FAILURES
                        and exc.usage is None
                    )
                    if exc.code in {
                        "provider_capacity_exhausted",
                        "provider_authentication",
                        "provider_forbidden",
                        "provider_model_unavailable",
                        "provider_unconfigured",
                    }:
                        failures = 3
            record.update(
                provider_responses=traced.responses,
                usage=[r["usage"] for r in traced.responses],
                latency_ms=round((time.perf_counter() - started) * 1000, 3),
            )
            if record.get("failed_call_usage"):
                record["usage"].append(record["failed_call_usage"])
            recorder.append(record)
            print(
                json.dumps(
                    {
                        **job,
                        "status": record["status"],
                        "answer_status": record.get("answer", {}).get("status"),
                    }
                ),
                flush=True,
            )
        save(
            out / "technical-summary.json",
            summarize(recorder.path) if recorder.path.stat().st_size else {"dispatched": 0},
        )
    finally:
        recorder.handle.close()
        if sha256(base_path.read_bytes()).hexdigest() != base.checksum:
            raise RuntimeError("Original base bundle changed during evaluation")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", required=True)
    parser.add_argument("--base-bundle", help="Relocated recorded bundle; the frozen SHA-256 is still required")
    parser.add_argument("--freeze", default=str(FREEZE), help="Independent pre-dispatch expectation freeze")
    parser.add_argument("--out", required=True)
    parser.add_argument("--ids", help="Comma-separated frozen mutation IDs")
    parser.add_argument("--trials", type=int, default=1)
    parser.add_argument(
        "--validate-only", action="store_true", help="Build/retrieve fixtures without provider calls"
    )
    asyncio.run(main(parser.parse_args()))
