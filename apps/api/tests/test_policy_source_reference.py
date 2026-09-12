"""Historical record inspection must not change operational policy applicability."""

from datetime import date
from pathlib import Path

import pytest
from emer.contracts.answer import ProviderDraft
from emer.domain.policy import apply_policies
from emer.domain.scope import resolve_scopes
from emer.services.answering import AnswerValidationError, validate_draft
from emer.services.ingestion import ingest
from emer.services.retrieval import RetrievalService

ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture(scope="module")
def bundle():
    return ingest(ROOT / "config/corpus.json")


def policy_scopes(bundle, question):
    return [apply_policies(scope, bundle.config["policies"]) for scope in resolve_scopes(
        question, bundle.documents, today=date(2026, 9, 12),
    )]


@pytest.mark.parametrize("question", [
    "What did OPS-306-V1 say?",
    "What does OPS-306-V1 say about cancellation?",
    "Quote the cancellation terms in OPS-306-V1.",
    "Summarize OPS-306-V1.",
    "Show me the wording of OPS-306-V1.",
    "What did the source OPS-306-V1 require?",
    "ops-306-v1?",
])
def test_explicit_old_record_inspection_does_not_relabel_it_current(bundle, question):
    scope, = policy_scopes(bundle, question)
    assert scope.source_reference_ids == ["OPS-306-V1"]
    assert scope.as_of == "2026-09-12" and scope.date_origin == "today"
    assert scope.applicable_policy_ids == ["OPS-306-V2"]
    assert "OPS-306-V1" in scope.inapplicable_policy_ids
    assert {window.doc_id: window.relation for window in scope.policy_windows} == {
        "OPS-306-V1": "superseded", "OPS-306-V2": "applicable",
    }
    assert any("does not make their terms applicable" in warning for warning in scope.warnings)


@pytest.mark.parametrize("question", [
    "Compare OPS-306-V1 and OPS-306-V2.",
    "What is the difference between OPS-306-V1 and OPS-306-V2?",
    "Contrast OPS-306-V1 with OPS-306-V2.",
])
def test_record_comparison_keeps_both_documents_and_real_version_relations(bundle, question):
    scope, = policy_scopes(bundle, question)
    assert set(scope.source_reference_ids) == {"OPS-306-V1", "OPS-306-V2"}
    assert scope.applicable_policy_ids == ["OPS-306-V2"]
    assert scope.as_of == "2026-09-12"


@pytest.mark.parametrize("question", [
    "OPS-306-V1 says cancellations under 24 hours may incur a fee. Can we charge that fee today?",
    "Should we follow OPS-306-V1 now?",
    "Which cancellation rule applies today according to OPS-306-V1?",
    "I have OPS-306-V1 open. What policy currently applies?",
    "Does OPS-306-V1 still apply?",
    "Show whether OPS-306-V1 is still valid.",
    "Can I use the wording from OPS-306-V1 for today's appointment?",
])
def test_current_action_with_old_source_mention_does_not_open_old_terms(bundle, question):
    for scope in policy_scopes(bundle, question):
        assert scope.source_reference_ids == []
        assert scope.applicable_policy_ids == ["OPS-306-V2"]
        assert "OPS-306-V1" in scope.inapplicable_policy_ids


@pytest.mark.parametrize("question", [
    "What did OPS-306-V1 say and does it still apply now?",
    "Compare OPS-306-V1 and OPS-306-V2 and tell me which policy to use today.",
    "Quote OPS-306-V1; which cancellation policy applies now?",
])
def test_inspection_and_operational_clause_have_different_citation_permissions(bundle, question):
    scopes = policy_scopes(bundle, question)
    assert len(scopes) == 2
    assert "OPS-306-V1" in scopes[0].source_reference_ids
    assert scopes[1].source_reference_ids == []
    assert all(scope.applicable_policy_ids == ["OPS-306-V2"] for scope in scopes)
    assert all(scope.as_of == "2026-09-12" for scope in scopes)


def test_inspection_uses_document_metadata_without_replacing_user_date_or_patient_time(bundle):
    scope, = policy_scopes(bundle, "What did OPS-306-V1 say on March 1, 2026?")
    assert scope.as_of == "2026-03-01" and scope.date_origin == "question"
    assert scope.source_reference_ids == ["OPS-306-V1"]
    assert scope.applicable_policy_ids == ["OPS-306-V1"]
    patient, = policy_scopes(bundle, "What does PT-001 document about follow-up?")
    assert patient.as_of == "2026-09-12" and patient.date_origin == "today"
    assert patient.patient_ids == ["PT-001"]
    assert any("not patient encounters" in warning for warning in patient.warnings)


def test_reference_detection_uses_actual_record_ids_not_policy_specific_branches(bundle):
    replacements = {"OPS-306-V1": "MANUAL-ALPHA-R1", "OPS-306-V2": "MANUAL-ALPHA-R2"}
    documents = [doc.model_copy(update={"doc_id": replacements.get(doc.doc_id, doc.doc_id)})
                 for doc in bundle.documents]
    annotations = [{**item, "doc_id": replacements[item["doc_id"]],
                    "supersedes": [replacements[value] for value in item["supersedes"]]}
                   for item in bundle.config["policies"]]
    scope, = resolve_scopes("What did MANUAL-ALPHA-R1 say?", documents, today=date(2026, 9, 12))
    scoped = apply_policies(scope, annotations)
    assert scoped.source_reference_ids == ["MANUAL-ALPHA-R1"]
    assert scoped.applicable_policy_ids == ["MANUAL-ALPHA-R2"]
    unknown, = resolve_scopes("What did OPS-306-V1 say?", documents, today=date(2026, 9, 12))
    assert unknown.source_reference_ids == []


def draft_from_source(packet, part_id, doc_id):
    document = next(source for source in packet.sources if source.doc_id == doc_id)
    return ProviderDraft.model_validate({"statements": [{"part_id": part_id,
        "text": "The cited revision contains the quoted policy wording.",
        "citations": [{"doc_id": doc_id, "quote": document.text}]}], "gaps": [], "next_steps": []})


@pytest.mark.parametrize("mode", ["full", "lexical"])
def test_retrieval_and_validation_allow_attributed_inspection_only_in_its_part(bundle, mode):
    packet = RetrievalService(bundle).evidence(
        "Quote OPS-306-V1; which cancellation policy applies now?", as_of="2026-09-12", mode=mode, top_k=1,
    )
    inspection, operational = packet.scopes
    assert "OPS-306-V1" in inspection.allowed_source_ids
    assert "OPS-306-V1" not in operational.allowed_source_ids
    assert "OPS-306-V2" in operational.allowed_source_ids
    accepted, _ = validate_draft(packet, draft_from_source(packet, inspection.part_id, "OPS-306-V1"))
    assert accepted[0].citations[0].version == "1.0"
    assert accepted[0].citations[0].index_id == bundle.id
    with pytest.raises(AnswerValidationError, match="outside this question's patient or policy scope"):
        validate_draft(packet, draft_from_source(packet, operational.part_id, "OPS-306-V1"))


@pytest.mark.parametrize("mode", ["full", "lexical"])
def test_current_question_cannot_validate_old_quote_merely_because_it_was_retrieved(bundle, mode):
    packet = RetrievalService(bundle).evidence(
        "OPS-306-V1 says 24 hours. Should we use that rule today?", as_of="2026-09-12", mode=mode, top_k=1,
    )
    for scope in packet.scopes:
        assert "OPS-306-V1" not in scope.allowed_source_ids
        with pytest.raises(AnswerValidationError, match="outside this question's patient or policy scope"):
            validate_draft(packet, draft_from_source(packet, scope.part_id, "OPS-306-V1"))
