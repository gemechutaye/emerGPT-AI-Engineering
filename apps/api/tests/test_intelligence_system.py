"""Independent intelligence seam checks: real source/index/Postgres/ASGI, declared provider doubles.

These prove wiring and failure boundaries, not live model semantic correctness. Every database,
cache and provider response belongs to this file; the serving application is never restarted.
"""

import json
from datetime import timedelta
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import psycopg
import pytest
from emer.api import app as api
from emer.api import dependencies
from emer.contracts.answer import ProviderUsage
from emer.providers.openrouter import OpenRouterClient, StructuredResult
from emer.services import conversation_memory, conversation_titles, provider_activity, runs
from emer.services.ingestion import ingest, with_embeddings
from emer.services.provider_activity import TrackedOpenRouterClient
from emer.services.text_intent import resolve_text_intent
from emer.settings import settings
from emer.storage import bundles
from emer.storage.models import (
    ActiveIndex,
    Base,
    BrowserSession,
    Conversation,
    CorpusIndex,
    ProviderAttempt,
    Run,
    utcnow,
)
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
async def intelligence_db(monkeypatch, tmp_path):
    database = "emer_intelligence_test_" + uuid4().hex[:12]
    with psycopg.connect("postgresql://localhost:55432/postgres", autocommit=True) as admin:
        admin.execute(f"CREATE DATABASE {database}")
    engine = create_async_engine(f"postgresql+psycopg://localhost:55432/{database}")
    session = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    for module in (api, dependencies, bundles, runs, provider_activity, conversation_titles):
        monkeypatch.setattr(module, "Session", session)
    monkeypatch.setattr(settings, "shared_workspace", False)
    monkeypatch.setattr(settings, "app_origin", "http://intelligence.test")
    monkeypatch.setattr(settings, "cache_dir", str(tmp_path / "cache"))
    monkeypatch.setattr(settings, "openrouter_api_key", "")
    monkeypatch.setattr(settings, "openai_api_key", "")
    monkeypatch.setattr(settings, "retrieval_mode", "lexical")
    monkeypatch.setattr(settings, "retrieval_top_k", 3)
    monkeypatch.setattr(bundles, "_cache", {})
    monkeypatch.setattr(runs, "dispatch", lambda run_id: None)

    async def no_metadata(*args, **kwargs):
        pass

    monkeypatch.setattr(conversation_memory, "schedule_auto", no_metadata)
    monkeypatch.setattr(conversation_titles, "queue_title", no_metadata)
    bundle = await bundles.store_and_activate(ingest(ROOT / "config/corpus.json"))
    async with session.begin() as db:
        owner = BrowserSession(
            token_hash=dependencies.token_hash("intelligence-owner"), expires_at=utcnow() + timedelta(hours=1)
        )
        db.add(owner)
        await db.flush()
        conversation = Conversation(session_id=owner.id)
        db.add(conversation)
        await db.flush()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api.app, raise_app_exceptions=False),
        base_url="http://intelligence.test",
        headers={"Origin": "http://intelligence.test"},
        cookies={dependencies.COOKIE: "intelligence-owner"},
    ) as client:
        try:
            yield SimpleNamespace(
                session=session,
                owner=owner,
                conversation=conversation,
                bundle=bundle,
                client=client,
                cache=tmp_path / "cache",
            )
        finally:
            await provider_activity.drain_provider_receipts()
    await engine.dispose()
    with psycopg.connect("postgresql://localhost:55432/postgres", autocommit=True) as admin:
        admin.execute(f"DROP DATABASE {database} WITH (FORCE)")


async def test_every_original_record_and_metadata_resolves_through_http(intelligence_db):
    fixture = intelligence_db
    original = [
        json.loads(line)
        for line in (ROOT / "EMER_AI_TakeHome_Knowledge_Corpus.jsonl").read_text().splitlines()
    ]
    corpus = (await fixture.client.get("/api/v1/corpus")).json()
    assert corpus["count"] == len(original) == 35
    assert {doc["doc_id"] for doc in corpus["sources"]} == {doc["doc_id"] for doc in original}
    for record in original:
        response = await fixture.client.get(
            f"/api/v1/sources/{record['doc_id']}", params={"index_id": fixture.bundle.id}
        )
        assert response.status_code == 200
        source = response.json()
        assert {field: source[field] for field in record} == record
        assert source["sha256"] == sha256(record["text"].encode()).hexdigest()
        assert source["index_id"] == fixture.bundle.id


async def test_unknown_pinned_index_is_a_structured_not_found(intelligence_db):
    response = await intelligence_db.client.get(
        "/api/v1/sources/PT-001", params={"index_id": "missing-index"}
    )
    assert response.status_code == 404, response.text
    assert response.json()["detail"]["code"] == "NOT_FOUND"


@pytest.mark.parametrize("path", ["/api/v1/sources/PT-004", "/api/v1/corpus", "/api/v1/health/ready"])
async def test_absent_active_index_is_structured_unavailable(intelligence_db, path):
    # This fixture owns an isolated test database; preserve every stored source bundle.
    async with intelligence_db.session.begin() as db:
        await db.execute(delete(ActiveIndex))
    bundles._cache.clear()
    response = await intelligence_db.client.get(path)
    assert response.status_code == 503, response.text
    assert response.json()["error"]["code"] == "CORPUS_UNAVAILABLE"
    assert response.json()["error"]["retryable"] is True
    # Missing activation does not make the historical source index unavailable.
    historical = await intelligence_db.client.get(
        "/api/v1/sources/PT-004", params={"index_id": intelligence_db.bundle.id}
    )
    assert historical.status_code == 200
    assert historical.json()["index_id"] == intelligence_db.bundle.id


async def test_corrupt_durable_bundle_has_explicit_unavailable_readiness(intelligence_db):
    async with intelligence_db.session.begin() as db:
        index = await db.get(CorpusIndex, intelligence_db.bundle.id)
        index.bundle = b"corrupted durable bundle"
    bundles._cache.clear()
    response = await intelligence_db.client.get("/api/v1/health/ready")
    assert response.status_code == 503, response.text
    assert response.json()["error"]["code"] == "CORPUS_UNAVAILABLE"


async def test_cold_cache_rebuild_retains_historical_index_and_original_sources(intelligence_db):
    fixture = intelligence_db
    # Synthetic vectors are used only to create a second immutable index identity, never recall evidence.
    second = with_embeddings(
        fixture.bundle,
        "declared-fixture-space",
        {chunk.chunk_id: [float(i + 1), 1.0] for i, chunk in enumerate(fixture.bundle.chunks)},
    )
    await bundles.store_and_activate(second)
    bundles._cache.clear()  # A fresh process has no memory cache; the local cache is initially absent.
    assert not fixture.cache.exists()
    current = await bundles.load_bundle()
    old = await bundles.load_bundle(fixture.bundle.id)
    assert current.id == second.id and old.id == fixture.bundle.id
    assert old.documents == current.documents == fixture.bundle.documents
    assert (fixture.cache / f"{old.checksum}.sqlite").read_bytes() == old.data
    assert (fixture.cache / f"{current.checksum}.sqlite").read_bytes() == current.data
    # A damaged disposable cache cannot replace the durable bytes on another cold load.
    (fixture.cache / f"{old.checksum}.sqlite").write_bytes(b"corrupt disposable cache")
    bundles._cache.clear()
    restored = await bundles.load_bundle(old.id)
    assert restored.data == fixture.bundle.data
    assert (fixture.cache / f"{old.checksum}.sqlite").read_bytes() == restored.data
    response = await fixture.client.get("/api/v1/sources/PT-001", params={"index_id": old.id})
    assert response.status_code == 200 and response.json()["index_id"] == old.id


async def test_http_question_retrieval_provider_citation_history_and_receipts(intelligence_db, monkeypatch):
    fixture = intelligence_db
    payloads = []

    async def declared_provider(self, system, payload, schema, operation="generation", max_tokens=4500):
        payloads.append((operation, payload))
        if operation == "query_planning":
            value = {"parts": [{"scope_id": payload["scopes"][0]["part_id"], "query": payload["question"],
                                "requested_information": ["completed and pending consultation work"]}]}
        elif operation == "evidence_assessment":
            value = {"parts": [{"part_id": "part-1", "status": "sufficient", "missing_requests": [],
                               "search_query": None, "facts": [{"fact_id": "work-status",
                               "text": "PT-007 has completed photography and measurements; provider assessment is pending.",
                               "citations": [{"doc_id": "PT-007", "quote": "Current status: Photography and measurements completed. Provider assessment pending."}]}]}],
                     "unassigned_requests": []}
        elif operation in {"generation", "repair"}:
            evidence = payload["evidence"]
            record = next(doc for doc in evidence["sources"] if doc["doc_id"] == "PT-007")
            quote = "Current status: Photography and measurements completed. Provider assessment pending."
            assert "text" not in record
            assert any(quote in part["text"] for part in evidence["passages"] if part["doc_id"] == "PT-007")
            value = {
                "statements": [
                    {
                        "part_id": evidence["scopes"][0]["part_id"],
                        "text": "PT-007 has completed photography and measurements; the provider assessment is pending.",
                        "citations": [{"doc_id": "PT-007", "quote": quote}],
                    }
                ],
                "gaps": [],
                "next_steps": [],
            }
        else:
            assert operation == "support_check"
            value = {
                "source_review": [{"unit_id": unit["unit_id"], "disposition": "represented", "reason": "Declared fixture."} for unit in payload["source_audit_units"]],
                "fact_coverage": [{"fact_id": "work-status", "status": "covered", "statement_indices": [0],
                                   "next_step_indices": [], "reason": "Declared source fixture."}],
                "verdicts": [
                    {
                        "kind": "statement",
                        "index": 0,
                        "supported": True,
                        "reason": "Declared transport fixture, not a model judgment.",
                        "citation_replacements": payload["items"][0]["citations"],
                    }
                ],
                "coverage": [
                    {
                        "part_id": payload["evidence"]["scopes"][0]["part_id"],
                        "status": "answered",
                        "reason": "Declared transport fixture.",
                    }
                ],
            }
        return StructuredResult(
            schema.model_validate(value),
            ProviderUsage(operation=operation, model="declared-provider-double", latency_ms=1, cost=None),
        )

    monkeypatch.setattr(OpenRouterClient, "structured", declared_provider)
    body = {
        "question": "What is complete and pending in PT-007's consultation?",
        "context_version": 1,
        "idempotency_key": "intelligence-whole-path",
        "automatic_context": True,
    }
    submitted = await fixture.client.post(f"/api/v1/conversations/{fixture.conversation.id}/runs", json=body)
    assert submitted.status_code == 202, submitted.text
    run_id = submitted.json()["id"]
    await runs.execute(run_id)
    snapshot = (await fixture.client.get(f"/api/v1/runs/{run_id}")).json()
    assert snapshot["status"] == "completed", snapshot
    assert [operation for operation, _ in payloads] == ["query_planning", "evidence_assessment", "generation", "support_check"]
    packet = payloads[2][1]["evidence"]
    assert "retrieval" not in packet
    patients = {source["doc_id"] for source in packet["sources"] if source["category"] == "synthetic_patient"}
    assert patients == {"PT-007"}
    async with fixture.session() as db:
        persisted = await db.get(Run, run_id)
    from emer.contracts.answer import EvidencePacket
    from emer.services.answering import model_context
    assert model_context(EvidencePacket.model_validate(persisted.evidence), body["question"])["evidence"] == packet
    assert persisted.evidence["retrieval"]["mode"] == "lexical"
    assert snapshot["answer"]["diagnostics"]["retrieval"] == persisted.evidence["retrieval"]
    citation = snapshot["answer"]["statements"][0]["citations"][0]
    source = (
        await fixture.client.get(
            f"/api/v1/sources/{citation['doc_id']}", params={"index_id": citation["index_id"]}
        )
    ).json()
    assert source["text"][citation["start"] : citation["end"]] == citation["quote"]
    assert source["sha256"] == citation["source_sha256"]
    assert source["version"] == citation["version"] and source["title"] == citation["title"]
    assert [event["sequence"] for event in snapshot["events"]] == [1, 2, 3, 4]
    assert [event["type"] for event in snapshot["events"]] == [
        "queued",
        "retrieving",
        "generating",
        "completed",
    ]
    assert [attempt["operation"] for attempt in snapshot["provider_attempts"]] == [
        "query_planning", "evidence_assessment",
        "generation",
        "support_check",
    ]
    assert all(attempt["status"] == "completed" for attempt in snapshot["provider_attempts"])
    assert snapshot["metrics"]["total_ms"] > 0
    history = (await fixture.client.get(f"/api/v1/conversations/{fixture.conversation.id}")).json()
    assert history["runs"][0]["answer"] == snapshot["answer"]
    duplicate = await fixture.client.post(f"/api/v1/conversations/{fixture.conversation.id}/runs", json=body)
    assert duplicate.json()["id"] == run_id and len(payloads) == 4


async def test_missing_generation_provider_preserves_real_evidence_and_honest_failure(intelligence_db):
    fixture = intelligence_db
    response = await fixture.client.post(
        f"/api/v1/conversations/{fixture.conversation.id}/runs",
        json={
            "question": "What is the status of PT-005?",
            "context_version": 1,
            "idempotency_key": "intelligence-provider-failure",
            "automatic_context": True,
        },
    )
    assert response.status_code == 202
    run_id = response.json()["id"]
    await runs.execute(run_id)
    result = (await fixture.client.get(f"/api/v1/runs/{run_id}")).json()
    assert result["status"] == "failed" and result["answer"] is None
    assert result["error"]["code"] == "PROVIDER_NOT_CONFIGURED"
    async with fixture.session() as db:
        persisted = await db.get(Run, run_id)
    assert persisted.evidence is None  # Planner failed before retrieval; no pretend evidence packet.
    assert result["provider_attempts"][0]["operation"] == "query_planning"
    assert len(result["provider_attempts"]) == 1
    assert result["provider_attempts"][0]["status"] == "failed"
    assert result["provider_attempts"][0]["usage"] is None


async def test_embedding_receipt_names_the_embedding_model(intelligence_db):
    fixture = intelligence_db
    embedding_model = "openai/text-embedding-3-large"

    def response(request):
        assert json.loads(request.content)["model"] == embedding_model
        return httpx.Response(
            200,
            json={
                "model": embedding_model,
                "data": [{"index": 0, "embedding": [1.0, 2.0]}],
                "usage": {"prompt_tokens": 4, "total_tokens": 4},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as transport:
        client = TrackedOpenRouterClient(
            "fixture-key", "openai/gpt-5.6-luna", fixture.owner.id, client=transport
        )
        await client.embed(["A declared query embedding fixture"], embedding_model)
    async with fixture.session() as db:
        attempt = await db.scalar(select(ProviderAttempt))
    assert attempt.operation == "query_embedding"
    assert attempt.model == embedding_model
    assert attempt.usage["model"] == embedding_model


async def test_self_contained_topic_change_cannot_acquire_a_previous_patient():
    class StaleIntentDouble:
        async def structured(self, *args, **kwargs):
            return SimpleNamespace(
                value={"question": "What are office hours for PT-006?", "clarification": None}
            )

    result = await resolve_text_intent(
        StaleIntentDouble(), "What are office hours?", ["What is the status of PT-006?"], "PT-006", None
    )
    assert result.question == "What are office hours?" and result.clarification is None


async def test_automatic_new_topic_does_not_store_the_previous_patient(intelligence_db):
    fixture = intelligence_db
    async with fixture.session.begin() as db:
        previous = Run(
            session_id=fixture.owner.id,
            conversation_id=fixture.conversation.id,
            index_id=fixture.bundle.id,
            idempotency_key="prior-patient",
            body_hash="prior-hash",
            question="What is the status of PT-006?",
            context_version=1,
            context={},
            status="completed",
            answer={"status": "answered"},
        )
        db.add(previous)
    response = await fixture.client.post(
        f"/api/v1/conversations/{fixture.conversation.id}/runs",
        json={
            "question": "How does audit logging work?",
            "context_version": 1,
            "idempotency_key": "new-independent-topic",
            "automatic_context": True,
        },
    )
    assert response.status_code == 202, response.text
    async with fixture.session() as db:
        current = await db.get(Run, response.json()["id"])
    assert current.context["patient_id"] is None


async def test_evaluation_history_is_not_presented_as_current_runtime(intelligence_db):
    response = await intelligence_db.client.get("/api/v1/evaluations")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "historical_evidence"
    assert body["runtime"]["retrieval_mode_requested"] == "lexical"
    assert "historical_report" in body and "current_selection" not in body


async def test_answer_event_stream_rotates_and_reconnects_without_duplicate_admission(intelligence_db, monkeypatch):
    from unittest.mock import AsyncMock

    from emer.storage.models import RunEvent

    fixture = intelligence_db
    body = {"question": "What is PT-001's status?", "context_version": 1,
            "idempotency_key": "stream-rotation", "automatic_context": True}
    endpoint = f"/api/v1/conversations/{fixture.conversation.id}/runs"
    admitted = (await fixture.client.post(endpoint, json=body)).json()
    run_id = admitted["id"]
    ticks = []

    async def virtual_tick(seconds):
        ticks.append(seconds)

    monkeypatch.setattr(api.asyncio, "sleep", virtual_tick)
    request = SimpleNamespace(headers={}, is_disconnected=AsyncMock(return_value=False))
    stream = await api.events(run_id, request, owner=fixture.owner)
    chunks = [chunk async for chunk in stream.body_iterator]
    assert ticks == [1] * 60
    assert "".join(chunks).count("id: 1\n") == 1
    assert (await fixture.client.post(endpoint, json=body)).json()["id"] == run_id
    async with fixture.session.begin() as db:
        run = await db.get(Run, run_id)
        run.status = "completed"
        db.add(RunEvent(run_id=run_id, sequence=2, type="completed", data={}))
    request.headers = {"last-event-id": "1"}
    stream = await api.events(run_id, request, owner=fixture.owner)
    resumed = "".join([chunk async for chunk in stream.body_iterator])
    assert "id: 2\n" in resumed and "id: 1\n" not in resumed


@pytest.mark.parametrize(
    "followup, expected_patient",
    [
        ("Are outside records definitely available?", "PT-005"),
        ("Then write an internal handoff about whether an injectable plan is finalized.", "PT-005"),
        ("What are the two consultation prices?", None),
        ("Separate issue: what are the office hours?", None),
    ],
)
async def test_http_admission_retains_record_topic_and_clears_general_topics(
    intelligence_db,
    followup,
    expected_patient,
):
    from emer.contracts.answer import ProviderDraft, PublishedAnswer
    from emer.services.answering import validate_draft
    from emer.services.retrieval import RetrievalService

    fixture = intelligence_db
    question = "What is holding up PT-005’s consultation?"
    created = await fixture.client.post(
        f"/api/v1/conversations/{fixture.conversation.id}/runs",
        json={
            "question": question,
            "context_version": 1,
            "automatic_context": True,
            "idempotency_key": "record-topic-seed",
        },
    )
    assert created.status_code == 202
    packet = RetrievalService(fixture.bundle).evidence(question, mode="lexical", top_k=8)
    doc = next(d for d in packet.sources if d.doc_id == "PT-005")
    proposed = ProviderDraft.model_validate(
        {
            "statements": [
                {
                    "part_id": packet.scopes[0].part_id,
                    "text": "PT-005 has uncertain prior filler history and an incomplete consultation.",
                    "citations": [{"doc_id": doc.doc_id, "quote": doc.text}],
                }
            ],
            "gaps": [],
            "next_steps": [],
        }
    )
    statements, _ = validate_draft(packet, proposed)
    answer = PublishedAnswer(
        status="answered",
        statements=statements,
        gaps=[],
        next_steps=[],
        scopes=packet.scopes,
        index_id=packet.index_id,
        index_checksum=packet.index_checksum,
        usage=[],
        diagnostics={"fixture": "Declared saved source-grounded turn; no provider call"},
    )
    async with fixture.session.begin() as db:
        previous = await db.get(Run, created.json()["id"])
        previous.status = "completed"
        previous.answer, previous.evidence = answer.model_dump(), packet.model_dump()
        previous.context = {**previous.context, "resolved_question": question}
        previous.completed_at = utcnow()
    response = await fixture.client.post(
        f"/api/v1/conversations/{fixture.conversation.id}/runs",
        json={
            "question": followup,
            "context_version": 1,
            "automatic_context": True,
            "idempotency_key": "record-topic-followup",
        },
    )
    assert response.status_code == 202, response.text
    async with fixture.session() as db:
        stored = await db.get(Run, response.json()["id"])
    assert stored.context["patient_id"] is None
    assert stored.context["dialogue_state"]["turns"][-1]["assistant_references"]
    from emer.contracts.answer import ProviderUsage
    from emer.domain.dialogue_state import DialogueState
    from emer.domain.scope import patient_ids
    from emer.providers.openrouter import StructuredResult
    class ReferenceProvider:
        async def structured(self, instructions, payload, schema, **kwargs):
            ref = DialogueState.model_validate(payload["dialogue_state"]).turns[-1].assistant_references[0]
            mention = "outside records" if "outside records" in followup else "an injectable plan"
            return StructuredResult(schema(status="resolved", base_reference_id=None,
                bindings=[{"mention": mention, "target": "PT-005", "reference_id": ref.reference_id, "mode": "qualify"}]),
                ProviderUsage(operation="text_intent_resolution", model="declared", latency_ms=0))
    resolved = await resolve_text_intent(ReferenceProvider(), followup, [], None, None,
                                         dialogue_state=stored.context["dialogue_state"])
    assert set(patient_ids(resolved.question)) == ({expected_patient} if expected_patient else set())
    assert stored.context["previous_questions"] == [question]


@pytest.mark.parametrize("terminal", ["completed", "failed", "cancelled"])
async def test_terminal_metrics_include_intent_and_unknown_attempts_from_durable_ledger(
    intelligence_db, terminal
):
    fixture = intelligence_db
    async with fixture.session.begin() as db:
        run = Run(
            session_id=fixture.owner.id,
            conversation_id=fixture.conversation.id,
            index_id=fixture.bundle.id,
            idempotency_key="cost-ledger",
            body_hash="test",
            question="What about that record?",
            context_version=1,
            status="running",
            owner_id=runs.OWNER,
            fence=1,
            lease_expires_at=utcnow() + timedelta(minutes=1),
        )
        db.add(run)
        await db.flush()
        for number, operation in enumerate(["text_intent_resolution", "query_embedding", "generation"], 1):
            db.add(
                ProviderAttempt(
                    session_id=fixture.owner.id,
                    run_id=run.id,
                    operation=operation,
                    model="declared-receipt-fixture",
                    status="completed",
                    usage=ProviderUsage(
                        operation=operation,
                        model="declared-receipt-fixture",
                        request_id=f"receipt-{number}",
                        cost=number / 1000,
                        latency_ms=number,
                    ).model_dump(),
                )
            )
        db.add(
            ProviderAttempt(
                session_id=fixture.owner.id,
                run_id=run.id,
                operation="support_check",
                model="declared-receipt-fixture",
                status="uncertain",
                usage=None,
            )
        )
    await runs.finish(run.id, 1, terminal, metrics={"total_ms": 20, "usage": [{"cost": 999}]})
    async with fixture.session() as db:
        saved = await db.get(Run, run.id)
        assert saved.status == terminal
        assert saved.metrics["usage_source"] == "provider_attempt_ledger"
        assert {u["request_id"] for u in saved.metrics["usage"]} == {"receipt-1", "receipt-2", "receipt-3"}
        assert sum(u["cost"] for u in saved.metrics["usage"]) == pytest.approx(0.006)
        assert saved.metrics["provider_attempt_count"] == 4
        assert saved.metrics["unreceipted_provider_attempts"] == 1
        assert saved.metrics["total_ms"] == 20
