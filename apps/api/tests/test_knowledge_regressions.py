"""Adversarial publication regressions; semantic verdicts are supplied test fixtures."""

from pathlib import Path

import pytest
from emer.contracts.answer import ProviderDraft, ProviderUsage
from emer.providers.openrouter import StructuredResult
from emer.services.answering import AnsweringService, AnswerValidationError, validate_draft
from emer.services.ingestion import ingest
from emer.services.retrieval import RetrievalService

ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture(scope="module")
def bundle():
    return ingest(ROOT / "config/corpus.json")


class ScriptedProvider:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.calls = []
        self.approved_items = []

    async def structured(self, system, payload, schema, operation, **kwargs):
        self.calls.append((operation, payload))
        value = next(self.replies)
        if operation == "support_check":
            cached = payload.get("previously_verified_items", [])
            for item in cached:
                assert item["kind"] != "gap"
                assert {k: v for k, v in item.items() if k != "index"} in self.approved_items
            cached_ids = {(item["kind"], item["index"]) for item in cached}
            value = {
                **value,
                "verdicts": [v for v in value["verdicts"] if (v["kind"], v["index"]) not in cached_ids],
            }
            for verdict in value["verdicts"]:
                if verdict["supported"]:
                    reviewed = next(
                        (
                            item
                            for item in payload["items"]
                            if (item["kind"], item["index"]) == (verdict["kind"], verdict["index"])
                        ),
                        None,
                    )
                    if reviewed is not None:
                        # The semantic fixture declares this exact item supported; supply
                        # its actual citation proof under the current checker protocol.
                        if verdict["kind"] != "gap":
                            verdict.setdefault("citation_replacements", reviewed["citations"])
                        self.approved_items.append({k: v for k, v in reviewed.items() if k != "index"})
        return StructuredResult(
            schema.model_validate(value),
            ProviderUsage(operation=operation, model="offline-regression", latency_ms=0),
        )


def statement(part_id, text, source):
    return {
        "part_id": part_id,
        "text": text,
        "citations": [{"doc_id": source.doc_id, "quote": source.text}],
    }


@pytest.mark.asyncio
async def test_genuine_quote_does_not_bypass_rejection_of_an_unsupported_action_order(bundle):
    question = "Could staff proceed with a light-based treatment for PT-003 if photographs are ready?"
    packet = RetrievalService(bundle).evidence(question)
    patient = next(s for s in packet.sources if s.doc_id == "PT-003")
    draft = {
        "statements": [
            statement(
                "part-1",
                "PT-003 has no selected treatment, and the provider postponed discussion until sun exposure is reassessed.",
                patient,
            )
        ],
        "gaps": [],
        "next_steps": [
            statement(
                "part-1",
                "Obtain treating-provider review after PT-003's sun exposure is reassessed.",
                patient,
            )
        ],
    }
    # Exact genuine citations establish identity only. The separate semantic boundary
    # must reject the unsupported ordering and cannot be skipped after repair.
    assert len(validate_draft(packet, ProviderDraft.model_validate(draft))[1]) == 1
    check = {
        "verdicts": [
            {"kind": "statement", "index": 0, "supported": True, "reason": "Documented record status."},
            {
                "kind": "next_step",
                "index": 0,
                "supported": False,
                "reason": "The source does not order a separate review after reassessment.",
            },
        ],
        "coverage": [
            {
                "part_id": "part-1",
                "status": "answered",
                "reason": "The surviving statement establishes the evidentiary limitation.",
            }
        ],
    }
    repair = {
        "statement_replacements": [],
        "gap_replacements": [],
        "next_step_replacements": [{"index": 0, "replacement": draft["next_steps"][0]}],
        "additions": {"statements": [], "gaps": [], "next_steps": []},
    }
    provider = ScriptedProvider([draft, check, repair, check])
    answer = await AnsweringService(provider).answer(packet, question)
    assert answer.statements[0].text == draft["statements"][0]["text"]
    assert not answer.next_steps and not answer.gaps
    assert answer.status == "answered"
    assert answer.diagnostics["withheld_count"] == 1
    assert answer.diagnostics["repair_count"] == 1
    assert [operation for operation, _ in provider.calls] == [
        "generation",
        "support_check",
        "repair",
        "support_check",
    ]
    assert [item["kind"] for item in provider.calls[-1][1]["items"]] == ["next_step"]
    cached = provider.calls[-1][1]["previously_verified_items"]
    assert [item["kind"] for item in cached] == ["statement"]
    assert cached[0]["text"] == draft["statements"][0]["text"]


def test_real_current_quote_cannot_support_the_historical_part_of_a_comparison(bundle):
    packet = RetrievalService(bundle).evidence("Compare cancellation on June 30, 2026 and July 1, 2026.")
    current = next(s for s in packet.sources if s.doc_id == "OPS-306-V2")
    draft = ProviderDraft.model_validate(
        {
            "statements": [
                statement("part-1", "The policy recommends at least 48 hours when possible.", current)
            ],
            "gaps": [],
            "next_steps": [],
        }
    )
    with pytest.raises(AnswerValidationError, match="outside this question's patient or policy scope"):
        validate_draft(packet, draft)
    draft.statements[0].part_id = "part-2"
    accepted, _ = validate_draft(packet, draft)
    assert accepted[0].citations[0].source_sha256 == current.sha256
    assert accepted[0].citations[0].index_id == packet.index_id
    assert accepted[0].citations[0].version == "2.0"
