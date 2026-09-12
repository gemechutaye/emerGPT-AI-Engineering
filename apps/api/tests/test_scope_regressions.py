"""Deterministic scope regressions against the original corpus; no provider calls."""

from pathlib import Path

import pytest
from emer.services.ingestion import ingest
from emer.services.retrieval import RetrievalService

ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture(scope="module")
def retriever():
    return RetrievalService(ingest(ROOT / "config/corpus.json"))


def retrieved_patients(packet):
    return {source.doc_id for source in packet.sources if source.category == "synthetic_patient"}


@pytest.mark.parametrize("mode", ["full", "lexical"])
@pytest.mark.parametrize(
    "question",
    [
        "Which patients, PT-001 or PT-006, have a follow-up interval?",
        "List the follow-up status for patients PT-001 and PT-006.",
        "List all patients PT-001 or PT-006 with a follow-up interval.",
    ],
)
def test_aggregate_wording_preserves_explicit_patient_subset(retriever, question, mode):
    packet = retriever.evidence(question, mode=mode, top_k=1)
    assert retrieved_patients(packet) == {"PT-001", "PT-006"}
    assert {identifier for scope in packet.scopes for identifier in scope.patient_ids} == {
        "PT-001",
        "PT-006",
    }
    assert all(not scope.all_patients for scope in packet.scopes)
    for scope in packet.scopes:
        assert set(scope.allowed_source_ids) & retrieved_patients(packet) == set(scope.patient_ids)


@pytest.mark.parametrize("mode", ["full", "lexical"])
@pytest.mark.parametrize(
    "question",
    [
        "Which patients, PT-009 or PT-006, have completed a consultation?",
        "List all patients PT-009 or PT-006 with a completed consultation.",
    ],
)
def test_aggregate_wording_retains_unknown_patient_without_substitution(retriever, question, mode):
    packet = retriever.evidence(question, patient_id="PT-005", mode=mode, top_k=1)
    assert retrieved_patients(packet) == {"PT-006"}
    assert packet.scopes[0].patient_ids == ["PT-006"]
    assert packet.scopes[0].unknown_patient_ids == ["PT-009"]
    assert not packet.scopes[0].all_patients
    assert any("must not be substituted" in warning for warning in packet.scopes[0].warnings)


@pytest.mark.parametrize("mode", ["full", "lexical"])
@pytest.mark.parametrize(
    "question",
    [
        "Are the two-, six-, and four-week follow-up intervals overdue?",
        "List the documented follow-up intervals.",
        "Which follow up is due next?",
        "Compare the documented followups.",
        "Which follow-ups are overdue?",
    ],
)
def test_generic_followup_aggregation_enumerates_every_record(retriever, question, mode):
    packet = retriever.evidence(question, as_of="2026-09-30", mode=mode, top_k=1)
    expected = {doc.doc_id for doc in retriever.bundle.documents if doc.category == "synthetic_patient"}
    assert packet.scopes[0].all_patients
    assert set(packet.scopes[0].patient_ids) == expected
    assert retrieved_patients(packet) == expected
    assert packet.scopes[0].as_of == "2026-09-30"
    assert any("publication dates" in warning for warning in packet.scopes[0].warnings)


@pytest.mark.parametrize("mode", ["full", "lexical"])
@pytest.mark.parametrize(
    "question",
    ["Which follow-up is due next?", "List the documented follow-up intervals."],
)
def test_inferred_aggregation_preserves_selected_patient(retriever, question, mode):
    packet = retriever.evidence(question, patient_id="PT-006", mode=mode, top_k=1)
    assert packet.scopes[0].patient_ids == ["PT-006"]
    assert not packet.scopes[0].all_patients
    assert not packet.scopes[0].patient_discovery
    assert retrieved_patients(packet) == {"PT-006"}


@pytest.mark.parametrize("mode", ["full", "lexical"])
def test_which_patient_question_is_discovery_even_with_selected_patient(retriever, mode):
    packet = retriever.evidence(
        "Which patients have documented follow-ups?", patient_id="PT-006", mode=mode, top_k=8
    )
    scope = packet.scopes[0]
    assert scope.patient_discovery and not scope.all_patients
    assert set(scope.patient_ids) <= {doc.doc_id for doc in retriever.bundle.documents}
    assert retrieved_patients(packet) == set(scope.patient_ids)
    if mode == "full":
        assert len(scope.patient_ids) == 8


@pytest.mark.parametrize("mode", ["full", "lexical"])
@pytest.mark.parametrize("quantifier", ["all patients", "every patient", "each patient"])
def test_explicit_universal_request_overrides_selected_context(retriever, quantifier, mode):
    packet = retriever.evidence(
        f"List the follow-up status for {quantifier}.", patient_id="PT-006", mode=mode, top_k=1
    )
    expected = {doc.doc_id for doc in retriever.bundle.documents if doc.category == "synthetic_patient"}
    assert packet.scopes[0].all_patients
    assert set(packet.scopes[0].patient_ids) == expected
    assert retrieved_patients(packet) == expected


@pytest.mark.parametrize("mode", ["full", "lexical"])
@pytest.mark.parametrize(
    "question",
    ["Which intervals are overdue?", "List procedure recovery intervals.", "What is follow-up policy?"],
)
def test_generic_question_does_not_admit_neighboring_patient_records(retriever, question, mode):
    """A general topic or unidentified patient request cannot enroll similar records."""
    packet = retriever.evidence(question, mode=mode, top_k=1)
    scope = packet.scopes[0]
    assert not scope.all_patients and not scope.patient_discovery and not scope.patient_ids
    assert not retrieved_patients(packet)
    assert any("general knowledge only" in warning for warning in scope.warnings)
