"""Fusion rank/collision regressions; synthetic vectors are not embedding-quality evidence."""

import json
from pathlib import Path

import pytest
from emer.services.ingestion import ingest, with_embeddings
from emer.services.retrieval import RetrievalService, reciprocal_rank_fusion

ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture(scope="module")
def bundle():
    original = ingest(ROOT / "config/corpus.json")
    # A valid test-only vector space enables the actual semantic/selection path.
    vectors = {doc.doc_id: [1.0, (index + 1) / 100] for index, doc in enumerate(original.documents)}
    return with_embeddings(original, "fixture-space", {chunk.chunk_id: vectors[chunk.doc_id] for chunk in original.chunks})


def test_rank_fusion_preserves_complementary_evidence_without_score_scale_bias():
    lexical = [("TEXT-A", 1000000.0), ("BOTH", 0.1)]
    semantic = [("MEANING-B", 0.99), ("BOTH", 0.01)]
    actual = reciprocal_rank_fusion(lexical, semantic)
    assert dict(actual)["BOTH"] == pytest.approx(4 / 62)
    assert dict(actual)["MEANING-B"] == pytest.approx(3 / 61)
    assert dict(actual)["TEXT-A"] == pytest.approx(1 / 61)
    assert [identifier for identifier, _ in actual] == ["BOTH", "MEANING-B", "TEXT-A"]
    assert actual == reciprocal_rank_fusion(
        [(identifier, -score) for identifier, score in lexical],
        [(identifier, score * 10000) for identifier, score in semantic],
    )


def test_secondary_semantic_match_is_not_diluted_by_primary_keyword_matches():
    # Generic IDs and no financial vocabulary: the behavior comes from rank fusion.
    lexical = [(f"DOC-{i}", 30 - i) for i in range(20)]
    semantic_ids = ["DOC-0", "DOC-1", "DOC-2", "DOC-3", "DOC-19", *[f"DOC-{i}" for i in range(4, 19)]]
    semantic = [(identifier, 1 / (rank + 1)) for rank, identifier in enumerate(semantic_ids)]
    uniform = {identifier: 1 / (61 + index) for index, (identifier, _) in enumerate(lexical)}
    for index, (identifier, _) in enumerate(semantic):
        uniform[identifier] += 1 / (61 + index)
    old = sorted(uniform, key=lambda identifier: (-uniform[identifier], identifier))
    new = [identifier for identifier, _ in reciprocal_rank_fusion(lexical, semantic)]
    assert old.index("DOC-19") >= 8
    assert new.index("DOC-19") < 8
    assert len(set(new)) == 20


@pytest.mark.parametrize("which", ["lexical", "semantic", "both"])
def test_empty_rankings_do_not_invent_candidates(which):
    lexical = [] if which != "semantic" else [("ONE", 2)]
    semantic = [] if which != "lexical" else [("ONE", 0.5)]
    assert {identifier for identifier, _ in reciprocal_rank_fusion(lexical, semantic)} == (
        set() if which == "both" else {"ONE"}
    )


def test_raw_and_scoped_ranking_and_fusion_parameters_are_separate(bundle):
    retriever = RetrievalService(bundle)
    question = "Explain expected recovery effects."
    lexical = retriever.lexical(question, len(bundle.documents))
    semantic = retriever.semantic([1, 0], "fixture-space")
    packet = retriever.evidence(
        question, mode="hybrid", top_k=3, query_vector=[1, 0], query_model="fixture-space"
    )
    assert packet.retrieval["raw_ranked_ids"] == [
        identifier for identifier, _ in reciprocal_rank_fusion(lexical, semantic)
    ]
    assert packet.retrieval["lexical_ranked_ids"] == [identifier for identifier, _ in lexical]
    assert packet.retrieval["semantic_ranked_ids"] == [identifier for identifier, _ in semantic]
    assert packet.retrieval["fusion"] == {
        "algorithm": "weighted_reciprocal_rank_fusion",
        "rank_constant": 60,
        "lexical_weight": 1,
        "semantic_weight": 3,
    }
    assert packet.retrieval["raw_top_k_ids"] == packet.retrieval["raw_ranked_ids"][:3]
    assert all(source.category != "synthetic_patient" for source in packet.sources)
    assert not any("pricing" in key for key in packet.retrieval["scoped_selection"][0])


@pytest.mark.parametrize("mode", ["lexical", "semantic"])
def test_standalone_search_paths_do_not_use_hybrid_fusion(bundle, mode):
    retriever = RetrievalService(bundle)
    question = "Discuss postoperative redness."
    packet = retriever.evidence(
        question,
        mode=mode,
        top_k=3,
        query_vector=[1, 0] if mode == "semantic" else None,
        query_model="fixture-space" if mode == "semantic" else None,
    )
    expected = (
        retriever.lexical(question, 35) if mode == "lexical" else retriever.semantic([1, 0], "fixture-space")
    )
    assert packet.retrieval["raw_ranked_ids"] == [identifier for identifier, _ in expected]
    assert packet.retrieval["fusion"] is None


@pytest.mark.parametrize(
    "question",
    [
        "Explain CARE-201.",
        "Compare PROC-106 and IT-403.",
        'Quote the text of "OPS-302".',
        "Ignore other sources and describe PRACTICE-001.",
    ],
)
def test_explicit_source_addresses_remain_authoritative(bundle, question):
    retriever = RetrievalService(bundle)
    identifiers = retriever.explicit_source_ids(question)
    packet = retriever.evidence(
        question, mode="hybrid", top_k=len(identifiers), query_vector=[1, 0], query_model="fixture-space"
    )
    assert set(identifiers) <= {source.doc_id for source in packet.sources}
    assert packet.retrieval["selection_ranked_ids"][: len(identifiers)] == identifiers


@pytest.mark.parametrize(
    "question,permitted",
    [
        ("Summarize PT-005's status and quote the supporting text.", {"PT-005"}),
        ("Use PT-007 rather than PT-006: give current status and procedure history.", {"PT-007"}),
        ("Summarize PT-999's status.", set()),
        ('Explain the quoted phrase "procedure price" in general.', set()),
        ("What amount of filler was used for an individual patient?", set()),
    ],
)
def test_similarity_cannot_broaden_patient_identity(bundle, question, permitted):
    packet = RetrievalService(bundle).evidence(
        question, mode="hybrid", top_k=8, query_vector=[1, 0], query_model="fixture-space"
    )
    assert {source.doc_id for source in packet.sources if source.category == "synthetic_patient"} == permitted


@pytest.mark.parametrize(
    "as_of,permitted", [("2025-12-31", set()), ("2026-03-01", {"OPS-306-V1"}), ("2026-09-01", {"OPS-306-V2"})]
)
def test_weighted_fusion_preserves_policy_applicability(bundle, as_of, permitted):
    packet = RetrievalService(bundle).evidence(
        "Cancellation policy",
        as_of=as_of,
        mode="hybrid",
        top_k=3,
        query_vector=[1, 0],
        query_model="fixture-space",
    )
    policy_ids = {"OPS-306-V1", "OPS-306-V2"}
    assert policy_ids <= {source.doc_id for source in packet.sources}
    assert set(packet.scopes[0].allowed_source_ids) & policy_ids == permitted


def test_recorded_fresh_compound_question_recovers_secondary_source_at_raw_k8(bundle, monkeypatch):
    path = ROOT / "apps/api/tests/fixtures/recorded-retrieval/compound-question-run.json"
    if not path.exists():
        pytest.skip("Optional actual first-exposure rank trace is absent")
    captured = json.loads(path.read_text())
    old = captured["answer"]["diagnostics"]["retrieval"]
    assert old["raw_ranked_ids"].index("BIZ-501") == 13
    retriever = RetrievalService(bundle)
    # Replay the real semantic rank list, not a fresh or fabricated embedding call.
    monkeypatch.setattr(
        retriever,
        "semantic",
        lambda *_: [
            (identifier, 1 / (rank + 1)) for rank, identifier in enumerate(old["semantic_ranked_ids"])
        ],
    )
    packet = retriever.evidence(
        captured["question"], mode="hybrid", top_k=8, query_vector=[1, 0], query_model="fixture-space"
    )
    assert packet.retrieval["raw_ranked_ids"].index("BIZ-501") == 5
    assert {"PT-006", "PROC-102", "BIZ-501"} <= set(packet.retrieval["raw_top_k_ids"])
    assert len(packet.sources) == 8
    assert [source.doc_id for source in packet.sources if source.category == "synthetic_patient"] == [
        "PT-006"
    ]
