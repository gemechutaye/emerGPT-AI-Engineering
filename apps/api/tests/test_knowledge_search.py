"""Actual chunk indexes test search contracts; provider/reranker doubles are declared."""

import json
from hashlib import sha256
from types import SimpleNamespace

import pytest
from emer.contracts.answer import EvidencePassage, SourceDocument
from emer.contracts.knowledge import QuestionPlan, SearchPart
from emer.domain.scope import resolve_scopes
from emer.providers.openrouter import ProviderError
from emer.services.ingestion import IngestionError, canonical, ingest, with_embeddings
from emer.services.knowledge_search import KnowledgeSearch, get_chunk_index, retrieve_plan, tokens
from emer.services.question_planning import PlanDraft, plan_question


def document(identifier, text, *, title=None, patient=False, effective_date="2026-01-01"):
    return SourceDocument(
        doc_id=identifier,
        title=title or f"Original reference {identifier}",
        category="synthetic_patient" if patient else "operations",
        version="1.0",
        effective_date=effective_date,
        authority="Synthetic Patient Source" if patient else "Approved Training Dataset",
        text=text,
        sha256=sha256(text.encode()).hexdigest(),
    )


def index_for(tmp_path, documents, *, annotations=None, authority=None):
    raw = b"".join(canonical(doc.model_dump(exclude={"sha256"})) + b"\n" for doc in documents)
    path = tmp_path / "sources.jsonl"
    path.write_bytes(raw)
    manifest = {
        "schema_version": 2 if annotations is not None else 1,
        "corpus": {
            "path": str(path),
            "records": len(documents),
            "bytes": len(raw),
            "sha256": sha256(raw).hexdigest(),
        },
        "preserve": [],
    }
    if annotations is None:
        manifest["policies"] = []
    else:
        manifest.update(source_metadata=annotations, authority=authority or {})
    location = tmp_path / "manifest.json"
    location.write_text(json.dumps(manifest))
    return ingest(location)


def plan_for(bundle, queries, *, as_of="2026-09-01"):
    parts = []
    for number, query in enumerate(queries, 1):
        part_id = f"part-{number}"
        scope = resolve_scopes(query, bundle.documents, as_of=as_of)[0].model_copy(
            update={"part_id": part_id}
        )
        parts.append(SearchPart(part_id=part_id, query=query, requested_information=[query], scope=scope))
    return QuestionPlan(original_question="; ".join(queries), parts=parts, planner="declared-test-plan")


class DeclaredReranker:
    def __init__(self, preferred):
        self.identity = {"model": "declared-test-reranker", "revision": "fixture-v1"}
        self.preferred = preferred
        self.calls = []

    def score(self, query, passages):
        self.calls.append((query, passages))
        return [10.0 if self.preferred in text else 0.0 for text in passages]


def test_long_source_search_selects_original_late_span_not_full_source(tmp_path):
    doc = document(
        "DOC-LONG",
        "# Routine\n\n"
        + "Routine record. " * 1500
        + "\n\n# Rare topic\n\nQuartz narwhal remains the exact lookup target.",
    )
    bundle = index_for(tmp_path, [doc])
    packet = KnowledgeSearch(bundle).search(
        plan_for(bundle, ["quartz narwhal"]), mode="lexical", top_k=1, token_budget=1200
    )
    assert len(bundle.chunks) > 10
    assert len(packet.passages) == 1
    passage = packet.passages[0]
    assert "Quartz narwhal" in passage.text
    assert passage.start > 0 and passage.text == doc.text[passage.start : passage.end]
    assert passage.source_sha256 == doc.sha256 and passage.scope_ids == ["part-1"]
    assert len(passage.text) < len(doc.text) / 10
    assert packet.retrieval["context_tokens"] <= 1200


def test_topical_exact_source_address_does_not_let_opening_chunks_displace_ranked_target(tmp_path):
    paragraphs = ["# Context\n\n"] + [
        f"## Section {number}\n\n" + "Routine administrative context. " * 70 + "\n\n" for number in range(12)
    ]
    paragraphs.append("## Target\n\nQuartz narwhal is the key condition. " + "Relevant context. " * 85)
    doc = document("DOC-LONG", "".join(paragraphs))
    bundle = index_for(tmp_path, [doc])
    reranker = DeclaredReranker("Quartz narwhal")
    packet = KnowledgeSearch(bundle, reranker=reranker).search(
        plan_for(bundle, ["In DOC-LONG, what is the quartz narwhal condition?"]),
        mode="lexical",
        candidate_k=24,
        top_k=1,
        token_budget=900,
    )
    assert any("Quartz narwhal" in passage.text for passage in packet.passages), (
        "A topical direct source address must prioritize the relevant late passage, "
        "not spend its budget on every source chunk in document order."
    )
    assert packet.retrieval["context_tokens"] <= 900


def test_parts_have_independent_semantic_rankings_and_permissions(tmp_path):
    a, b = document("A", "Coral reference."), document("B", "Glacier reference.")
    bundle = index_for(tmp_path, [a, b])
    bundle = with_embeddings(
        bundle,
        "fixture/space",
        {chunk.chunk_id: [1.0, 0.0] if chunk.doc_id == "A" else [0.0, 1.0] for chunk in bundle.chunks},
    )
    packet = KnowledgeSearch(bundle).search(
        plan_for(bundle, ["coral", "glacier"]),
        mode="semantic",
        vectors={"part-1": [1, 0], "part-2": [0, 1]},
        model="fixture/space",
        top_k=1,
        token_budget=2000,
    )
    assert packet.scopes[0].allowed_source_ids == ["A"]
    assert packet.scopes[1].allowed_source_ids == ["B"]
    assert packet.retrieval["scoped_selection"][0]["raw_ranked_ids"][0] == "A"
    assert packet.retrieval["scoped_selection"][1]["raw_ranked_ids"][0] == "B"
    assert {passage.doc_id: passage.scope_ids for passage in packet.passages} == {
        "A": ["part-1"],
        "B": ["part-2"],
    }


def test_cross_encoder_order_selects_top_k_from_real_ranked_candidates(tmp_path):
    docs = [
        document("A", "Quartz quartz quartz ordinary."),
        document("B", "Quartz narwhal preferred."),
        document("C", "Quartz alternative."),
    ]
    bundle = index_for(tmp_path, docs)
    reranker = DeclaredReranker("narwhal")
    packet = KnowledgeSearch(bundle, reranker=reranker).search(
        plan_for(bundle, ["quartz"]), mode="lexical", candidate_k=3, top_k=1
    )
    assert packet.scopes[0].allowed_source_ids == ["B"]
    assert len(reranker.calls) == 1 and len(reranker.calls[0][1]) == 3
    assert packet.retrieval["reranker"] == reranker.identity
    assert len(packet.retrieval["scoped_selection"][0]["reranked_chunk_ids"]) == 3


def test_generic_family_adds_one_ranked_chunk_per_related_source_not_all_chunks(tmp_path):
    a = document("A", "Quartz narwhal.")
    b = document(
        "B",
        "# Background\n\n" + "Administrative context. " * 1500 + "\n\n# Target\n\nQuartz related condition.",
    )
    annotations = [{"doc_id": doc.doc_id, "family": "topic", "logical_id": doc.doc_id} for doc in [a, b]]
    bundle = index_for(tmp_path, [a, b], annotations=annotations)
    packet = KnowledgeSearch(bundle).search(
        plan_for(bundle, ["quartz narwhal"]), mode="lexical", candidate_k=1, top_k=1
    )
    b_passages = [passage for passage in packet.passages if passage.doc_id == "B"]
    assert len(b_passages) == 1 < len([chunk for chunk in bundle.chunks if chunk.doc_id == "B"])
    assert "Quartz related condition" in b_passages[0].text
    assert packet.scopes[0].conflicting_source_ids == []


def test_round_robin_preserves_two_parts_with_one_candidate_each_under_shared_budget(tmp_path):
    docs = [
        document("A", "Coral route one. " * 30),
        document("B", "Glacier route two. " * 30),
        document("C", "Coral ancillary detail. " * 30),
    ]
    bundle = index_for(tmp_path, docs)
    packet = KnowledgeSearch(bundle).search(
        plan_for(bundle, ["coral", "glacier"]), mode="lexical", top_k=2, token_budget=1100
    )
    assert packet.scopes[0].allowed_source_ids
    assert packet.scopes[1].allowed_source_ids == ["B"]
    assert packet.retrieval["context_tokens"] <= 1100
    actual = tokens(
        {
            "sources": [doc.model_dump(exclude={"text"}) for doc in packet.sources],
            "passages": [passage.model_dump(mode="json") for passage in packet.passages],
        }
    )
    assert actual == packet.retrieval["context_tokens"]


def test_budget_omissions_do_not_claim_complete_source_coverage(tmp_path):
    doc = document("LONG", "# Context\n\n" + "Reference qualification. " * 2000)
    bundle = index_for(tmp_path, [doc])
    packet = KnowledgeSearch(bundle).search(
        plan_for(bundle, ["Read LONG."]), mode="lexical", top_k=24, token_budget=900
    )
    diagnostic = packet.retrieval["scoped_selection"][0]
    assert diagnostic["budget_omitted_chunk_ids"]
    assert diagnostic["complete_source_ids"] == []
    assert any("absence is not established" in warning for warning in packet.scopes[0].warnings)


def test_duplicate_passage_charges_once_and_retains_both_scope_ids(tmp_path):
    bundle = index_for(tmp_path, [document("A", "Coral and glacier route.")])
    packet = KnowledgeSearch(bundle).search(plan_for(bundle, ["coral", "glacier"]), mode="lexical")
    assert len(packet.passages) == 1
    assert packet.passages[0].scope_ids == ["part-1", "part-2"]
    assert all(scope.allowed_source_ids == ["A"] for scope in packet.scopes)


def test_semantic_space_and_invalid_query_vectors_fail_before_evidence(tmp_path):
    bundle = index_for(tmp_path, [document("A", "Original source.")])
    bundle = with_embeddings(bundle, "fixture/space", {chunk.chunk_id: [1, 0] for chunk in bundle.chunks})
    index = get_chunk_index(bundle)
    for vector, model in [
        ([1, 0], "other/space"),
        ([1], "fixture/space"),
        ([0, 0], "fixture/space"),
        ([float("nan"), 0], "fixture/space"),
    ]:
        with pytest.raises(IngestionError):
            index.semantic(vector, model)


def test_index_cache_reuses_exact_content_and_invalidates_changed_corpus(tmp_path):
    bundle = index_for(tmp_path, [document("A", "Original source.")])
    first = get_chunk_index(bundle)
    assert get_chunk_index(bundle) is first
    updated = index_for(tmp_path, [document("A", "Changed source.")])
    assert updated.checksum != bundle.checksum and get_chunk_index(updated) is not first
    assert get_chunk_index(bundle).documents["A"].text == "Original source."


def test_version_filtering_and_historical_inspection_remain_distinct(tmp_path):
    old = document("RULE-V1", "Effective 2026-01-01 until 2026-06-30. Old quartz term.")
    current = document(
        "RULE-V2",
        "Effective 2026-07-01. This supersedes RULE-V1. Current quartz term.",
        effective_date="2026-07-01",
    )
    annotations = [
        {
            "doc_id": doc.doc_id,
            "logical_id": "quartz-rule",
            "family": "quartz-rules",
            "date_semantics": "effective",
            "effective_from": doc.effective_date,
            "effective_until": "2026-06-30" if doc is old else None,
            "supersedes": [] if doc is old else ["RULE-V1"],
            "provenance": [
                {"field": "effective_from", "quote": doc.text},
                {"field": "effective_until" if doc is old else "supersedes", "quote": doc.text},
            ],
        }
        for doc in [old, current]
    ]
    bundle = index_for(tmp_path, [old, current], annotations=annotations)
    operational = KnowledgeSearch(bundle).search(
        plan_for(bundle, ["Which quartz rule applies now?"]), mode="lexical"
    )
    assert operational.scopes[0].allowed_source_ids == ["RULE-V2"]
    inspection = KnowledgeSearch(bundle).search(plan_for(bundle, ["Read RULE-V1."]), mode="lexical")
    assert "RULE-V1" in inspection.scopes[0].allowed_source_ids
    assert "RULE-V1" in inspection.scopes[0].inapplicable_source_ids
    assert "RULE-V1" not in inspection.scopes[0].applicable_source_ids


class EmbeddingProvider:
    def __init__(self):
        self.calls = []

    async def embed(self, texts, model):
        self.calls.append((texts, model))
        return SimpleNamespace(model=model, vectors=[[1, 0] if "coral" in text else [0, 1] for text in texts])


@pytest.mark.asyncio
async def test_retrieve_plan_embeds_each_part_query_as_separate_batch_item(tmp_path):
    bundle = index_for(tmp_path, [document("A", "Coral guide."), document("B", "Glacier guide.")])
    bundle = with_embeddings(
        bundle,
        "fixture/space",
        {chunk.chunk_id: [1, 0] if chunk.doc_id == "A" else [0, 1] for chunk in bundle.chunks},
    )
    settings = SimpleNamespace(
        retrieval_mode="semantic",
        reranking_enabled=False,
        retrieval_candidate_k=8,
        retrieval_top_k=1,
        context_token_budget=2000,
    )
    provider = EmbeddingProvider()
    packet = await retrieve_plan(bundle, provider, plan_for(bundle, ["coral", "glacier"]), settings)
    assert provider.calls == [(["coral", "glacier"], "fixture/space")]
    assert [scope.allowed_source_ids for scope in packet.scopes] == [["A"], ["B"]]


class PlannerProvider:
    def __init__(self, parts):
        self.parts = parts

    async def structured(self, system, payload, schema, **kwargs):
        return SimpleNamespace(value=PlanDraft(parts=self.parts))


@pytest.mark.asyncio
@pytest.mark.parametrize("attribute", ["Exact $999 charge", "PT-888 history", "Follow-up on 2028-01-01"])
async def test_planner_rejects_invented_details_in_requested_attributes(tmp_path, attribute):
    bundle = index_for(tmp_path, [document("A", "Original guidance.")])
    provider = PlannerProvider(
        [
            {
                "scope_id": "part-1",
                "query": "What is the consultation cost?",
                "requested_information": [attribute],
            }
        ]
    )
    with pytest.raises(ProviderError, match="plan"):
        await plan_question(provider, "What is the consultation cost?", bundle, as_of="2026-09-01")


@pytest.mark.asyncio
async def test_planner_rejects_numeric_detail_borrowed_from_another_patient_scope(tmp_path):
    patients = [
        document("PT-451", "Original first patient.", patient=True),
        document("PT-452", "Original second patient.", patient=True),
    ]
    bundle = index_for(tmp_path, patients)
    question = "PT-451: is a 2-week review recorded?; PT-452: is a 6-week review recorded?"
    provider = PlannerProvider(
        [
            {
                "scope_id": "part-1",
                "query": "Does PT-451 have a 6-week review?",
                "requested_information": ["Review interval"],
            },
            {
                "scope_id": "part-2",
                "query": "Does PT-452 have a 6-week review?",
                "requested_information": ["Review interval"],
            },
        ]
    )
    with pytest.raises(ProviderError, match="plan"):
        await plan_question(provider, question, bundle, as_of="2026-09-01")


def test_budget_accounting_counts_unicode_and_original_source_metadata(tmp_path):
    doc = document("UNICODE", "Café 東京 👩🏽‍⚕️. Exact source words.")
    bundle = index_for(tmp_path, [doc])
    packet = KnowledgeSearch(bundle).search(plan_for(bundle, ["café"]), mode="lexical", token_budget=1000)
    assert packet.passages[0].text == doc.text
    passage = EvidencePassage(**bundle.chunks[0].model_dump(), scope_ids=["part-1"])
    assert packet.retrieval["context_tokens"] == tokens(
        {
            "sources": [doc.model_dump(exclude={"text"})],
            "passages": [passage.model_dump(mode="json")],
        }
    )
