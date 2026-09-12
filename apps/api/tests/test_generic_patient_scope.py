"""Generic clinical questions cannot acquire a patient through embedding similarity."""

from pathlib import Path

import pytest
from emer.contracts.answer import ProviderDraft
from emer.domain.scope import patient_discovery_requested, resolve_scopes
from emer.services.answering import AnswerValidationError, validate_draft
from emer.services.ingestion import ingest, with_embeddings
from emer.services.retrieval import RetrievalService

ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture(scope="module")
def retriever():
    bundle = ingest(ROOT / "config/corpus.json")
    ids = [doc.doc_id for doc in bundle.documents]
    vectors = {identifier: [float(i == j) for i in range(len(ids))] for j, identifier in enumerate(ids)}
    return RetrievalService(with_embeddings(bundle, "scope-test-space", {chunk.chunk_id: vectors[chunk.doc_id] for chunk in bundle.chunks}))


def retrieve(retriever, question, mode, **kwargs):
    options = {}
    if mode in {"semantic", "hybrid"}:
        # Deliberately make the unrelated filler patient the strongest semantic match.
        vector = [float(identifier == "PT-005") for identifier in retriever.vector_ids]
        options = {"query_vector": vector, "query_model": "scope-test-space"}
    return retriever.evidence(question, mode=mode, top_k=8, **options, **kwargs)


@pytest.mark.parametrize("mode", ["full", "lexical", "semantic", "hybrid"])
@pytest.mark.parametrize(
    "question",
    [
        "Summarize common temporary filler effects, then tell me which product and volume to choose for an individual patient.",
        "Which filler product should we choose for a patient?",
        "What volume would you use for an unspecified patient?",
        "Explain filler risks. Which product is appropriate for a specific patient?",
        "If a patient has sensitive skin, which skincare products should they use?",
        "For a hypothetical patient with acne scars, what device setting should be used?",
        "What should the patient tell us about sun exposure before IPL?",
        "Does the patient have an injectable plan?",
        "What is the individual patient's prior filler history?",
        "Describe the effects of filler on a patient.",
        "List patient preparation instructions for IPL.",
        "What precautions apply to all patients before a procedure?",
        "Must every patient receive provider review before treatment?",
        "List expected filler effects for all patients.",
        "What documented risks should all patients understand?",
        "Summarize patient education before a chemical peel.",
        "What is follow-up policy?",
        "What are the office hours?",
        "List procedure recovery intervals.",
        "What is Alex's filler history?",
        "What is the follow-up interval for the individual patient?",
        "What is the record status for the unspecified patient?",
        "What is the preparation guidance for the hypothetical patient?",
        "What is the routine for the typical person?",
        "What is the status of the generic case?",
    ],
)
def test_generic_or_unidentified_request_never_retrieves_patient_records(retriever, question, mode):
    packet = retrieve(retriever, question, mode)
    assert not any(doc.category == "synthetic_patient" for doc in packet.sources)
    assert all(not scope.patient_ids and not scope.patient_discovery for scope in packet.scopes)
    assert all(
        not any(identifier.startswith("PT-") for identifier in scope.allowed_source_ids)
        for scope in packet.scopes
    )


@pytest.mark.parametrize(
    "question",
    [
        "Which patient has uncertain filler history?",
        "Find the patient with uncertain filler history.",
        "Describe the patient whose prior filler history is uncertain.",
        "Tell me about the 47-year-old patient.",
        "What is the acne-scar patient's status?",
        "The one who is 29 and still controlling breakouts.",
        "Who wanted downtime information before deciding?",
        "Which of the eight patients had a light-based discussion postponed?",
    ],
)
def test_actual_patient_identification_and_ambiguous_descriptions_stay_searchable(retriever, question):
    packet = retrieve(retriever, question, "full")
    assert any(scope.patient_discovery or scope.all_patients for scope in packet.scopes)
    assert len([doc for doc in packet.sources if doc.category == "synthetic_patient"]) == 8


@pytest.mark.parametrize("mode", ["full", "lexical", "semantic", "hybrid"])
@pytest.mark.parametrize("question", [
    "What is the follow-up interval for the acne-scar patient?",
    "What is the status of the acne-scar patient?",
    "Tell me the recorded interval for the acne-scar patient.",
    "What is the follow-up interval for the 47-year-old person?",
    "What is the record status for the body-contouring case?",
])
def test_later_descriptive_phrase_is_not_consumed_by_an_earlier_article(retriever, question, mode):
    assert patient_discovery_requested(question)
    initial_scopes = resolve_scopes(question, retriever.bundle.documents)
    assert all(scope.patient_discovery and len(scope.patient_ids) == 8 for scope in initial_scopes)
    packet = retrieve(retriever, question, mode)
    assert all(scope.patient_discovery for scope in packet.scopes)
    assert any(source.category == "synthetic_patient" for source in packet.sources)
    # Top-k modes narrow discovery candidates to retrieved records; they do not
    # turn a matching description into a verified unique identity.
    selected = {source.doc_id for source in packet.sources if source.category == "synthetic_patient"}
    assert all(set(scope.patient_ids) == selected and not scope.source_reference_ids for scope in packet.scopes)


def test_ambiguous_acne_scar_interval_lookup_contains_both_original_records(retriever):
    packet = retrieve(retriever, "What is the follow-up interval for the acne-scar patient?", "lexical")
    patient_sources = {source.doc_id for source in packet.sources if source.category == "synthetic_patient"}
    assert {"PT-001", "PT-004"} <= patient_sources


@pytest.mark.parametrize("mode", ["full", "lexical", "semantic", "hybrid"])
def test_descriptor_discovery_does_not_override_explicit_rejected_identity(retriever, mode):
    packet = retrieve(
        retriever,
        "Use PT-007 rather than PT-006: what is the assessment status for the body-contouring patient?",
        mode,
    )
    assert all(scope.patient_ids == ["PT-007"] and not scope.patient_discovery for scope in packet.scopes)
    assert all("PT-006" not in scope.allowed_source_ids for scope in packet.scopes)
    assert {source.doc_id for source in packet.sources if source.category == "synthetic_patient"} == {"PT-007"}


@pytest.mark.parametrize("mode", ["full", "lexical", "semantic", "hybrid"])
def test_long_descriptive_lookup_does_not_enroll_neighboring_generic_question(retriever, mode):
    packet = retrieve(
        retriever,
        "What is the follow-up interval for the acne-scar patient? What product should an individual patient choose?",
        mode,
    )
    assert packet.scopes[0].patient_discovery
    assert not packet.scopes[1].patient_discovery and not packet.scopes[1].patient_ids
    assert not any(identifier.startswith("PT-") for identifier in packet.scopes[1].allowed_source_ids)


@pytest.mark.parametrize(
    "question",
    [
        "List every patient's follow-up status.",
        "Give a compact status roll-up for every synthetic patient, one line per patient.",
        "Which patients have documented follow-up intervals?",
        "List the patients with explicit follow-up or tolerance intervals.",
        "How many patients are in the corpus?",
    ],
)
def test_explicit_complete_patient_queries_preserve_complete_set(retriever, question):
    packet = retrieve(retriever, question, "hybrid")
    assert packet.scopes[0].all_patients
    assert len([doc for doc in packet.sources if doc.category == "synthetic_patient"]) == 8


def test_explicit_patient_and_inherited_context_remain_authoritative(retriever):
    for question, context in [
        ("What is PT-006's RF history?", {}),
        ("What is their RF history?", {"patient_id": "PT-006"}),
    ]:
        packet = retrieve(retriever, question, "hybrid", **context)
        assert {doc.doc_id for doc in packet.sources if doc.category == "synthetic_patient"} == {"PT-006"}


def test_compound_request_can_discover_one_part_without_enrolling_generic_part(retriever):
    packet = retrieve(
        retriever,
        "Which patient has uncertain filler history? Which product should be used for an individual patient?",
        "hybrid",
    )
    assert packet.scopes[0].patient_discovery
    assert packet.scopes[1].patient_ids == [] and not packet.scopes[1].patient_discovery
    assert "PT-005" in packet.scopes[0].allowed_source_ids
    assert "PT-005" not in packet.scopes[1].allowed_source_ids


def test_answer_cannot_name_semantically_similar_patient_for_generic_question(retriever):
    packet = retrieve(retriever, "Which filler product should an individual patient choose?", "hybrid")
    draft = ProviderDraft.model_validate(
        {
            "statements": [
                {
                    "part_id": "part-1",
                    "text": "PT-005 has no finalized injectable plan.",
                    "citations": [{"doc_id": "PT-005", "quote": "No injectable plan finalized."}],
                }
            ],
            "gaps": [],
            "next_steps": [],
        }
    )
    with pytest.raises(AnswerValidationError, match="crossed patient scope"):
        validate_draft(packet, draft)
