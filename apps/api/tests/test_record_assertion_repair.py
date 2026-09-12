"""Record-language guards through the real answer/repair/publication pipeline.

Evidence and provider outputs are declared fixtures. No providers or database
are called; these establish enforcement and one-repair behavior, not model skill.
"""

from copy import deepcopy

import pytest
from emer.contracts.answer import EvidencePacket, QuestionScope
from emer.services.answering import AnsweringService, AnswerValidationError
from test_answering_coverage import ProviderDouble, check, draft
from test_record_assertions import source


@pytest.fixture
def packet():
    patient = source("record-river", "Procedure history is not documented. "
                     "Consultation scheduled. Photography and measurements completed.")
    overview = source("workflow-map", "The practice documents baseline appearance and treatment areas. "
                      "The licensed provider determines product choice and dose.", patient=False)
    return EvidencePacket(
        index_id="declared-record-repair-fixture", index_checksum="fixture-index-sha",
        corpus_checksum="fixture-corpus-sha", sources=[patient, overview],
        scopes=[QuestionScope(
            part_id="part-1", question="Explain the record and relevant general workflow.",
            patient_ids=[patient.doc_id], unknown_patient_ids=[], as_of="2026-01-01",
            date_origin="question", all_patients=False, warnings=[],
            allowed_source_ids=[patient.doc_id, overview.doc_id],
        )], retrieval={"mode": "declared-fixture"},
    )


def item(packet, text, *, mixed=False):
    return {
        "part_id": "part-1", "text": text,
        "citations": [
            {"doc_id": document.doc_id, "quote": document.text}
            for document in packet.sources[:2 if mixed else 1]
        ],
    }


def assert_traceable(packet, answer):
    sources = {document.doc_id: document for document in packet.sources}
    for published in [*answer.statements, *answer.next_steps]:
        for citation in published.citations:
            original = sources[citation.doc_id]
            assert citation.quote == original.text[citation.start:citation.end]
            assert citation.source_sha256 == original.sha256
            assert citation.index_id == packet.index_id
            assert citation.title == original.title and citation.version == original.version


@pytest.mark.parametrize("kind", ["history", "workflow"])
@pytest.mark.parametrize("field", ["statements", "next_steps"])
async def test_ambiguous_record_clause_gets_one_repair_before_semantic_check(packet, kind, field):
    stable = item(packet, "Photography and measurements are completed.")
    if kind == "history":
        bad = item(packet, "The record documents an upcoming consultation and no procedure history so far.")
        corrected = item(packet, "The procedure history is not documented.")
        problem = "Negative history is ambiguous"
    else:
        bad = item(packet, "The consultation addresses preferences, with baseline appearance and treatment areas documented.",
                   mixed=True)
        corrected = item(packet, "The general overview says baseline appearance and treatment areas are documented.",
                         mixed=True)
        problem = "Mixed patient and general-source citations"
    original = draft([stable, bad] if field == "statements" else [stable],
                     next_steps=[bad] if field == "next_steps" else [])
    repaired = draft([stable, corrected] if field == "statements" else [stable],
                     next_steps=[corrected] if field == "next_steps" else [])
    before = deepcopy(original)
    provider = ProviderDouble([original, repaired, check(repaired, "answered", "Declared supported repair.")])

    answer = await AnsweringService(provider).answer(packet, packet.scopes[0].question)

    assert [call[0] for call in provider.calls] == ["generation", "repair", "support_check"]
    feedback = provider.calls[1][2]["repair"]
    assert feedback["draft"] == before
    assert any(problem in reason and field + "[" in reason for reason in feedback["problems"])
    assert "Preserve supported parts" in feedback["instruction"]
    reviewed_text = [entry["text"] for entry in provider.calls[2][2]["items"]]
    assert corrected["text"] in reviewed_text and bad["text"] not in reviewed_text
    assert answer.status == "answered" and answer.diagnostics["repair_count"] == 1
    assert answer.diagnostics["record_assertion_policy"].startswith("source-aware-record-clarity-v1")
    assert answer.statements[0].text == stable["text"]
    assert [entry.text for entry in getattr(answer, field)][-1] == corrected["text"]
    assert [entry.operation for entry in answer.usage] == ["generation", "repair", "support_check"]
    assert original == before  # Validation does not silently normalize ambiguous prose.
    assert_traceable(packet, answer)


@pytest.mark.parametrize("kind", ["history", "workflow"])
@pytest.mark.parametrize("check_support", [True, False])
async def test_repair_that_remains_ambiguous_fails_closed_without_a_second_repair(packet, kind, check_support):
    text = "There is no procedure history." if kind == "history" else "Baseline appearance is documented."
    ambiguous = draft([item(packet, text, mixed=kind == "workflow")])
    provider = ProviderDouble([ambiguous, ambiguous])

    with pytest.raises(AnswerValidationError) as rejected:
        await AnsweringService(provider, check_support=check_support).answer(packet, "Explain the record.")

    assert [call[0] for call in provider.calls] == ["generation", "repair"]
    assert len(rejected.value.attempt_usage) == 2
    assert all(entry["operation"] in {"generation", "repair"} for entry in rejected.value.attempt_usage)
    assert "Negative history" in str(rejected.value) if kind == "history" else "Mixed patient" in str(rejected.value)


async def test_explicit_negative_history_real_completed_work_and_provider_instruction_publish_unchanged(packet):
    packet.sources[0] = source("record-river", "No prior procedure history. "
                               "Consultation scheduled. Photography and measurements completed.")
    value = draft([
        item(packet, "There is no prior procedure history."),
        item(packet, "Photography and measurements are completed.", mixed=True),
        item(packet, "The licensed provider must determine product choice and dose.", mixed=True),
    ])
    provider = ProviderDouble([value, check(value, "answered", "Declared explicit source support.")])

    answer = await AnsweringService(provider).answer(packet, "Explain the record and remaining provider decisions.")

    assert [call[0] for call in provider.calls] == ["generation", "support_check"]
    assert [entry.text for entry in answer.statements] == [entry["text"] for entry in value["statements"]]
    assert answer.status == "answered" and answer.diagnostics["repair_count"] == 0
    assert_traceable(packet, answer)
