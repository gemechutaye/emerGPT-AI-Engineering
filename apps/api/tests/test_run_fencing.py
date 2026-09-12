"""Behavioral concurrency checks with real isolated Postgres and no paid provider calls."""

import asyncio
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import psycopg
import pytest
from emer.contracts.http import RunCreate
from emer.providers.openrouter import ProviderError
from emer.services import answering, conversation_memory, intelligence, retrieval, runs
from emer.services.ingestion import ingest
from emer.storage.models import Base, BrowserSession, Conversation, CorpusIndex, Run, RunEvent, utcnow
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from test_discovery_context import discovery as isolated_discovery

source_discovery = isolated_discovery


@pytest.fixture
async def run_db(monkeypatch):
    database = "emer_fence_test_" + uuid4().hex[:12]
    dsn = f"postgresql://localhost:55432/{database}"
    with psycopg.connect("postgresql://localhost:55432/postgres", autocommit=True) as admin:
        admin.execute(f"CREATE DATABASE {database}")
    engine = create_async_engine(f"postgresql+psycopg://localhost:55432/{database}")
    session = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    monkeypatch.setattr(runs, "Session", session)
    monkeypatch.setattr(runs, "dispatch", lambda run_id: None)

    async def no_metadata(*args, **kwargs):
        pass

    monkeypatch.setattr(conversation_memory, "schedule_auto", no_metadata)
    calls = []
    documents = ingest(Path(__file__).resolve().parents[3] / "config/corpus.json").documents

    async def bundle(index_id=None):
        # Admission now resolves topic follow-ups against real source content. Preserve
        # the declared fencing-test index while honoring the Bundle.documents contract.
        return SimpleNamespace(id="test-index", config={"embedding": None}, documents=documents)

    class Retrieval:
        def __init__(self, bundle):
            pass

        def evidence(self, question, **context):
            return SimpleNamespace(sources=[], retrieval={"mode": "lexical"}, model_dump=lambda **kw: {})

    class Answering:
        def __init__(self, provider):
            pass

        async def answer(self, packet, question):
            calls.append(question)
            return SimpleNamespace(usage=[], model_dump=lambda **kw: {"fixture": "declared provider double"})

    class Intelligence:
        """Declared stage double keeps ownership tests independent of paid model work."""
        def __init__(self, provider, verifier, settings):
            self.provider, self.packet = provider, None

        async def answer(self, bundle, question, *, on_evidence, **context):
            self.packet = await asyncio.to_thread(retrieval.RetrievalService(bundle).evidence, question, **context)
            await on_evidence(self.packet)
            answer = await answering.AnsweringService(self.provider).answer(self.packet, question)
            return answer, self.packet

    monkeypatch.setattr(intelligence, "IntelligenceService", Intelligence)
    monkeypatch.setattr(runs, "load_bundle", bundle)
    monkeypatch.setattr(retrieval, "RetrievalService", Retrieval)
    monkeypatch.setattr(answering, "AnsweringService", Answering)
    async with session.begin() as db:
        owner = BrowserSession(token_hash=uuid4().hex, expires_at=utcnow() + timedelta(hours=1))
        db.add(owner)
        db.add(CorpusIndex(id="test-index", checksum="test-checksum", corpus_checksum="corpus-test",
                           bundle=b"fixture-only", manifest={}))
        await db.flush()
        conversation = Conversation(session_id=owner.id)
        db.add(conversation)
        await db.flush()
    try:
        yield SimpleNamespace(session=session, owner=owner, conversation=conversation, calls=calls, dsn=dsn)
    finally:
        await engine.dispose()
        with psycopg.connect("postgresql://localhost:55432/postgres", autocommit=True) as admin:
            admin.execute(f"DROP DATABASE {database} WITH (FORCE)")


def request(key="stable-run-key"):
    return RunCreate(question="A test question", context_version=1, idempotency_key=key)


async def create(fixture, body=None):
    return await runs.create_run(fixture.owner.id, fixture.conversation.id, body or request())


async def running(fixture, *, expired=False):
    row = await create(fixture)
    async with fixture.session.begin() as db:
        run = await db.get(Run, row.id)
        run.status, run.owner_id, run.fence = "running", runs.OWNER, 7
        run.lease_expires_at = utcnow() + timedelta(seconds=-1 if expired else 30)
    return row


async def test_concurrent_same_key_reserves_exactly_one_run_and_one_queued_event(run_db):
    a, b = await asyncio.gather(create(run_db), create(run_db))
    assert a.id == b.id
    async with run_db.session() as db:
        assert await db.scalar(select(func.count()).select_from(Run)) == 1
        assert await db.scalar(select(func.count()).select_from(RunEvent)) == 1
    # An idempotent retry remains retrievable despite the active-conversation admission gate.
    assert (await create(run_db)).id == a.id


async def test_distinct_concurrent_requests_cannot_both_enter_one_conversation(run_db):
    results = await asyncio.gather(create(run_db, request("request-one")), create(run_db, request("request-two")),
                                   return_exceptions=True)
    assert sum(isinstance(value, Run) for value in results) == 1
    error = next(value for value in results if isinstance(value, HTTPException))
    assert error.status_code == 409


@pytest.mark.parametrize("changed_context", [False, True])
async def test_cited_discovery_is_inherited_only_within_its_context_version(run_db, changed_context, source_discovery):
    # A current source-grounded fixture keeps its answer/citation/index identity together.
    # Archived provider answers cannot be paired with a rebuilt index and called verified.
    answer, packet = source_discovery
    first = await create(run_db)
    async with run_db.session.begin() as db:
        previous = await db.get(Run, first.id)
        previous.question, previous.status = packet.scopes[0].question, "completed"
        previous.answer, previous.evidence = answer.model_dump(mode="json"), packet.model_dump(mode="json")
        if changed_context:
            conversation = await db.get(Conversation, run_db.conversation.id)
            conversation.context_version = 2
    followup = await create(run_db, RunCreate(
        question="What is their follow-up?", context_version=2 if changed_context else 1,
        idempotency_key="discovery-followup",
    ))
    assert followup.context["patient_id"] is None  # Identity is resolved from provenance-bound references later.
    refs = [ref for turn in followup.context["dialogue_state"]["turns"] for ref in turn["assistant_references"]]
    assert bool(refs) is (not changed_context)
    if refs:
        assert refs[0]["patient_ids"] == ["PT-003"]


@pytest.mark.parametrize("old_question,new_question", [
    ("Which patient reports sun exposure?", "What about patient one million?"),
    ("What is patient one million's status?", "What is their follow-up?"),
])
async def test_unsupported_spoken_number_does_not_fail_run_admission(run_db, old_question, new_question):
    from emer.services.text_intent import resolve_text_intent

    first = await create(run_db)
    async with run_db.session.begin() as db:
        previous = await db.get(Run, first.id)
        previous.question, previous.status, previous.answer = old_question, "completed", {"status": "answered"}
    followup = await create(run_db, RunCreate(
        question=new_question, context_version=1, idempotency_key="unsupported-number",
    ))
    assert followup.status == "queued"
    # A missing provider object proves clarification happens before any paid call.
    intent = await resolve_text_intent(None, new_question, followup.context["previous_questions"], None, None)
    assert intent.clarification and not intent.question


async def test_expired_or_superseded_controller_cannot_publish_and_terminal_is_once(run_db):
    row = await running(run_db, expired=True)
    await runs.finish(row.id, 7, "completed", answer={"should": "never publish"})
    async with run_db.session() as db:
        assert (await db.get(Run, row.id)).answer is None
    async with run_db.session.begin() as db:
        current = await db.get(Run, row.id)
        current.fence, current.lease_expires_at = 8, utcnow() + timedelta(seconds=30)
    await runs.finish(row.id, 7, "completed", answer={"stale": True})
    await asyncio.gather(runs.finish(row.id, 8, "completed", answer={"winner": True}),
                         runs.finish(row.id, 8, "failed", error={"late": True}))
    async with run_db.session() as db:
        result = await db.get(Run, row.id)
        assert result.status in {"completed", "failed"}
        assert result.answer != {"stale": True}
        terminal = list(await db.scalars(select(RunEvent).where(RunEvent.type.in_(runs.TERMINAL))))
        assert len(terminal) == 1


async def test_monitor_does_not_resurrect_expired_lease(run_db):
    row = await running(run_db, expired=True)
    parent = SimpleNamespace(cancelled=False)
    parent.cancel = lambda: setattr(parent, "cancelled", True)
    task = asyncio.create_task(runs.monitor(row.id, 7, parent))
    try:
        await asyncio.sleep(2.15)
        async with run_db.session() as db:
            current = await db.get(Run, row.id)
            assert current.lease_expires_at < utcnow(), "An expired execution lease must not be renewed."
        assert parent.cancelled
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("state", ["revoked", "expired"])
async def test_admission_rechecks_owner_after_dependency_resolution(run_db, state):
    async with run_db.session.begin() as db:
        browser = await db.get(BrowserSession, run_db.owner.id)
        if state == "revoked":
            browser.revoked = True
        else:
            browser.expires_at = utcnow() - timedelta(seconds=1)
    with pytest.raises(HTTPException) as rejected:
        await create(run_db)
    assert rejected.value.status_code == 401
    async with run_db.session() as db:
        assert await db.scalar(select(func.count()).select_from(Run)) == 0


@pytest.mark.parametrize("state", ["revoked", "expired"])
async def test_queued_work_for_inactive_owner_cannot_start_generation(run_db, state):
    row = await create(run_db)
    async with run_db.session.begin() as db:
        browser = await db.get(BrowserSession, run_db.owner.id)
        if state == "revoked":
            browser.revoked = True
        else:
            browser.expires_at = utcnow() - timedelta(seconds=1)
    await runs.execute(row.id)
    assert run_db.calls == [], "Owner expiry must be checked before starting a paid generation request."


async def test_lease_expiry_during_retrieval_blocks_new_provider_call(run_db, monkeypatch):
    row = await create(run_db)

    class SlowRetrieval:
        def __init__(self, bundle):
            pass

        def evidence(self, question, **context):
            # Represents time passing / a stalled worker before external generation starts.
            with psycopg.connect(run_db.dsn) as db:
                db.execute("UPDATE runs SET lease_expires_at=NOW()-INTERVAL '1 second' WHERE id=%s", (row.id,))
            return SimpleNamespace(sources=[], model_dump=lambda **kw: {})

    monkeypatch.setattr(retrieval, "RetrievalService", SlowRetrieval)
    await runs.execute(row.id)
    assert run_db.calls == [], "A stalled owner must not begin new provider work after its lease expired."
    await runs.recover()
    async with run_db.session() as db:
        assert (await db.get(Run, row.id)).status in runs.TERMINAL


async def test_context_change_before_commit_prevents_old_answer_publication(run_db):
    row = await running(run_db)
    async with run_db.session.begin() as db:
        conversation = await db.get(Conversation, run_db.conversation.id)
        conversation.context_version += 1
    await runs.finish(row.id, 7, "completed", answer={"outdated": True})
    async with run_db.session() as db:
        current = await db.get(Run, row.id)
        assert current.status == "superseded" and current.answer is None


async def test_checker_failure_with_dictionary_usage_publishes_failed_terminal(run_db, monkeypatch):
    receipt = {"operation": "generation", "model": "fixture-model", "provider": "fixture",
               "request_id": "fixture-receipt", "prompt_tokens": 7, "completion_tokens": 3,
               "total_tokens": 10, "cost": 0.002, "latency_ms": 12}

    class FailedChecker:
        def __init__(self, provider):
            pass

        async def answer(self, packet, question):
            error = ProviderError("fixture_checker_failed", "Declared test failure")
            error.attempt_usage = [receipt]
            raise error

    monkeypatch.setattr(answering, "AnsweringService", FailedChecker)
    monkeypatch.setattr(runs.settings, "openrouter_api_key", "fixture-no-network")
    row = await create(run_db)
    await runs.execute(row.id)
    async with run_db.session() as db:
        current = await db.get(Run, row.id)
        assert current.status == "failed" and current.answer is None
        assert current.error["code"] == "fixture_checker_failed"
        assert current.metrics["usage"] == [receipt]
        terminal = list(await db.scalars(select(RunEvent).where(RunEvent.type.in_(runs.TERMINAL))))
        assert [event.type for event in terminal] == ["failed"]


@pytest.mark.parametrize("automatic", [True, False])
async def test_automatic_typed_question_ignores_hidden_legacy_context(run_db, automatic):
    async with run_db.session.begin() as db:
        conversation = await db.get(Conversation, run_db.conversation.id)
        conversation.patient_id, conversation.as_of = "PT-005", "2026-03-01"
    row = await create(run_db, request().model_copy(update={"automatic_context": automatic}))
    assert row.context["patient_id"] == (None if automatic else "PT-005")
    assert row.context["as_of"] == (None if automatic else "2026-03-01")


async def test_preexisting_typed_idempotency_hash_remains_retrievable(run_db):
    from hashlib import sha256

    body = request()
    row = await create(run_db, body)
    assert row.body_hash == sha256(body.model_dump_json(exclude={"idempotency_key", "automatic_context"}).encode()).hexdigest()
    assert (await create(run_db, body)).id == row.id
