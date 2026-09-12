"""Independent corpus/scope and publication checks; supplied verdicts are not semantic proof."""

from pathlib import Path

import pytest
from emer.contracts.answer import ProviderDraft
from emer.domain.scope import ScopeError
from emer.services.answering import AnsweringService, AnswerValidationError, validate_draft
from emer.services.ingestion import ingest
from emer.services.retrieval import RetrievalService
from emer.services.text_intent import resolve_text_intent
from test_knowledge import FakeProvider
from test_text_intent import ProviderDouble


@pytest.fixture(scope="module")
def retrieval():
    return RetrievalService(ingest(Path(__file__).resolve().parents[2] / "config/corpus.json"))


@pytest.mark.parametrize("false_claim", [
    "PT-006 has paid a deposit.",
    "The RF microneedling procedure price for PT-006 is $250.",
    "PT-006 must return on September 15, 2026.",
    "PT-006 has never had any prior aesthetic treatment.",
])
async def test_genuine_quote_does_not_override_semantic_rejection(retrieval, false_claim):
    packet = retrieval.evidence("What is documented about PT-006?", as_of="2026-09-11")
    source = next(s for s in packet.sources if s.doc_id == "PT-006")
    draft = {
        "statements": [
            {"part_id": "part-1", "text": claim,
             "citations": [{"doc_id": source.doc_id, "quote": source.text}]}
            for claim in ["PT-006 completed an educational consultation.", false_claim]
        ],
        "gaps": [], "next_steps": [],
    }
    # Deliberately genuine quotes pass only membership/span validation.
    assert len(validate_draft(packet, ProviderDraft.model_validate(draft))[0]) == 2
    check = {
        "verdicts": [
            {"kind": "statement", "index": index, "supported": index == 0,
             "reason": "Independent supplied fixture verdict, not model accuracy."}
            for index in range(2)
        ],
        "coverage": [{"part_id": "part-1", "status": "partial", "reason": "Rejected requested claim."}],
    }
    repair = {
        "statement_replacements": [{"index": 1, "replacement": draft["statements"][1]}],
        "gap_replacements": [], "next_step_replacements": [],
        "additions": {"statements": [], "gaps": [], "next_steps": []},
    }
    provider = FakeProvider([draft, check, repair, check])
    answer = await AnsweringService(provider).answer(packet, "What is documented about PT-006?")
    assert [s.text for s in answer.statements] == [draft["statements"][0]["text"]]
    assert answer.status == "partial" and answer.diagnostics["withheld_count"] == 1
    assert provider.operations == ["generation", "support_check", "repair", "support_check"]


def test_compound_patient_date_claims_cannot_borrow_other_parts_sources(retrieval):
    packet = retrieval.evidence(
        "For PT-001 in March 2026 and PT-004 in September 2026, summarize follow-up."
    )
    assert [(s.patient_ids, s.as_of) for s in packet.scopes] == [
        (["PT-001"], "2026-03-01"), (["PT-004"], "2026-09-01")
    ]
    for wrong_source in ["PT-004", "OPS-306-V2"]:
        source = next(s for s in packet.sources if s.doc_id == wrong_source)
        draft = ProviderDraft.model_validate({
            "statements": [{"part_id": "part-1", "text": "The requested detail is documented.",
                            "citations": [{"doc_id": source.doc_id, "quote": source.text}]}],
            "gaps": [], "next_steps": [],
        })
        with pytest.raises(AnswerValidationError, match="outside this question"):
            validate_draft(packet, draft)


async def test_before_policy_boundary_does_not_authorize_boundary_day_revision(retrieval):
    question = "What was the cancellation rule before July 1, 2026?"
    provider = ProviderDouble()
    intent = await resolve_text_intent(provider, question, [], None, None)
    # Clarifying a range is acceptable; silently treating 'before' as 'on' is not.
    if intent.clarification:
        assert intent.question == "" and not provider.calls
        assert "exact date" in intent.clarification.lower()
        with pytest.raises(ScopeError, match="exact date"):
            retrieval.evidence(question)
        return
    packet = retrieval.evidence(question)
    assert all("OPS-306-V2" not in scope.allowed_source_ids for scope in packet.scopes), (
        "Before July 1 was converted to July 1, excluding the historical policy and allowing V2"
    )


@pytest.mark.parametrize("mode", ["full", "lexical"])
@pytest.mark.parametrize("question,selected,expected", [
    ("List patients PT-003, PT-007 and PT-009 needing a follow-up.", None, {"PT-003", "PT-007"}),
    # An implicit follow-up uses the selection. "Which patients" is an explicit
    # discovery request, covered separately in test_scope_regressions.py.
    ("Which follow-up is due next?", "PT-004", {"PT-004"}),
    ("List the documented follow-up interval and whether it is due.", None,
     {f"PT-{number:03}" for number in range(1, 9)}),
])
def test_aggregate_wording_preserves_named_or_selected_scope(retrieval, mode, question, selected, expected):
    packet = retrieval.evidence(question, patient_id=selected, mode=mode, top_k=1)
    present = {doc.doc_id for doc in packet.sources if doc.category == "synthetic_patient"}
    assert present == expected
    if "PT-009" in question:
        assert {pid for scope in packet.scopes for pid in scope.unknown_patient_ids} == {"PT-009"}
