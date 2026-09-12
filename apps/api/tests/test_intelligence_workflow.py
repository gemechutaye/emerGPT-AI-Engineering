"""Real chunk search with declared model doubles: bounded recovery and publication contracts."""

from types import SimpleNamespace

import pytest
from emer.contracts.answer import ProviderUsage
from emer.providers.openrouter import ProviderError, StructuredResult
from emer.services.answering import AnswerValidationError
from emer.services.intelligence import IntelligenceService
from test_knowledge_search import document, index_for


def response(schema, value):
    return StructuredResult(schema.model_validate(value), ProviderUsage(operation="fixture", model="declared", latency_ms=0))


class Generator:
    model = "declared/generator"

    def __init__(self, omit=False):
        self.calls, self.omit = [], omit

    async def structured(self, system, payload, schema, operation, **options):
        self.calls.append((operation, payload))
        if operation == "query_planning":
            return response(schema, {"parts": [{"scope_id": payload["scopes"][0]["part_id"],
                "request_text": payload["scopes"][0]["question"], "context_texts": []}]})
        facts = payload["evidence"]["assessment"]["parts"][0]["facts"]
        statements = [{"part_id": "part-1", "text": fact["text"], "citations": fact["citations"]}
                      for fact in facts if not self.omit or fact["fact_id"] != "fee"]
        draft = {"statements": statements, "next_steps": [], "gaps": []}
        if operation == "repair":
            assert schema.__name__ == "ProviderDraft"
            assert "rejected_indices" not in payload["repair"]
            assert "complete corrected ProviderDraft" in payload["repair"]["instruction"]
        return response(schema, draft)


class Verifier:
    model = "declared/independent-verifier"

    def __init__(self):
        self.calls = []

    async def structured(self, system, payload, schema, operation, **options):
        self.calls.append((operation, payload))
        if operation == "evidence_assessment":
            available = {p["doc_id"] for p in payload["evidence"]["passages"]}
            candidates = [("CAL", "cal", "Calibration is complete.", "calibration status"),
                          ("SHIP", "fee", "The shipping fee is twenty credits.", "shipping fee")]
            facts = [{"fact_id": fact_id, "text": text, "citations": [{"doc_id": doc_id, "quote": text}]}
                     for doc_id, fact_id, text, _ in candidates if doc_id in available]
            missing = [query for doc_id, _, _, query in candidates if doc_id not in available]
            return response(schema, {"parts": [{"part_id": "part-1", "status": "partial" if missing else "sufficient",
                "facts": facts, "missing_requests": missing,
                "search_query": missing[0] if missing else None}], "unassigned_requests": []})
        items = payload["items"]
        facts = payload["evidence"]["assessment"]["parts"][0]["facts"]
        fact_coverage = []
        missing = []
        for fact in facts:
            indices = [item["index"] for item in items if item["kind"] == "statement" and item["text"] == fact["text"]]
            fact_coverage.append({"fact_id": fact["fact_id"], "status": "covered" if indices else "omitted",
                                  "statement_indices": indices, "next_step_indices": [], "reason": "Declared fixture verdict."})
            if not indices:
                missing.append(fact["text"])
        return response(schema, {"source_review": [{"unit_id": unit["unit_id"], "disposition": "represented", "reason": "Declared fixture."} for unit in payload["source_audit_units"]], "verdicts": [{"kind": item["kind"], "index": item["index"], "supported": True,
            "reason": "Declared exact-source fixture.", "citation_replacements": item["citations"]} for item in items],
            "coverage": [{"part_id": "part-1", "status": "partial" if missing else "answered",
                          "reason": "Declared coverage fixture.", "missing_supported_facts": missing}],
            "fact_coverage": fact_coverage})


def configuration():
    return SimpleNamespace(retrieval_mode="lexical", retrieval_top_k=1, retrieval_candidate_k=8,
                           context_token_budget=2000, reranking_enabled=False)


async def test_missing_fact_causes_one_search_and_preserves_previously_supported_passage(tmp_path):
    bundle = index_for(tmp_path, [document("CAL", "Calibration is complete."),
                                  document("SHIP", "The shipping fee is twenty credits.")])
    generator, verifier = Generator(), Verifier()
    answer, packet = await IntelligenceService(generator, verifier, configuration()).answer(bundle, "Give calibration status and shipping fee.")
    assert answer.status == "answered"
    assert {p.doc_id for p in packet.passages} == {"CAL", "SHIP"}
    assert [round_["reason"] for round_ in packet.retrieval["search_rounds"]] == ["initial", "missing_evidence_recovery"]
    assert packet.retrieval["context_tokens"] <= 2000
    assert [operation for operation, _ in verifier.calls] == ["evidence_assessment", "evidence_assessment", "support_check"]
    assert all("text" not in source for _, payload in verifier.calls for source in payload["evidence"]["sources"])
    assert all("retrieval" not in payload["evidence"] for _, payload in verifier.calls)
    assert {c.doc_id for statement in answer.statements for c in statement.citations} == {"CAL", "SHIP"}
    assert len(answer.diagnostics["fact_coverage"]) == 2


async def test_required_fact_omission_after_one_repair_is_not_published_as_finished(tmp_path):
    bundle = index_for(tmp_path, [document("CAL", "Calibration is complete."),
                                  document("SHIP", "The shipping fee is twenty credits.")])
    generator, verifier = Generator(omit=True), Verifier()
    with pytest.raises(AnswerValidationError, match="remain missing"):
        await IntelligenceService(generator, verifier, configuration()).answer(bundle, "Give calibration status and shipping fee.")
    assert [op for op, _ in generator.calls] == ["query_planning", "generation", "repair"]
    assert [op for op, _ in verifier.calls].count("support_check") == 2
    # Even unchanged claims are independently rechecked; no earlier verdict is silently reused.
    assert all(not payload["previously_verified_items"] for op, payload in verifier.calls if op == "support_check")


def test_same_model_cannot_be_its_own_production_verifier():
    with pytest.raises(ProviderError, match="distinct"):
        IntelligenceService(Generator(), Generator(), configuration())
