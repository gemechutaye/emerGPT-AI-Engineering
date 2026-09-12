"""Comparative authorization is shared only within one explicit patient/date task."""

from datetime import date
from pathlib import Path

import pytest
from emer.contracts.answer import ProviderDraft
from emer.domain.scope import resolve_scopes
from emer.services.answering import AnswerValidationError, validate_draft
from emer.services.ingestion import ingest
from emer.services.retrieval import RetrievalService

ROOT = Path(__file__).resolve().parents[3]
TODAY = date(2026, 9, 12)


@pytest.fixture(scope="module")
def bundle():
    return ingest(ROOT / "config/corpus.json")


@pytest.mark.parametrize(
    "question",
    [
        "Contrast PT-001 and PT-004’s scar-planning stage, using the acne overview to explain why they cannot be assigned the same next procedure.",
        "Compare PT-001 and PT-004 scar-planning stages.",
        "How do PT-001 and PT-004 differ in readiness?",
        "What is the difference between PT-001 and PT-004?",
        "PT-001 versus PT-004: compare their current planning stage.",
        "PT-001 vs PT-004?",
        "Compare patient one and patient four.",
        "Contrast the status of PT-001 and PT-004 as of 2026-09-01.",
        "On September 1, 2026, compare PT-001 and PT-004.",
        "Compare PT-001 on 2026-09-01 and PT-004 on 2026-09-01.",
    ],
)
def test_named_same_date_comparison_keeps_both_requested_identities(bundle, question):
    scopes = resolve_scopes(question, bundle.documents, patient_id="PT-008", today=TODAY)
    assert len(scopes) == 1
    assert scopes[0].patient_ids == ["PT-001", "PT-004"]
    assert not scopes[0].all_patients and not scopes[0].patient_discovery


@pytest.mark.parametrize(
    "question,expected",
    [
        ("PT-001 status and PT-004 status", [(["PT-001"], "2026-09-12"), (["PT-004"], "2026-09-12")]),
        (
            "Compare PT-001 to the acne overview; PT-004 status?",
            [(["PT-001"], "2026-09-12"), (["PT-004"], "2026-09-12")],
        ),
        (
            "Compare PT-001 to the acne overview? PT-004 status?",
            [(["PT-001"], "2026-09-12"), (["PT-004"], "2026-09-12")],
        ),
        (
            "Compare PT-001 on May 1, 2026 and PT-004 on September 1, 2026.",
            [(["PT-001"], "2026-05-01"), (["PT-004"], "2026-09-01")],
        ),
        (
            "Compare PT-001 on May 1, 2026 and PT-004.",
            [(["PT-001"], "2026-05-01"), (["PT-004"], "2026-09-12")],
        ),
        (
            "Compare PT-001 today and PT-004 on May 1, 2026.",
            [(["PT-001"], "2026-09-12"), (["PT-004"], "2026-05-01")],
        ),
    ],
)
def test_independent_patient_clauses_and_date_pairing_remain_separate(bundle, question, expected):
    scopes = resolve_scopes(question, bundle.documents, today=TODAY)
    assert [(scope.patient_ids, scope.as_of) for scope in scopes] == expected


@pytest.mark.parametrize("mode", ["lexical", "full"])
def test_comparative_claim_can_cite_both_requested_records_but_never_a_third(bundle, mode):
    packet = RetrievalService(bundle).evidence(
        "Contrast PT-001 and PT-004 scar-planning stages.", mode=mode, top_k=1
    )
    scope = packet.scopes[0]
    assert len(packet.scopes) == 1
    assert {doc.doc_id for doc in packet.sources if doc.category == "synthetic_patient"} == {
        "PT-001",
        "PT-004",
    }
    sources = {doc.doc_id: doc for doc in packet.sources}
    value = {
        "statements": [
            {
                "part_id": scope.part_id,
                "text": "PT-001 and PT-004 have different documented planning contexts.",
                "citations": [{"doc_id": key, "quote": sources[key].text[:80]} for key in scope.patient_ids],
            }
        ],
        "gaps": [],
        "next_steps": [],
    }
    statements, _ = validate_draft(packet, ProviderDraft.model_validate(value))
    assert {cite.doc_id for cite in statements[0].citations} == {"PT-001", "PT-004"}
    value["statements"][0]["text"] = "PT-008 has the same status as PT-001 and PT-004."
    with pytest.raises(AnswerValidationError, match="crossed patient scope"):
        validate_draft(packet, ProviderDraft.model_validate(value))
    value["statements"][0]["text"] = "PT-001 and PT-004 have different documented planning contexts."
    value["statements"][0]["citations"] = value["statements"][0]["citations"][:1]
    with pytest.raises(AnswerValidationError, match="patient-specific statement must cite"):
        validate_draft(packet, ProviderDraft.model_validate(value))


def test_comparison_unknown_patient_remains_missing_not_a_substitution(bundle):
    scope = resolve_scopes("Compare PT-001 and PT-999.", bundle.documents, today=TODAY)[0]
    assert scope.patient_ids == ["PT-001"] and scope.unknown_patient_ids == ["PT-999"]
