"""Deterministic domain/provider-contract checks; no paid calls or live-quality claims."""

import json
from datetime import date
from pathlib import Path

import httpx
import pytest
from emer.contracts.answer import ProviderDraft, ProviderUsage
from emer.domain.policy import apply_policies
from emer.domain.scope import ScopeError, resolve_scopes
from emer.providers.openrouter import OpenRouterClient, ProviderError, StructuredResult
from emer.services.answering import AnsweringService, AnswerValidationError, validate_draft
from emer.services.ingestion import Bundle, IngestionError, ingest
from emer.services.retrieval import RetrievalService

ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture(scope="module")
def bundle():
    return ingest(ROOT / "config/corpus.json")


def draft_for(
    packet,
    doc_id="PT-006",
    text="PT-006 completed an educational consultation.",
    quote="Educational consultation completed.",
):
    return ProviderDraft.model_validate(
        {
            "statements": [
                {
                    "part_id": packet.scopes[0].part_id,
                    "text": text,
                    "citations": [{"doc_id": doc_id, "quote": quote}],
                }
            ],
            "gaps": [],
            "next_steps": [],
        }
    )


def test_ingests_only_original_manifest_and_preserves_hashes(bundle):
    assert len(bundle.documents) == 35
    assert bundle.corpus_checksum == "b66264c95a383b2169650083ca85bd9f50cf3929b838ac7790ff6b6049a6901a"
    assert sum(d.category == "synthetic_patient" for d in bundle.documents) == 8
    assert len({d.doc_id for d in bundle.documents}) == 35


def test_bundle_reproducible_and_reconstructable(bundle):
    rebuilt = ingest(ROOT / "config/corpus.json")
    assert bundle.data == rebuilt.data
    restored = Bundle.from_bytes(bundle.data, bundle.checksum)
    assert restored.documents == bundle.documents
    assert restored.id == bundle.id


def test_bundle_checksum_rejects_modified_cache(bundle):
    with pytest.raises(IngestionError, match="checksum"):
        Bundle.from_bytes(bundle.data + b"changed", bundle.checksum)


def test_mutated_original_or_evaluation_sentinel_fails(tmp_path):
    manifest = json.loads((ROOT / "config/corpus.json").read_text())
    source = tmp_path / "corpus.jsonl"
    source.write_bytes(
        (ROOT / "EMER_AI_TakeHome_Knowledge_Corpus.jsonl").read_bytes()
        + b'\n{"evaluation_sentinel":"never ingest"}\n'
    )
    manifest["corpus"]["path"] = str(source)
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(IngestionError, match="verification failed"):
        ingest(manifest_path)


def test_undeclared_source_field_rejected(tmp_path):
    manifest = json.loads((ROOT / "config/corpus.json").read_text())
    manifest["evaluation_files"] = ["evals/cases.json"]
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest))
    with pytest.raises(IngestionError, match="Unsupported"):
        ingest(path)


def test_patient_scope_cannot_leak_neighbors(bundle):
    packet = RetrievalService(bundle).evidence("Summarize PT 6")
    assert packet.scopes[0].patient_ids == ["PT-006"]
    assert {s.doc_id for s in packet.sources if s.category == "synthetic_patient"} == {"PT-006"}


def test_unknown_patient_never_substituted(bundle):
    packet = RetrievalService(bundle).evidence("What is PT-009's status?", patient_id="PT-006")
    assert packet.scopes[0].unknown_patient_ids == ["PT-009"]
    assert not packet.scopes[0].patient_ids
    assert not any(s.category == "synthetic_patient" for s in packet.sources)


def test_all_patient_question_enumerates_every_record(bundle):
    packet = RetrievalService(bundle).evidence(
        "List all patients and their follow-up status", mode="lexical", top_k=1
    )
    assert len(packet.scopes[0].patient_ids) == 8
    assert sum(s.category == "synthetic_patient" for s in packet.sources) == 8


def test_multi_patient_parts_stay_independent(bundle):
    packet = RetrievalService(bundle).evidence("What is PT-001's status and PT-006's status?")
    assert [scope.patient_ids for scope in packet.scopes] == [["PT-001"], ["PT-006"]]
    assert "PT-006" not in packet.scopes[0].allowed_source_ids


def test_context_patient_only_used_without_explicit_override(bundle):
    assert RetrievalService(bundle).evidence("What is their status?", patient_id="PT-005").scopes[
        0
    ].patient_ids == ["PT-005"]
    assert RetrievalService(bundle).evidence("What is PT-004's status?", patient_id="PT-005").scopes[
        0
    ].patient_ids == ["PT-004"]


def test_invalid_context_identifier_and_calendar_date_fail(bundle):
    with pytest.raises(ScopeError):
        RetrievalService(bundle).evidence("Status", patient_id="PT-banana")
    with pytest.raises(ScopeError):
        RetrievalService(bundle).evidence("Cancellation on 2026-02-31")


def test_march_and_september_resolve_independently(bundle):
    scopes = (
        RetrievalService(bundle).evidence("Compare cancellation rules in March and September 2026.").scopes
    )
    assert [(s.as_of, s.applicable_policy_ids) for s in scopes] == [
        ("2026-03-01", ["OPS-306-V1"]),
        ("2026-09-01", ["OPS-306-V2"]),
    ]
    assert "OPS-306-V2" not in scopes[0].allowed_source_ids
    assert "OPS-306-V1" not in scopes[1].allowed_source_ids


@pytest.mark.parametrize(
    "as_of,expected",
    [
        ("2025-12-31", []),
        ("2026-01-01", ["OPS-306-V1"]),
        ("2026-06-30", ["OPS-306-V1"]),
        ("2026-07-01", ["OPS-306-V2"]),
    ],
)
def test_policy_interval_boundaries(bundle, as_of, expected):
    assert (
        RetrievalService(bundle).evidence("Cancellation", as_of=as_of).scopes[0].applicable_policy_ids
        == expected
    )


def test_unresolved_overlapping_policy_is_visible(bundle):
    scope = resolve_scopes("Cancellation", bundle.documents, today=date(2026, 9, 10))[0]
    annotations = [
        dict(bundle.config["policies"][1]),
        {**bundle.config["policies"][1], "doc_id": "policy-conflict", "supersedes": []},
    ]
    result = apply_policies(scope, annotations)
    assert set(result.conflicting_policy_ids) == {"OPS-306-V2", "policy-conflict"}


def test_fts_injection_is_quoted_and_safe(bundle):
    retriever = RetrievalService(bundle)
    assert isinstance(retriever.lexical('" OR NEAR( -- * : title )'), list)
    # Search operators stay data, and do not authorize arbitrary patient records.
    assert all(
        source.category != "synthetic_patient"
        for source in retriever.evidence('" OR NEAR( -- * : title )').sources
    )


def test_no_fake_semantic_index(bundle):
    with pytest.raises(ValueError, match="no built index"):
        RetrievalService(bundle).evidence("follow-up", mode="semantic")


def test_citation_offsets_and_metadata_are_server_owned(bundle):
    packet = RetrievalService(bundle).evidence("Status PT-006")
    statements, _ = validate_draft(packet, draft_for(packet))
    citation = statements[0].citations[0]
    source = next(s for s in packet.sources if s.doc_id == citation.doc_id)
    assert source.text[citation.start : citation.end] == citation.quote
    assert citation.title == source.title and citation.version == source.version
    assert citation.source_sha256 == source.sha256


def test_invalid_quote_is_rejected_not_fuzzy_matched(bundle):
    packet = RetrievalService(bundle).evidence("Status PT-006")
    with pytest.raises(AnswerValidationError, match="exact original"):
        validate_draft(packet, draft_for(packet, quote="Educational consultation completed!"))


def test_patient_statement_requires_its_patient_citation(bundle):
    packet = RetrievalService(bundle).evidence("What does PT-006 need?", mode="full")
    doc = next(d for d in packet.sources if d.doc_id == "OPS-303")
    with pytest.raises(AnswerValidationError, match="patient's source"):
        validate_draft(packet, draft_for(packet, doc_id=doc.doc_id, quote=doc.text[:100]))


def test_historical_policy_cannot_cite_current_revision(bundle):
    packet = RetrievalService(bundle).evidence("Cancellation in March 2026")
    with pytest.raises(AnswerValidationError, match="scope"):
        validate_draft(
            packet,
            draft_for(
                packet,
                doc_id="OPS-306-V2",
                text="48 hours is required.",
                quote="Current policy, effective July 1, 2026.",
            ),
        )


def test_provider_cannot_supply_offsets_or_metadata(bundle):
    packet = RetrievalService(bundle).evidence("PT-006")
    raw = draft_for(packet).model_dump()
    raw["statements"][0]["citations"][0]["start"] = 0
    with pytest.raises(ValueError):
        ProviderDraft.model_validate(raw)


class FakeProvider:
    def __init__(self, values):
        self.values = iter(values)
        self.operations = []
        self.approved_items = []

    async def structured(self, system, payload, schema, operation, max_tokens=4500):
        self.operations.append(operation)
        value = next(self.values)
        if isinstance(value, Exception):
            raise value
        if operation == "support_check":
            # Legacy canned reviews contain every draft unit. The current checker
            # reviews only new/changed units; immutable approved prose stays visible.
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
            ProviderUsage(operation=operation, model="deterministic-test-double", latency_ms=1),
        )


@pytest.mark.asyncio
async def test_answer_publish_after_checker_and_usage_record(bundle):
    packet = RetrievalService(bundle).evidence("PT-006 status")
    provider = FakeProvider(
        [
            draft_for(packet).model_dump(),
            {
                "coverage": [
                    {
                        "part_id": "part-1",
                        "status": "answered",
                        "reason": "Requested completed action is answered.",
                    }
                ],
                "verdicts": [
                    {
                        "kind": "statement",
                        "index": 0,
                        "supported": True,
                        "reason": "Entailed by cited consultation status.",
                    }
                ],
            },
        ]
    )
    answer = await AnsweringService(provider).answer(packet, "PT-006 status")
    assert answer.status == "answered"
    assert provider.operations == ["generation", "support_check"]
    assert len(answer.usage) == 2
    assert answer.usage[0].cost is None


@pytest.mark.asyncio
async def test_checker_outage_is_not_a_knowledge_gap(bundle):
    packet = RetrievalService(bundle).evidence("PT-006 status")
    provider = FakeProvider(
        [draft_for(packet).model_dump(), ProviderError("provider_unavailable", "Unavailable")]
    )
    with pytest.raises(ProviderError) as raised:
        await AnsweringService(provider).answer(packet, "PT-006 status")
    assert raised.value.code == "provider_unavailable"
    assert len(raised.value.attempt_usage) == 1


@pytest.mark.asyncio
async def test_checker_missing_items_fails_closed(bundle):
    packet = RetrievalService(bundle).evidence("PT-006 status")
    provider = FakeProvider(
        [
            draft_for(packet).model_dump(),
            {
                "verdicts": [],
                "coverage": [{"part_id": "part-1", "status": "answered", "reason": "Test coverage"}],
            },
        ]
    )
    with pytest.raises(AnswerValidationError, match="exactly once"):
        await AnsweringService(provider).answer(packet, "PT-006 status")


@pytest.mark.asyncio
async def test_repair_is_bounded_once(bundle):
    packet = RetrievalService(bundle).evidence("PT-006 status")
    bad = draft_for(packet, quote="Imaginary consultation detail.").model_dump()
    provider = FakeProvider([bad, bad])
    with pytest.raises(AnswerValidationError):
        await AnsweringService(provider).answer(packet, "PT-006 status")
    assert provider.operations == ["generation", "repair"]


@pytest.mark.asyncio
async def test_openrouter_no_key_returns_explicit_unconfigured():
    with pytest.raises(ProviderError) as raised:
        await OpenRouterClient(None, "unverified-model").structured("", {}, ProviderDraft)
    assert raised.value.code == "provider_unconfigured"


@pytest.mark.asyncio
async def test_openrouter_contract_actual_usage_and_no_fallback():
    calls = []

    async def handle(request):
        calls.append(request)
        return httpx.Response(
            200,
            json={
                "id": "test-call",
                "model": "model-returned",
                "provider": "provider-returned",
                "usage": {"prompt_tokens": 12, "completion_tokens": 7, "total_tokens": 19, "cost": 0.0002},
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": '{"statements":[],"gaps":[],"next_steps":[]}'},
                    }
                ],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        result = await OpenRouterClient("test-only-key", "model-requested", ["endpoint"], client).structured(
            "system", {}, ProviderDraft
        )
    sent = json.loads(calls[0].content)
    assert sent["provider"]["allow_fallbacks"] is False
    assert sent["provider"]["only"] == ["endpoint"]
    assert calls[0].headers["X-OpenRouter-Cache"] == "false"
    assert result.usage.total_tokens == 19 and result.usage.cost == 0.0002
    assert result.usage.model == "model-returned"


@pytest.mark.asyncio
async def test_provider_timeout_has_no_automatic_retry():
    attempts = 0

    async def handle(request):
        nonlocal attempts
        attempts += 1
        raise httpx.ReadTimeout("Timeout")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(ProviderError) as raised:
            await OpenRouterClient("test-only-key", "model", client=client).structured("", {}, ProviderDraft)
    assert attempts == 1
    assert raised.value.code == "provider_timeout"


def test_embedding_bundle_is_new_immutable_space_and_restores(bundle):
    from emer.services.ingestion import with_embeddings

    vectors = {doc.doc_id: [float(i + 1), 1.0, 0.0] for i, doc in enumerate(bundle.documents)}
    embedded = with_embeddings(bundle, "test-space", {chunk.chunk_id: vectors[chunk.doc_id] for chunk in bundle.chunks})
    assert embedded.id != bundle.id and embedded.checksum != bundle.checksum
    assert bundle.config["embedding"] is None
    assert embedded.data == with_embeddings(bundle, "test-space", {chunk.chunk_id: vectors[chunk.doc_id] for chunk in bundle.chunks}).data
    restored = RetrievalService(Bundle.from_bytes(embedded.data, embedded.checksum))
    ranked = restored.semantic(vectors[bundle.documents[0].doc_id], "test-space")
    assert ranked[0][0] == bundle.documents[0].doc_id
    assert ranked[0][1] == pytest.approx(1.0)


def test_semantic_and_hybrid_scope_cannot_leak_other_patients(bundle):
    from emer.services.ingestion import with_embeddings

    vectors = {doc.doc_id: [1.0, float(i)] for i, doc in enumerate(bundle.documents)}
    retriever = RetrievalService(with_embeddings(bundle, "test-space", {chunk.chunk_id: vectors[chunk.doc_id] for chunk in bundle.chunks}))
    for mode in ("semantic", "hybrid"):
        packet = retriever.evidence(
            "PT-006 status", mode=mode, query_vector=[1, 2], query_model="test-space", top_k=3
        )
        assert {doc.doc_id for doc in packet.sources if doc.category == "synthetic_patient"} == {"PT-006"}
        assert len(packet.retrieval["raw_top_k_ids"]) == 3
        assert packet.retrieval["policy_family_expansion"] == []


def test_embedding_spaces_and_dimensions_cannot_be_mixed(bundle):
    from emer.services.ingestion import with_embeddings

    vectors = {doc.doc_id: [1.0, 2.0] for doc in bundle.documents}
    retriever = RetrievalService(with_embeddings(bundle, "test-space", {chunk.chunk_id: vectors[chunk.doc_id] for chunk in bundle.chunks}))
    with pytest.raises(ValueError, match="different model"):
        retriever.semantic([1, 2], "another-space")
    with pytest.raises(ValueError, match="dimensions"):
        retriever.semantic([1], "test-space")
    with pytest.raises(IngestionError, match="every source"):
        with_embeddings(bundle, "test-space", {"PT-006": [1, 2]})


@pytest.mark.asyncio
async def test_embedding_provider_reorders_by_index_and_records_actual_usage():
    async def handle(request):
        assert request.url.path.endswith("/embeddings")
        assert json.loads(request.content)["provider"]["allow_fallbacks"] is False
        return httpx.Response(
            200,
            json={
                "model": "test-space",
                "data": [{"index": 1, "embedding": [0.0, 1.0]}, {"index": 0, "embedding": [1.0, 0.0]}],
                "usage": {"prompt_tokens": 4, "total_tokens": 4},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        result = await OpenRouterClient("test-only-key", "unused-generation", client=client).embed(
            ["first", "second"], "test-space"
        )
    assert result.vectors == [[1.0, 0.0], [0.0, 1.0]]
    assert result.usage.total_tokens == 4 and result.usage.cost is None


def test_evaluation_fixture_counts_hashes_and_all_source_coverage(bundle):
    from emer.evaluation.__main__ import load_suite

    development, _ = load_suite("development")
    holdout, _ = load_suite("holdout")
    voice, _ = load_suite("voice")
    assert len(development["cases"]) == 40 and len(holdout["cases"]) == 20 and len(voice["cases"]) == 12
    assert {s for c in development["cases"] for s in c["required_sources"]} == {
        d.doc_id for d in bundle.documents
    }
    assert all(c["assessment"] is None for c in development["cases"] + holdout["cases"])


def test_google_request_subset_keeps_server_contract_bounds():
    client = OpenRouterClient("test-only-key", "google/gemini-3.6-flash")
    schema = client._schema(ProviderDraft)
    assert "minLength" not in json.dumps(schema) and "maxLength" not in json.dumps(schema)
    assert "minLength" in json.dumps(ProviderDraft.model_json_schema())


@pytest.mark.asyncio
async def test_default_verified_luna_route_and_token_parameter():
    async def handle(request):
        body = json.loads(request.content)
        assert body["provider"]["only"] == ["azure/eu"]
        assert body["max_completion_tokens"] == 4500 and "max_tokens" not in body
        return httpx.Response(
            200,
            json={
                "model": "openai/gpt-5.6-luna",
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": '{"statements":[],"gaps":[],"next_steps":[]}'},
                    }
                ],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        await OpenRouterClient("test-only-key", "openai/gpt-5.6-luna", client=client).structured(
            "", {}, ProviderDraft
        )


def test_multiple_explicit_years_do_not_share_first_year(bundle):
    packet = RetrievalService(bundle).evidence("Compare cancellation in March 2025 and September 2026.")
    assert [s.as_of for s in packet.scopes] == ["2025-03-01", "2026-09-01"]
    assert packet.scopes[0].applicable_policy_ids == []
    assert packet.scopes[1].applicable_policy_ids == ["OPS-306-V2"]


@pytest.mark.parametrize("mutation", ["expiry", "supersedes"])
def test_policy_annotations_cannot_invent_an_interval_or_predecessor(tmp_path, mutation):
    manifest = json.loads((ROOT / "config/corpus.json").read_text())
    manifest["corpus"]["path"] = str(ROOT / "EMER_AI_TakeHome_Knowledge_Corpus.jsonl")
    manifest["preserve"][0]["path"] = str(ROOT / "EMER_AI_TakeHome_Data_README.txt")
    manifest = {"schema_version": 1, "corpus": manifest["corpus"], "preserve": manifest["preserve"],
                "policies": ingest(ROOT / "config/corpus.json").config["policies"]}
    if mutation == "expiry":
        manifest["policies"][0]["valid_through"] = "2026-06-20"
    else:
        manifest["policies"][1]["supersedes"] = ["PT-006"]
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest))
    with pytest.raises(IngestionError, match="not backed"):
        ingest(path)


def test_evaluation_ledger_fences_competing_runner_and_preserves_uncertain_attempt(tmp_path):
    from emer.evaluation.__main__ import Recorder

    first = Recorder(tmp_path, {"configuration": "fixed"})
    first.append({"attempt_id": "uncertain", "status": "dispatched", "job": {"model": "test"}})
    with pytest.raises(ValueError, match="Another evaluator"):
        Recorder(tmp_path, {"configuration": "fixed"})
    first.handle.close()
    resumed = Recorder(tmp_path, {"configuration": "fixed"})
    assert "uncertain" in resumed.seen
    resumed.handle.close()


def test_source_mutation_is_isolated_and_cannot_change_active_original(bundle):
    from emer.evaluation.__main__ import load_suite
    from emer.evaluation.mutations import mutate_packet

    suite, _ = load_suite("mutations")
    packet = RetrievalService(bundle).evidence("What does PT-006 want before deciding?")
    changed = mutate_packet(packet, suite["cases"][0])
    original = next(doc for doc in packet.sources if doc.doc_id == "PT-006")
    mutated = next(doc for doc in changed.sources if doc.doc_id == "PT-006")
    assert "No prior RF microneedling." in original.text
    assert "one prior RF microneedling treatment" in mutated.text
    assert changed.index_id.startswith("eval-") and changed.index_id != packet.index_id
    assert changed.retrieval["never_activate"] is True
    assert original.sha256 != mutated.sha256


def test_evaluation_resume_retains_completed_uncertain_and_other_failed_but_releases_402(tmp_path):
    from emer.evaluation.__main__ import Recorder, retain_prior_attempts

    old = Recorder(tmp_path / "old", {"command": "retrieval", "suite_sha256": "fixed"})
    rows = [
        ("done", "completed", None),
        ("uncertain", "dispatched", None),
        ("capacity", "failed", "provider_capacity_exhausted"),
        ("other", "failed", "provider_timeout"),
    ]
    for name, status, error in rows:
        old.append(
            {
                "attempt_id": name,
                "status": status,
                "job": {"model": "test", "case_id": name},
                "error": {"code": error} if error else {},
            }
        )
    old.handle.close()
    config = {"command": "retrieval", "suite_sha256": "fixed", "resume_from": str(tmp_path / "old")}
    resumed = Recorder(tmp_path / "new", config)
    retain_prior_attempts(resumed, tmp_path / "old", config, True)
    assert len(resumed.seen) == 3
    assert resumed.id_for({"model": "test", "case_id": "capacity"}) not in resumed.seen
    resumed.handle.close()


@pytest.mark.parametrize(
    "question",
    [
        "What may the assistant do, and what boundaries apply to patient-specific answers?",
        "May the assistant recommend a treatment?",
        "What symptoms may occur after treatment?",
        "May I cancel or reschedule my appointment?",
        "Which cancellation fees may be waived?",
    ],
)
def test_modal_may_does_not_replace_date_scope(bundle, question):
    scope = RetrievalService(bundle).evidence(question, as_of="2026-09-10").scopes[0]
    assert scope.as_of == "2026-09-10"
    assert scope.date_origin == "context"
    assert scope.applicable_policy_ids == ["OPS-306-V2"]


@pytest.mark.parametrize(
    "question,expected",
    [
        ("What policy applied in May?", ["2026-05-01"]),
        ("What policy applied for may 2026?", ["2026-05-01"]),
        ("What policy applied on May 15, 2026?", ["2026-05-15"]),
        ("What policy applied on May 15?", ["2026-05-15"]),
        ("May 15", ["2026-05-15"]),
        ("May 2026 cancellation policy", ["2026-05-01"]),
        ("May", ["2026-05-01"]),
        ("Compare March and May 2026.", ["2026-03-01", "2026-05-01"]),
        ("Compare May and September 2026.", ["2026-05-01", "2026-09-01"]),
        ("In May, what may the assistant say?", ["2026-05-01"]),
        ("What may the assistant say in September 2026?", ["2026-09-01"]),
    ],
)
def test_explicit_may_dates_and_mixed_modal_phrases(bundle, question, expected):
    scopes = RetrievalService(bundle).evidence(question, as_of="2026-09-10").scopes
    assert [scope.as_of for scope in scopes] == expected
    assert all(scope.date_origin == "question" for scope in scopes)


@pytest.mark.asyncio
async def test_semantic_rejection_after_one_repair_withholds_only_rejected_content(bundle):
    """Control-flow verification only; supplied verdicts do not measure checker accuracy."""
    packet = RetrievalService(bundle).evidence("Current cancellation policy", as_of="2026-09-10")
    source = next(doc for doc in packet.sources if doc.doc_id == "OPS-306-V2")
    draft = {
        "statements": [
            {
                "part_id": packet.scopes[0].part_id,
                "text": text,
                "citations": [{"doc_id": source.doc_id, "quote": source.text}],
            }
            for text in (
                "The cancellation guidance preserves the qualification when possible.",
                "Cancellation always requires at least 48 hours of notice with no exceptions.",
            )
        ],
        "gaps": [],
        "next_steps": [],
    }
    verdicts = {
        "coverage": [
            {
                "part_id": "part-1",
                "status": "partial",
                "reason": "Only one requested aspect remains supported.",
            }
        ],
        "verdicts": [
            {"kind": "statement", "index": 0, "supported": True, "reason": "Preserves qualification."},
            {"kind": "statement", "index": 1, "supported": False, "reason": "Invents a hard obligation."},
        ],
    }
    repair = {
        "statement_replacements": [{"index": 1, "replacement": draft["statements"][1]}],
        "gap_replacements": [],
        "next_step_replacements": [],
        "additions": {"statements": [], "gaps": [], "next_steps": []},
    }
    provider = FakeProvider([draft, verdicts, repair, verdicts])
    answer = await AnsweringService(provider).answer(packet, "Current cancellation policy")
    assert len(answer.statements) == 1
    assert answer.statements[0].text == draft["statements"][0]["text"]
    assert answer.status == "partial"
    assert answer.diagnostics["withheld_count"] == 1
    assert answer.diagnostics["repair_count"] == 1
    assert provider.operations == ["generation", "support_check", "repair", "support_check"]


def test_support_payload_keeps_per_kind_indexes_explicit(bundle):
    from emer.services.answering import support_payload

    packet = RetrievalService(bundle).evidence("PT-006 status")
    draft = draft_for(packet)
    draft.next_steps = [draft.statements[0].model_copy()]
    raw = draft.model_dump()
    raw["gaps"] = [{"part_id": packet.scopes[0].part_id, "text": "An appointment date is not documented."}]
    payload = support_payload(packet, "PT-006 status", ProviderDraft.model_validate(raw))
    assert [(item["kind"], item["index"]) for item in payload["items"]] == [
        ("statement", 0),
        ("gap", 0),
        ("next_step", 0),
    ]
    assert all(item["part_id"] == packet.scopes[0].part_id for item in payload["items"])


def test_negative_checker_item_index_is_a_contract_failure():
    from emer.contracts.answer import SupportCheck

    with pytest.raises(ValueError):
        SupportCheck.model_validate(
            {"verdicts": [{"kind": "statement", "index": -1, "supported": True, "reason": "Invalid index"}]}
        )


def test_single_repair_feedback_identifies_all_invalid_patient_items(bundle):
    packet = RetrievalService(bundle).evidence("PT-006 status", mode="full")
    source = next(doc for doc in packet.sources if doc.doc_id == "OPS-302")
    item = {
        "part_id": packet.scopes[0].part_id,
        "text": "PT-006 has a consultation workflow.",
        "citations": [{"doc_id": source.doc_id, "quote": source.text}],
    }
    draft = ProviderDraft.model_validate({"statements": [item], "gaps": [], "next_steps": [item]})
    with pytest.raises(AnswerValidationError) as error:
        validate_draft(packet, draft)
    assert "statements[0]" in str(error.value) and "next_steps[0]" in str(error.value)
    assert str(error.value).count("Missing citations for: PT-006") == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "supported_statement,reported,expected",
    [(True, "answered", "answered"), (False, "partial", "unsupported")],
)
async def test_status_describes_surviving_content_not_rejected_gap_history(
    bundle, supported_statement, reported, expected
):
    packet = RetrievalService(bundle).evidence("What did PT-006 complete?")
    draft = draft_for(packet).model_dump()
    draft["gaps"] = [{"part_id": "part-1", "text": "A requested detail cannot be established."}]
    check = {
        "verdicts": [
            {
                "kind": "statement",
                "index": 0,
                "supported": supported_statement,
                "reason": "Deterministic supplied verdict.",
            },
            {
                "kind": "gap",
                "index": 0,
                "supported": not supported_statement,
                "reason": "Deterministic supplied verdict.",
            },
        ],
        "coverage": [
            {"part_id": "part-1", "status": reported, "reason": "Coverage of only surviving items."}
        ],
    }
    repair = {
        "statement_replacements": []
        if supported_statement
        else [{"index": 0, "replacement": draft["statements"][0]}],
        "gap_replacements": [{"index": 0, "replacement": draft["gaps"][0]}] if supported_statement else [],
        "next_step_replacements": [],
        "additions": {"statements": [], "gaps": [], "next_steps": []},
    }
    provider = FakeProvider([draft, check, repair, check])
    answer = await AnsweringService(provider).answer(packet, "What did PT-006 complete?")
    assert answer.status == expected
    assert answer.diagnostics["withheld_count"] == 1
    assert len(answer.usage) == 4


@pytest.mark.asyncio
async def test_reported_complete_coverage_cannot_override_a_supported_requested_gap(bundle):
    packet = RetrievalService(bundle).evidence("PT-006 status and procedure price")
    draft = draft_for(packet).model_dump()
    draft["gaps"] = [{"part_id": "part-1", "text": "The requested procedure price is missing."}]
    check = {
        "verdicts": [
            {"kind": kind, "index": 0, "supported": True, "reason": "Supplied verdict."}
            for kind in ["statement", "gap"]
        ],
        "coverage": [
            {"part_id": "part-1", "status": "answered", "reason": "Deliberately inconsistent test report."}
        ],
    }
    provider = FakeProvider([draft, check])
    answer = await AnsweringService(provider).answer(packet, "PT-006 status and procedure price")
    assert answer.status == "partial"
    assert provider.operations == ["generation", "support_check"]


def test_cross_record_followup_query_resolves_scope_without_known_patient_branches(bundle):
    packet = RetrievalService(bundle).evidence(
        "As of September 30, 2026, are the two-, six-, and four-week follow-ups overdue?"
    )
    assert packet.scopes[0].all_patients
    assert {source.doc_id for source in packet.sources if source.category == "synthetic_patient"} == {
        source.doc_id for source in bundle.documents if source.category == "synthetic_patient"
    }
    assert packet.scopes[0].as_of == "2026-09-30"


@pytest.mark.parametrize("patient", ["PT-001", "PT-008"])
def test_followup_aggregation_preserves_explicit_patient_and_publication_is_not_an_event_filter(
    bundle, patient
):
    packet = RetrievalService(bundle).evidence(f"Are follow-ups overdue for {patient} on August 15, 2026?")
    assert packet.scopes[0].patient_ids == [patient] and not packet.scopes[0].all_patients
    assert [source.doc_id for source in packet.sources if source.category == "synthetic_patient"] == [patient]
    assert packet.scopes[0].as_of == "2026-08-15"
    assert any("publication dates" in warning for warning in packet.scopes[0].warnings)


def test_pre_policy_date_exposes_provenance_windows_without_authorizing_future_terms(bundle):
    scope = RetrievalService(bundle).evidence("Which cancellation policy applied December 1, 2025?").scopes[0]
    assert scope.applicable_policy_ids == [] and scope.superseded_policy_ids == []
    assert {window.relation for window in scope.policy_windows} == {"not_yet_effective"}
    assert {window.valid_from for window in scope.policy_windows} == {"2026-01-01", "2026-07-01"}
    assert all(window.doc_id not in scope.allowed_source_ids for window in scope.policy_windows)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "finish_reason,expected_code", [("stop", "provider_schema_invalid"), ("length", "provider_incomplete")]
)
async def test_failed_provider_response_preserves_bounded_diagnostics_without_reasoning(
    finish_reason, expected_code
):
    calls = 0
    content = json.dumps({"statements": [], "gaps": [], "next_steps": [], "extra": "invalid"})

    async def handle(request):
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            json={
                "id": "failed-example",
                "usage": {"total_tokens": 100, "cost": 0.001},
                "choices": [
                    {
                        "finish_reason": finish_reason,
                        "message": {
                            "content": content,
                            "reasoning": "do not persist this field",
                            "reasoning_details": [{"text": "private reasoning"}],
                        },
                    }
                ],
                "headers": {"Authorization": "must not persist"},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(ProviderError) as raised:
            await OpenRouterClient("test-only-key", "model", client=client).structured(
                "", {}, ProviderDraft, max_tokens=4500
            )
    error = raised.value
    assert error.code == expected_code and calls == 1
    assert error.usage.total_tokens == 100 and error.usage.cost == 0.001
    diagnostic = error.response_diagnostics
    assert diagnostic["content"] == content
    assert diagnostic["finish_reason"] == finish_reason
    assert diagnostic["max_completion_tokens"] == 4500
    if finish_reason == "stop":
        assert diagnostic["schema_errors"][0]["loc"] == ["extra"]
        assert "input" not in diagnostic["schema_errors"][0]
    serialized = json.dumps(diagnostic)
    assert "do not persist" not in serialized and "private reasoning" not in serialized
    assert "Authorization" not in serialized and "test-only-key" not in serialized
