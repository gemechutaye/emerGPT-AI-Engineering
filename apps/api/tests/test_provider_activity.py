"""Persistent usage receipts on isolated real Postgres; HTTP responses are declared fixtures."""

import asyncio
import json
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import httpx
import psycopg
import pytest
from emer.providers.openrouter import ProviderError
from emer.services import provider_activity
from emer.services.provider_activity import TrackedOpenRouterClient
from emer.storage.models import (
    Base,
    BrowserSession,
    Conversation,
    CorpusIndex,
    Draft,
    LiveSession,
    ProviderAttempt,
    Run,
    utcnow,
)
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


class Reply(BaseModel):
    model_config = ConfigDict(extra="forbid")
    answer: str


@pytest.fixture
async def activity_db(monkeypatch):
    database = "emer_activity_test_" + uuid4().hex[:12]
    with psycopg.connect("postgresql://localhost:55432/postgres", autocommit=True) as admin:
        admin.execute(f"CREATE DATABASE {database}")
    engine = create_async_engine(f"postgresql+psycopg://localhost:55432/{database}")
    session = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    monkeypatch.setattr(provider_activity, "Session", session)
    async with session.begin() as db:
        owner = BrowserSession(token_hash=uuid4().hex, expires_at=utcnow() + timedelta(hours=1))
        other = BrowserSession(token_hash=uuid4().hex, expires_at=utcnow() + timedelta(hours=1))
        db.add_all([owner, other])
        db.add(CorpusIndex(id="test-index", checksum="test-checksum", corpus_checksum="corpus-test",
                           bundle=b"fixture-only", manifest={}))
        await db.flush()
        conversation = Conversation(session_id=owner.id)
        db.add(conversation)
        await db.flush()
        run = Run(session_id=owner.id, conversation_id=conversation.id, index_id="test-index",
                  idempotency_key="fixture-run", body_hash="fixture-hash", question="Fixture question",
                  context_version=1, context={})
        live = LiveSession(session_id=owner.id, conversation_id=conversation.id, index_id="test-index",
                           context_version=1, context={}, idempotency_key="fixture-live", body_hash="fixture-hash",
                           status="working", snapshot={"revision": 1}, fence=1,
                           lease_expires_at=utcnow() + timedelta(seconds=20))
        db.add_all([run, live])
        await db.flush()
        draft = Draft(session_id=owner.id, run_id=run.id, index_id="test-index", text="Fixture draft")
        db.add(draft)
        await db.flush()
    try:
        yield SimpleNamespace(session=session, owner=owner, other=other, run=run, live=live, draft=draft)
    finally:
        await provider_activity.drain_provider_receipts()
        await engine.dispose()
        with psycopg.connect("postgresql://localhost:55432/postgres", autocommit=True) as admin:
            admin.execute(f"DROP DATABASE {database} WITH (FORCE)")


def completion(*, finish_reason="stop", content='{"answer":"Fixture response"}', cost=0.002):
    return httpx.Response(200, json={
        "id": "fixture-provider-request", "model": "openai/gpt-5.6-luna", "provider": "fixture",
        "usage": {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10, "cost": cost},
        "choices": [{"finish_reason": finish_reason, "message": {"content": content}}],
    })


def tracked(fixture, transport, **kwargs):
    if kwargs.get("live_session_id"):
        kwargs = {"live_revision": 1, "live_context_version": 1, "live_fence": 1, **kwargs}
    return TrackedOpenRouterClient(
        "private-fixture-key", "openai/gpt-5.6-luna", fixture.owner.id,
        client=transport, **kwargs,
    )


@pytest.mark.parametrize("change", ["revision", "context", "conversation", "lease", "fence", "closed", "missing_fence"])
async def test_obsolete_voice_cannot_dispatch_an_intent_call(activity_db, change):
    async with activity_db.session.begin() as db:
        live = await db.get(LiveSession, activity_db.live.id)
        if change == "revision":
            live.snapshot = {"revision": 2}
        elif change == "context":
            live.context_version = 2
        elif change == "conversation":
            conversation = await db.get(Conversation, live.conversation_id)
            conversation.context_version = 2
        elif change == "lease":
            live.lease_expires_at = utcnow() - timedelta(seconds=1)
        elif change == "fence":
            live.fence = 2
        elif change == "closed":
            live.status = "closed"
            live.lease_expires_at = None
            live.snapshot = {"revision": 1, "close_reason": "context_changed"}
    calls = 0

    def respond(request):
        nonlocal calls
        calls += 1
        return completion()

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as transport:
        kwargs = {"live_fence": None} if change == "missing_fence" else {}
        with pytest.raises(ProviderError) as failure:
            await tracked(activity_db, transport, live_session_id=activity_db.live.id, **kwargs).structured(
                "Fixture", {}, Reply, operation="voice_intent_resolution",
            )
    assert failure.value.code == "provider_claim_inactive"
    assert calls == 0 and await records(activity_db) == []


async def test_user_end_preserves_dispatch_of_already_authorized_background_work(activity_db):
    async with activity_db.session.begin() as db:
        live = await db.get(LiveSession, activity_db.live.id)
        live.status = "closed"
        live.lease_expires_at = None
        live.snapshot = {"revision": 1, "close_reason": "user", "provider_close_reason": "connection_lost"}
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: completion())) as transport:
        result = await tracked(activity_db, transport, live_session_id=activity_db.live.id).structured(
            "Fixture", {}, Reply, operation="voice_intent_resolution",
        )
    assert result.value.answer == "Fixture response"
    assert len(await records(activity_db)) == 1


async def records(fixture):
    async with fixture.session() as db:
        return list(await db.scalars(select(ProviderAttempt).order_by(ProviderAttempt.created_at)))


async def test_dispatch_committed_without_transaction_open_during_http(activity_db):
    calls = 0

    async def respond(request):
        nonlocal calls
        calls += 1
        # A second connection can lock both owner and dispatch row before HTTP returns.
        async with activity_db.session.begin() as db:
            attempt = await db.scalar(select(ProviderAttempt).with_for_update(nowait=True))
            owner = await db.scalar(select(BrowserSession).where(
                BrowserSession.id == activity_db.owner.id
            ).with_for_update(nowait=True))
            assert owner is not None and attempt.status == "dispatched" and attempt.usage is None
        return completion()

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as transport:
        result = await tracked(activity_db, transport, run_id=activity_db.run.id).structured(
            "Private prompt text", {"transcript": "Private transcript text"}, Reply,
        )
    rows = await records(activity_db)
    assert calls == 1 and result.value.answer == "Fixture response"
    assert len(rows) == 1 and rows[0].status == "completed" and rows[0].completed_at is not None
    assert rows[0].run_id == activity_db.run.id
    assert rows[0].usage["total_tokens"] == 10 and rows[0].usage["cost"] == 0.002
    saved = json.dumps(rows[0].usage)
    assert "private-fixture-key" not in saved and "Private prompt" not in saved and "Private transcript" not in saved


async def test_successful_receipt_survives_later_billed_checker_failure(activity_db):
    responses = [completion(cost=0.003), completion(finish_reason="length", cost=0.001)]
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: responses.pop(0))) as transport:
        client = tracked(activity_db, transport, run_id=activity_db.run.id)
        await client.structured("Fixture", {}, Reply, operation="generation")
        with pytest.raises(ProviderError) as failure:
            await client.structured("Fixture", {}, Reply, operation="support_check")
    rows = await records(activity_db)
    assert failure.value.code == "provider_incomplete"
    assert [(row.operation, row.status) for row in rows] == [("generation", "completed"), ("support_check", "failed")]
    assert [row.usage["cost"] for row in rows] == [0.003, 0.001]
    assert rows[1].error_code == "provider_incomplete"


async def test_cancellation_records_unknown_usage_without_fabricating_zero(activity_db):
    entered = asyncio.Event()
    calls = 0

    async def respond(request):
        nonlocal calls
        calls += 1
        entered.set()
        await asyncio.Event().wait()

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as transport:
        task = asyncio.create_task(tracked(activity_db, transport, live_session_id=activity_db.live.id).structured(
            "Fixture", {}, Reply, operation="voice_intent_resolution",
        ))
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    row = (await records(activity_db))[0]
    assert calls == 1 and row.status == "uncertain" and row.usage is None
    assert row.error_code == "provider_cancelled" and row.completed_at is not None
    assert row.live_session_id == activity_db.live.id


async def test_timeout_records_uncertain_attempt_without_retry(activity_db):
    calls = 0

    def respond(request):
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("Private transport detail", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as transport:
        with pytest.raises(ProviderError) as failure:
            await tracked(activity_db, transport, draft_id=activity_db.draft.id).structured(
                "Fixture", {}, Reply, operation="draft_recheck",
            )
    row = (await records(activity_db))[0]
    assert calls == 1 and failure.value.code == "provider_timeout"
    assert row.status == "uncertain" and row.usage is None and row.error_code == "provider_timeout"
    assert row.draft_id == activity_db.draft.id


async def test_cancellation_during_receipt_write_preserves_known_usage(activity_db):
    writing, release = asyncio.Event(), asyncio.Event()

    class SlowReceiptClient(TrackedOpenRouterClient):
        async def _write_receipt(self, attempt_id, status, usage, failure):
            writing.set()
            await release.wait()
            await super()._write_receipt(attempt_id, status, usage, failure)

    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: completion())) as transport:
        client = SlowReceiptClient("fixture-key", "openai/gpt-5.6-luna", activity_db.owner.id,
                                   run_id=activity_db.run.id, client=transport)
        task = asyncio.create_task(client.structured("Fixture", {}, Reply))
        await asyncio.wait_for(writing.wait(), 1)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    row = (await records(activity_db))[0]
    assert row.status == "completed" and row.usage["total_tokens"] == 10 and row.error_code is None


async def test_owner_or_resource_mismatch_stops_dispatch_before_http(activity_db):
    calls = 0

    def respond(request):
        nonlocal calls
        calls += 1
        return completion()

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as transport:
        client = TrackedOpenRouterClient("fixture-key", "openai/gpt-5.6-luna", activity_db.other.id,
                                         run_id=activity_db.run.id, client=transport)
        with pytest.raises(ProviderError) as scope_error:
            await client.structured("Fixture", {}, Reply)
        assert scope_error.value.code == "provider_scope_invalid"
        async with activity_db.session.begin() as db:
            owner = await db.get(BrowserSession, activity_db.owner.id)
            owner.revoked = True
        with pytest.raises(ProviderError) as owner_error:
            await tracked(activity_db, transport).structured("Fixture", {}, Reply)
        assert owner_error.value.code == "provider_owner_inactive"
    assert calls == 0 and await records(activity_db) == []


async def test_database_receipt_timeout_keeps_dispatch_and_returns_known_usage_error(activity_db):
    held = activity_db.session()
    transaction = await held.begin()

    async def respond(request):
        await held.scalar(select(ProviderAttempt).with_for_update())
        return completion()

    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as transport:
            with pytest.raises(ProviderError) as failure:
                await tracked(activity_db, transport, ledger_timeout_seconds=0.1).structured("Fixture", {}, Reply)
        assert failure.value.code == "provider_receipt_unavailable"
        assert failure.value.usage.total_tokens == 10
    finally:
        await transaction.rollback()
        await held.close()
    row = (await records(activity_db))[0]
    assert row.status == "dispatched" and row.usage is None and row.completed_at is None


@pytest.mark.parametrize("change", ["cancelled", "expired", "fenced", "owner", "context", "terminal"])
async def test_lost_run_claim_stops_every_later_paid_stage(activity_db, change):
    async with activity_db.session.begin() as db:
        row = await db.get(Run, activity_db.run.id)
        row.status, row.owner_id, row.fence = "running", "controller-one", 3
        row.lease_expires_at = utcnow() + timedelta(seconds=30)
    calls = 0

    async def respond(request):
        nonlocal calls
        calls += 1
        # Work loses its claim after generation, before a support-check/repair call.
        async with activity_db.session.begin() as db:
            row = await db.get(Run, activity_db.run.id)
            if change == "cancelled":
                row.cancel_requested = True
            elif change == "expired":
                row.lease_expires_at = utcnow() - timedelta(seconds=1)
            elif change == "fenced":
                row.fence += 1
            elif change == "owner":
                row.owner_id = "new-controller"
            elif change == "terminal":
                row.status = "interrupted"
            else:
                conversation = await db.get(Conversation, row.conversation_id)
                conversation.context_version += 1
        return completion()

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as transport:
        client = tracked(activity_db, transport, run_id=activity_db.run.id,
                         run_fence=3, run_owner="controller-one")
        await client.structured("Fixture", {}, Reply, operation="generation")
        with pytest.raises(ProviderError) as failure:
            await client.structured("Fixture", {}, Reply, operation="support_check")
        assert failure.value.code == "provider_claim_inactive"
    rows = await records(activity_db)
    assert calls == 1 and len(rows) == 1 and rows[0].status == "completed"


async def test_changed_draft_stops_paid_check_before_dispatch(activity_db):
    async with activity_db.session.begin() as db:
        draft = await db.get(Draft, activity_db.draft.id)
        draft.check_owner, draft.checking_version = "frozen-check", 1
        draft.check_lease_expires_at = utcnow() + timedelta(seconds=30)
        draft.version = 2
    calls = 0

    def respond(request):
        nonlocal calls
        calls += 1
        return completion()

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as transport:
        with pytest.raises(ProviderError) as failure:
            await tracked(activity_db, transport, draft_id=activity_db.draft.id,
                          draft_claim="frozen-check").structured("Fixture", {}, Reply, operation="draft_recheck")
        assert failure.value.code == "provider_claim_inactive"
    assert calls == 0 and await records(activity_db) == []
