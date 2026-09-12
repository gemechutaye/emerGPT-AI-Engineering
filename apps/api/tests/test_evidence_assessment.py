"""Evidence-inventory contracts and scoped source boundaries; no live-model quality claims."""

from copy import deepcopy
from hashlib import sha256
from types import SimpleNamespace

import pytest
from emer.contracts.answer import EvidencePacket, EvidencePassage, QuestionScope, SourceDocument
from emer.domain.chunking import count_tokens
from emer.providers.openrouter import ProviderError
from emer.services.evidence_assessment import (
    ASSESSMENT_INSTRUCTIONS,
    EvidenceAssessment,
    EvidenceAssessmentError,
    assess_evidence,
    assessment_payload,
    validate_assessment,
)


def packet(*, text=None, selection=None, parts=1):
    text = text or "Calibration is complete. Packaging remains unfinished. Release requires safety clearance."
    source = SourceDocument(
        doc_id="OPS-901", title="Release guidance", category="operations", version="2",
        effective_date="2026-01-01", authority="Approved operations guidance",
        text=text, sha256=sha256(text.encode()).hexdigest(),
    )
    scopes = [QuestionScope(
        part_id=f"part-{index + 1}", question="What remains before release?", patient_ids=[],
        unknown_patient_ids=[], as_of="2026-09-12", date_origin="today", all_patients=False,
        warnings=[], allowed_source_ids=[source.doc_id], requested_information=["release prerequisites"],
    ) for index in range(parts)]
    passage = EvidencePassage(
        chunk_id="chunk-test", doc_id=source.doc_id, source_sha256=source.sha256, ordinal=0,
        start=0, end=len(text) if selection is None else selection, section_path=["Release"],
        text=text if selection is None else text[:selection], token_count=count_tokens(text),
        scope_ids=[scope.part_id for scope in scopes],
    )
    return EvidencePacket(
        index_id="index-immutable", index_checksum="checksum", corpus_checksum="corpus-checksum",
        sources=[source], scopes=scopes, passages=[passage],
        retrieval={"unit": "source_chunk", "scoped_selection": [
            {"part_id": scope.part_id, "complete_source_ids": [source.doc_id],
             "selected_chunk_ids": [passage.chunk_id], "budget_omitted_chunk_ids": []}
            for scope in scopes
        ]},
    )


def inventory(*, parts=1, status="sufficient", facts=True, missing=(), query=None):
    return {
        "parts": [{
            "part_id": f"part-{index + 1}", "status": status,
            "facts": ([{
                "fact_id": f"part-{index + 1}:fact-1", "text": "Calibration is complete.",
                "citations": [{"doc_id": "OPS-901", "quote": "Calibration is complete."}],
            }] if facts else []),
            "missing_requests": list(missing), "search_query": query,
        } for index in range(parts)],
        "unassigned_requests": [],
    }


class Checker:
    def __init__(self, response=None, error=None):
        self.response = response or inventory()
        self.calls = []
        self.error = error

    async def structured(self, instructions, payload, schema, **options):
        self.calls.append((instructions, deepcopy(payload), schema, options))
        if self.error:
            raise self.error
        return SimpleNamespace(value=self.response)


async def test_all_subquestions_are_assessed_once_with_independent_client_and_exact_usage_operation():
    evidence = packet(parts=2)
    checker = Checker(inventory(parts=2))
    result = await assess_evidence(checker, evidence, "What is complete and what remains before release?")
    assert isinstance(result, EvidenceAssessment) and len(result.parts) == 2
    assert len(checker.calls) == 1
    instructions, payload, schema, options = checker.calls[0]
    assert schema is EvidenceAssessment
    assert options == {"operation": "evidence_assessment", "max_tokens": 2400}
    assert payload["question"] == "What is complete and what remains before release?"
    assert len(instructions.split()) < 600
    assert payload["retrieval_coverage"][0]["requested_information"] == ["release prerequisites"]
    assert payload["retrieval_coverage"][0]["complete_source_ids"] == ["OPS-901"]
    assert "text" not in payload["evidence"]["sources"][0]
    assert payload["evidence"]["passages"][0]["text"] == evidence.sources[0].text
    assert "retrieval" not in payload["evidence"]


def test_incomplete_long_source_never_inherits_reported_complete_flag_or_sends_hidden_tail():
    visible = "Calibration is complete."
    text = visible + "\n\n" + ("Historical release context. " * 800) + "\nThe shipping fee is 75 credits."
    evidence = packet(text=text, selection=len(visible))
    evidence.retrieval["scoped_selection"][0]["budget_omitted_chunk_ids"] = ["hidden-middle", "hidden-price"]
    payload = assessment_payload(evidence, "What is the shipping fee?")
    coverage = payload["retrieval_coverage"][0]
    assert coverage["complete_source_ids"] == []
    assert coverage["incomplete_source_ids"] == ["OPS-901"]
    assert coverage["budget_omitted_chunk_count"] == 2
    assert "75 credits" not in str(payload)
    assert "excerpts" in ASSESSMENT_INSTRUCTIONS and "never that information does not exist" in ASSESSMENT_INSTRUCTIONS


def test_not_found_means_unresolved_request_and_requires_recovery_search():
    evidence = packet(selection=24)
    result = validate_assessment(evidence, inventory(
        status="not_found", facts=False, missing=["shipping fee"], query="shipping fee release guidance",
    ))
    assert result.parts[0].status == "not_found"
    assert result.parts[0].facts == []
    assert result.parts[0].search_query == "shipping fee release guidance"
    assert "confidence" not in result.model_dump_json()
    assert "absence" not in result.model_dump_json()


@pytest.mark.parametrize("extra", [{"confidence": 0.99}, {"absence_proven": True}, {"corpus_exhausted": True}])
def test_model_cannot_smuggle_unmeasured_confidence_or_global_absence_flags(extra):
    value = inventory(status="not_found", facts=False, missing=["shipping fee"], query="shipping fee")
    value["parts"][0].update(extra)
    with pytest.raises(EvidenceAssessmentError, match="typed contract"):
        validate_assessment(packet(), value)


@pytest.mark.parametrize("mutation", ["missing-part", "extra-part", "duplicate-part", "duplicate-fact-id",
                                     "duplicate-fact-text", "blank-fact-id", "duplicate-request"])
def test_part_and_fact_identity_is_complete_unique_and_nonempty(mutation):
    value = inventory(parts=2)
    if mutation == "missing-part":
        value["parts"].pop()
    elif mutation == "extra-part":
        value["parts"].append({**deepcopy(value["parts"][0]), "part_id": "foreign"})
    elif mutation == "duplicate-part":
        value["parts"][1]["part_id"] = "part-1"
    elif mutation == "duplicate-fact-id":
        value["parts"][1]["facts"][0]["fact_id"] = "part-1:fact-1"
    elif mutation == "duplicate-fact-text":
        value["parts"][0]["facts"].append({**deepcopy(value["parts"][0]["facts"][0]), "fact_id": "other"})
    elif mutation == "blank-fact-id":
        value["parts"][0]["facts"][0]["fact_id"] = " "
    else:
        value["unassigned_requests"] = ["shipping fee", " Shipping   fee "]
    with pytest.raises(EvidenceAssessmentError):
        validate_assessment(packet(parts=2), value)


@pytest.mark.parametrize("status,facts,missing,query", [
    ("sufficient", False, (), None),
    ("sufficient", True, ("fee",), None),
    ("sufficient", True, (), "fee"),
    ("partial", False, ("fee",), "fee"),
    ("partial", True, (), "fee"),
    ("partial", True, ("fee",), None),
    ("not_found", False, (), "fee"),
    ("not_found", False, ("fee",), None),
    ("ambiguous", False, (), None),
])
def test_status_fields_cannot_contradict_inventory_state(status, facts, missing, query):
    with pytest.raises(EvidenceAssessmentError):
        validate_assessment(packet(), inventory(status=status, facts=facts, missing=missing, query=query))


def test_partial_and_ambiguous_inventory_preserves_supported_facts_and_unresolved_requests():
    for status, query in [("partial", "shipping fee release"), ("ambiguous", None)]:
        result = validate_assessment(packet(), inventory(status=status, missing=["shipping fee"], query=query))
        assert len(result.parts[0].facts) == 1
        assert result.parts[0].missing_requests == ["shipping fee"]


@pytest.mark.parametrize("mutation", ["nonexistent-source", "disallowed-source", "disallowed-part", "out-of-chunk",
                                     "fabricated-quote", "joined-quote", "duplicate-citation", "invented-offset"])
def test_fact_quotes_are_continuous_and_inside_the_part_permitted_original_passages(mutation):
    evidence = packet(selection=len("Calibration is complete."), parts=2)
    value = inventory(parts=2)
    reference = value["parts"][0]["facts"][0]["citations"][0]
    if mutation == "nonexistent-source":
        reference["doc_id"] = "UNKNOWN-7"
    elif mutation == "disallowed-source":
        evidence.scopes[0].allowed_source_ids = []
    elif mutation == "disallowed-part":
        evidence.passages[0].scope_ids = ["part-2"]
    elif mutation == "out-of-chunk":
        reference["quote"] = "Packaging remains unfinished."
    elif mutation == "fabricated-quote":
        reference["quote"] = "Packaging is complete."
    elif mutation == "joined-quote":
        reference["quote"] = "Calibration is complete. Release requires safety clearance."
    elif mutation == "duplicate-citation":
        value["parts"][0]["facts"][0]["citations"].append(deepcopy(reference))
    else:
        reference["start"] = 0
    with pytest.raises(EvidenceAssessmentError):
        validate_assessment(evidence, value)


def test_copying_normalization_returns_exact_original_without_mutating_provider_reply():
    evidence = packet(text="The team’s ‘final’ check is complete.")
    value = inventory()
    value["parts"][0]["facts"][0]["citations"][0]["quote"] = "the team's 'final' check is complete."
    original = deepcopy(value)
    result = validate_assessment(evidence, value)
    assert result.parts[0].facts[0].citations[0].quote == evidence.sources[0].text
    assert value == original


def test_contiguous_selected_chunks_can_support_one_quote_but_skipped_text_cannot():
    evidence = packet()
    whole = evidence.passages[0]
    cut = 14
    evidence.passages = [
        whole.model_copy(update={"end": cut, "text": whole.text[:cut]}),
        whole.model_copy(update={"chunk_id": "chunk-2", "start": cut, "text": whole.text[cut:]}),
    ]
    assert validate_assessment(evidence, inventory()).parts[0].facts
    assert assessment_payload(evidence, "Question")["retrieval_coverage"][0]["complete_source_ids"] == ["OPS-901"]
    evidence.passages[1].start = cut + 1
    evidence.passages[1].text = whole.text[cut + 1:]
    with pytest.raises(EvidenceAssessmentError, match="continuous permitted"):
        validate_assessment(evidence, inventory())


def test_identical_quote_at_unselected_offset_does_not_hide_a_valid_selected_occurrence():
    excerpt = "Calibration is complete."
    evidence = packet(text=excerpt + "\nOther context.\n" + excerpt)
    evidence.passages[0].start = len(evidence.sources[0].text) - len(excerpt)
    evidence.passages[0].text = excerpt
    assert validate_assessment(evidence, inventory()).parts[0].facts[0].citations[0].quote == excerpt


@pytest.mark.parametrize("query", ["PT-009 release status", "shipping fee 500", "October release date",
                                   "release on 2026-12-03"])
def test_query_expansion_cannot_introduce_identifiers_numbers_or_dates(query):
    with pytest.raises(EvidenceAssessmentError, match="unobserved"):
        validate_assessment(packet(), inventory(status="partial", missing=["shipping fee"], query=query))


def test_query_expansion_can_preserve_observed_date_and_spoken_identifier():
    evidence = packet()
    evidence.scopes[0].question = "What is patient nine's current status on October 3, 2026?"
    evidence.scopes[0].patient_ids = ["PT-009"]
    result = validate_assessment(evidence, inventory(
        status="not_found", facts=False, missing=["current status"], query="PT-009 status on 2026-10-03",
    ))
    assert result.parts[0].search_query == "PT-009 status on 2026-10-03"


@pytest.mark.parametrize("mutation", ["outside-patient", "missing-patient-citation"])
def test_patient_fact_requires_in_scope_identity_and_its_record_citation(mutation):
    evidence = packet()
    value = inventory()
    value["parts"][0]["facts"][0]["text"] = "PT-009 has completed calibration."
    if mutation == "missing-patient-citation":
        evidence.scopes[0].patient_ids = ["PT-009"]
    with pytest.raises(EvidenceAssessmentError, match="patient"):
        validate_assessment(evidence, value)


@pytest.mark.parametrize("mutation", ["source-bytes", "passage-bytes", "passage-hash"])
def test_mutated_original_or_selected_passage_fails_before_assessment_dispatch(mutation):
    evidence = packet()
    if mutation == "source-bytes":
        evidence.sources[0].text += "changed"
    elif mutation == "passage-bytes":
        evidence.passages[0].text += "changed"
    else:
        evidence.passages[0].source_sha256 = "wrong"
    with pytest.raises(EvidenceAssessmentError):
        assessment_payload(evidence, "What remains before release?")


def test_empty_chunk_retrieval_never_falls_back_to_full_original_context():
    evidence = packet()
    evidence.passages = []
    payload = assessment_payload(evidence, "What remains before release?")
    assert payload["retrieval_coverage"][0]["uninspected_source_ids"] == ["OPS-901"]
    assert not payload["retrieval_coverage"][0]["complete_source_ids"]
    assert "Calibration is complete" not in str(payload)
    with pytest.raises(EvidenceAssessmentError, match="outside"):
        validate_assessment(evidence, inventory())


async def test_reassessment_does_not_feed_the_prior_inventory_back_into_checker():
    evidence = packet()
    evidence.assessment = {"untrusted_prior_inventory": "Invented shipping fee"}
    checker = Checker()
    await assess_evidence(checker, evidence, "What remains before release?")
    assert checker.calls[0][1]["evidence"]["assessment"] == {}
    assert evidence.assessment == {"untrusted_prior_inventory": "Invented shipping fee"}


async def test_operational_provider_failure_propagates_without_retry_or_absence_label():
    error = ProviderError("provider_unavailable", "Checker unavailable")
    checker = Checker(error=error)
    with pytest.raises(ProviderError) as caught:
        await assess_evidence(checker, packet(), "What remains before release?")
    assert caught.value is error and len(checker.calls) == 1


def test_malformed_patient_reference_is_a_typed_inventory_validation_failure():
    value = inventory()
    value["parts"][0]["facts"][0]["text"] = "Patient one million has completed calibration."
    with pytest.raises(EvidenceAssessmentError, match="invalid patient identifier"):
        validate_assessment(packet(), value)
