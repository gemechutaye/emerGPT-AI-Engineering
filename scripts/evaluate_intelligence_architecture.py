"""Exercise the real intelligence service; frozen expectations never enter model inputs."""

import argparse
import asyncio
import json
import time
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

import httpx
from emer.contracts.answer import AnswerGap, PublishedAnswer
from emer.domain.dialogue_state import build_dialogue_state
from emer.providers.openrouter import OpenRouterClient, ProviderError
from emer.services.ingestion import Bundle
from emer.services.intelligence import IntelligenceService
from emer.services.text_intent import resolve_text_intent
from emer.settings import settings


def save(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")


class AuditedClient(OpenRouterClient):
    def __init__(self, model, directory, reasoning_effort=None):
        super().__init__(settings.openrouter_api_key, model, reasoning_effort=reasoning_effort)
        self.directory = directory
        self.calls = []

    async def record(self, operation, payload, call):
        target = self.directory / f"call-{len(self.calls) + 1:03d}-{operation}.json"
        row = {"status": "dispatched", "model": self.model, "operation": operation,
               "at": datetime.now(UTC).isoformat(), "input": payload}
        self.calls.append(row)
        save(target, row)
        try:
            result = await call()
            row.update(status="completed", usage=result.usage.model_dump(mode="json"))
            if hasattr(result, "value"):
                row["output"] = result.value.model_dump(mode="json")
            else:
                row["output"] = {"model": result.model, "vectors": result.vectors}
            return result
        except BaseException as exc:
            row.update(status="failed", error_type=type(exc).__name__, code=getattr(exc, "code", None))
            row["diagnostics"] = getattr(exc, "response_diagnostics", {})
            if getattr(exc, "usage", None):
                row["usage"] = exc.usage.model_dump(mode="json")
            raise
        finally:
            save(target, row)

    async def structured(self, system, payload, schema, operation="generation", max_tokens=4500):
        return await self.record(operation, {"system": system, "payload": payload, "schema": schema.__name__},
                                 lambda: super(AuditedClient, self).structured(
                                     system, payload, schema, operation=operation, max_tokens=max_tokens))

    async def embed(self, texts, model):
        return await self.record("query_embedding", {"texts": texts, "model": model},
                                 lambda: super(AuditedClient, self).embed(texts, model))


async def credit(out, name):
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.get("https://openrouter.ai/api/v1/credits",
                                    headers={"Authorization": "Bearer " + settings.openrouter_api_key})
        response.raise_for_status()
        data = response.json()["data"]
    save(out / name, {"at": datetime.now(UTC).isoformat(), **data})
    return data["total_credits"] - data["total_usage"]


async def main(args):
    out = Path(args.out)
    if out.exists():
        raise ValueError("Use a fresh output directory; completed and uncertain attempts are never replayed implicitly.")
    out.mkdir(parents=True)
    raw = Path(args.suite).read_bytes()
    cases = json.loads(raw)["cases"]
    if args.ids:
        ids = set(args.ids.split(","))
        cases = [case for case in cases if case["id"] in ids]
        if {case["id"] for case in cases} != ids:
            raise ValueError("Unknown selected case IDs")
    bundle = Bundle.from_bytes(Path(args.bundle).read_bytes())
    selected = settings.model_copy(update={"reranking_enabled": args.rerank})
    code = sorted([*Path("apps/api/src/emer").rglob("*.py"), *Path("apps/api/src/emer/prompts").glob("*.txt")])
    save(out / "manifest.json", {
        "suite": args.suite, "suite_sha256": sha256(raw).hexdigest(), "case_ids": [c["id"] for c in cases],
        "index_id": bundle.id, "index_checksum": bundle.checksum,
        "generator": selected.openrouter_model, "verifier": selected.verifier_model,
        "verifier_reasoning_effort": selected.verifier_reasoning_effort,
        "retrieval": {field: getattr(selected, field) for field in ["retrieval_mode", "retrieval_top_k", "retrieval_candidate_k", "context_token_budget", "reranking_enabled"]},
        "code": {str(path): sha256(path.read_bytes()).hexdigest() for path in code},
        "assessment": "Contract/provenance checks only; required-fact semantic review remains separate.",
    })
    if await credit(out, "credit-before.json") <= 0:
        raise RuntimeError("Existing provider capacity exhausted")
    outcomes, failures = [], 0
    for case in cases:
        history = []
        for number, turn in enumerate(case.get("turns") or [case], 1):
            identity = f"{case['id']}.T{number}"
            directory = out / identity
            directory.mkdir()
            gen_dir, check_dir = directory / "generator", directory / "verifier"
            gen_dir.mkdir(); check_dir.mkdir()
            generator = AuditedClient(selected.openrouter_model, gen_dir)
            verifier = AuditedClient(selected.verifier_model, check_dir, selected.verifier_reasoning_effort)
            service = IntelligenceService(generator, verifier, selected)
            started = time.perf_counter()
            row = {"id": identity, "question": turn["question"]}
            try:
                async with asyncio.timeout(selected.run_timeout_seconds):
                    state = build_dialogue_state(history, conversation_id=case["id"], context_version=1)
                    intent = await resolve_text_intent(generator, turn["question"], [], None, None, dialogue_state=state)
                    row["intent"] = intent.model_dump(mode="json")
                    if intent.clarification:
                        answer = PublishedAnswer(status="clarification", statements=[], next_steps=[],
                            gaps=[AnswerGap(part_id="intent", text=intent.clarification)], scopes=[],
                            index_id=bundle.id, index_checksum=bundle.checksum, usage=[], diagnostics={})
                        packet = None
                    else:
                        answer, packet = await service.answer(bundle, intent.question)
                row.update(status="completed", answer=answer.model_dump(mode="json"))
                errors, citation_count = [], 0
                sources = {source.doc_id: source for source in bundle.documents}
                for statement in [*answer.statements, *answer.next_steps]:
                    for citation in statement.citations:
                        citation_count += 1
                        source = sources[citation.doc_id]
                        if (source.text[citation.start:citation.end] != citation.quote
                            or citation.source_sha256 != source.sha256 or citation.index_id != bundle.id):
                            errors.append(citation.model_dump())
                row["citation_check"] = {"count": citation_count, "errors": errors}
                history.append({"id": identity, "conversation_id": case["id"], "context_version": 1,
                                "status": "completed", "question": turn["question"],
                                "context": {"resolved_question": intent.question},
                                "answer": row["answer"], "evidence": packet.model_dump(mode="json") if packet else None})
                failures = 0
            except (ProviderError, ValueError, TimeoutError) as exc:
                row.update(status="failed", error_type=type(exc).__name__, code=getattr(exc, "code", None),
                           error=str(exc), problems=getattr(exc, "problems", []))
                failures += 1
            row["latency_ms"] = round((time.perf_counter() - started) * 1000)
            row["usage"] = [call["usage"] for client in [generator, verifier] for call in client.calls if call.get("usage")]
            if service.packet:
                save(directory / "evidence.json", service.packet.model_dump(mode="json"))
            save(directory / "outcome.json", row)
            outcomes.append(row)
            save(out / "outcomes.json", outcomes)
            print(json.dumps({"id": identity, "status": row["status"], "code": row.get("code"),
                              "answer_status": row.get("answer", {}).get("status"),
                              "ms": row["latency_ms"], "cost": sum(u.get("cost") or 0 for u in row["usage"])}), flush=True)
            if failures >= 3 or row.get("code") == "provider_capacity_exhausted":
                await credit(out, "credit-after.json")
                return
    await credit(out, "credit-after.json")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--suite", required=True)
    parser.add_argument("--bundle", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--ids")
    parser.add_argument("--rerank", action="store_true")
    asyncio.run(main(parser.parse_args()))
