"""Cross-client change detection and title races on isolated Postgres; provider doubles."""

import asyncio
import json
from datetime import timedelta
from uuid import uuid4

import httpx
import pytest
from emer.api import history_routes
from emer.contracts.conversations import ConversationUpdate
from emer.services import conversation_titles as titles
from emer.services import conversations, voice_storage
from emer.storage.models import BrowserSession, Conversation, ProviderAttempt, Run, utcnow
from sqlalchemy import delete, select
from test_conversation_memory import memory_db as isolated_db

memory_db = isolated_db


@pytest.fixture
async def history_db(memory_db, monkeypatch):
    monkeypatch.setattr(titles, "Session", memory_db.session)
    monkeypatch.setattr(history_routes, "Session", memory_db.session)
    requests = []
    fixture = memory_db
    fixture.title_handler = None

    async def respond(request):
        body = json.loads(request.content)
        requests.append(body)
        if fixture.title_handler:
            return await fixture.title_handler(request)
        return httpx.Response(200, json={
            "id": "title-fixture", "model": titles.MODEL,
            "usage": {"prompt_tokens": 80, "completion_tokens": 12, "total_tokens": 92, "cost": 0.0001},
            "choices": [{"finish_reason": "stop", "message": {
                "content": json.dumps({"title": "Cancellation Policy Questions"}),
            }}],
        })

    client_type = titles.TitleClient
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        monkeypatch.setattr(titles, "TitleClient", lambda job: client_type(job, client=client))
        fixture.title_requests = requests
        yield fixture
        await titles.shutdown()


async def queued(fixture):
    async with fixture.session.begin() as db:
        row = await db.get(Conversation, fixture.conversation.id, with_for_update=True)
        await titles.queue_title(db, row)
        await titles.queue_title(db, row)
    async with fixture.session() as db:
        return await db.scalar(select(ProviderAttempt).where(ProviderAttempt.operation == titles.OPERATION))


async def test_early_title_is_durable_single_request_and_preserves_recap(history_db):
    async with history_db.session.begin() as db:
        run = await db.get(Run, history_db.run.id)
        run.status = "running"
    before = await history_routes.revision(history_db.owner.id)
    job = await queued(history_db)
    await asyncio.gather(titles.execute(job.id), titles.execute(job.id))
    async with history_db.session() as db:
        row = await db.get(Conversation, history_db.conversation.id)
        receipt = await db.get(ProviderAttempt, job.id)
        assert row.title == "Cancellation Policy Questions" and row.title_generated
        assert row.summary is None and row.activity_version == 1
        assert receipt.status == "completed" and receipt.usage["total_tokens"] == 92
    assert await history_routes.revision(history_db.owner.id) != before
    assert len(history_db.title_requests) == 1
    request = history_db.title_requests[0]
    assert request["model"] == "google/gemini-3.8-flash"
    assert request["reasoning"] == {"effort": "high", "exclude": True}
    assert request["provider"]["zdr"] and request["provider"]["allow_fallbacks"] is False
    await titles.recover()
    assert not titles._tasks


async def test_manual_rename_wins_while_title_request_is_in_flight(history_db):
    started, release = asyncio.Event(), asyncio.Event()

    async def delayed(request):
        started.set()
        await release.wait()
        return httpx.Response(200, json={"model": titles.MODEL, "choices": [{"finish_reason": "stop",
            "message": {"content": '{"title":"Generated Topic"}'}}]})

    history_db.title_handler = delayed
    job = await queued(history_db)
    task = asyncio.create_task(titles.execute(job.id))
    await asyncio.wait_for(started.wait(), 2)
    await conversations.update_owned(history_db.owner.id, history_db.conversation.id,
                                     ConversationUpdate(title="My policy notes"))
    release.set()
    await task
    async with history_db.session() as db:
        row = await db.get(Conversation, history_db.conversation.id)
        assert row.title == "My policy notes" and row.title_origin == "manual"


async def test_interrupted_dispatch_is_never_rebilled_on_restart(history_db):
    job = await queued(history_db)
    async with history_db.session.begin() as db:
        row = await db.get(ProviderAttempt, job.id)
        row.status, row.created_at = "dispatched", utcnow() - timedelta(minutes=2)
    await titles.recover()
    async with history_db.session() as db:
        row = await db.get(ProviderAttempt, job.id)
        assert row.status == "uncertain" and row.usage is None
    assert not history_db.title_requests and not titles._tasks


async def test_queued_title_resumes_after_restart(history_db):
    job = await queued(history_db)
    async with history_db.session.begin() as db:
        row = await db.get(ProviderAttempt, job.id)
        row.created_at = utcnow() - timedelta(seconds=3)
    await titles.recover()
    await asyncio.gather(*list(titles._tasks.values()))
    async with history_db.session() as db:
        row = await db.get(Conversation, history_db.conversation.id)
        assert row.title_generated


async def test_rate_limited_title_recovers_with_bounded_receipts(history_db):
    job = await queued(history_db)

    async def limited(request):
        return httpx.Response(429, json={"error": {"code": 429}})

    history_db.title_handler = limited
    await titles.execute(job.id)
    async with history_db.session() as db:
        assert (await db.get(ProviderAttempt, job.id)).error_code == "provider_rate_limited"
    await titles.recover()
    async with history_db.session() as db:
        retry = await db.scalar(select(ProviderAttempt).where(ProviderAttempt.status == "queued"))
        assert retry and retry.id != job.id
    history_db.title_handler = None
    await titles.execute(retry.id)
    assert len(history_db.title_requests) == 2
    async with history_db.session() as db:
        assert (await db.get(Conversation, history_db.conversation.id)).title_generated


async def test_persistent_rate_limit_stops_after_three_attempts(history_db):
    async def limited(request):
        return httpx.Response(429, json={"error": {"code": 429}})

    history_db.title_handler = limited
    await queued(history_db)
    for _ in range(3):
        async with history_db.session() as db:
            job = await db.scalar(select(ProviderAttempt).where(ProviderAttempt.status == "queued"))
        assert job
        await titles.execute(job.id)
        await titles.recover()
    async with history_db.session() as db:
        assert not await db.scalar(select(ProviderAttempt.id).where(ProviderAttempt.status == "queued"))
    assert len(history_db.title_requests) == 3


async def test_voice_transcript_without_rag_run_is_saved_named_and_notified(history_db):
    async with history_db.session.begin() as db:
        await db.execute(delete(Run).where(Run.id == history_db.run.id))
    before = await history_routes.revision(history_db.owner.id)
    hooks = voice_storage.PostgresVoiceHooks()
    assert await hooks.claim_event(history_db.live.id, 1, "new-voice-turn",
                                   "session.input_transcript.delta", {"delta": "What are the cancellation rules?"})
    assert not await hooks.claim_event(history_db.live.id, 1, "new-voice-turn",
                                       "session.input_transcript.delta", {"delta": "Duplicate"})
    async with history_db.session() as db:
        job = await db.scalar(select(ProviderAttempt).where(ProviderAttempt.operation == titles.OPERATION))
        assert (await db.get(Conversation, history_db.conversation.id)).activity_version == 2
    assert await history_routes.revision(history_db.owner.id) != before
    await titles.execute(job.id)
    payload = json.loads(history_db.title_requests[0]["messages"][1]["content"])
    assert "cancellation rules" in payload["message"] and "Duplicate" not in payload["message"]


async def test_change_notifications_are_owned_and_expire_on_reset(history_db):
    initial = await history_routes.revision(history_db.owner.id)
    async with history_db.session.begin() as db:
        foreign = BrowserSession(token_hash=uuid4().hex, expires_at=utcnow() + timedelta(hours=1))
        db.add(foreign)
        await db.flush()
        db.add(Conversation(session_id=foreign.id, title="Unrelated workspace"))
    assert await history_routes.revision(history_db.owner.id) == initial
    async with history_db.session.begin() as db:
        run = await db.get(Run, history_db.run.id)
        run.completed_at = utcnow()
    assert await history_routes.revision(history_db.owner.id) != initial
    async with history_db.session.begin() as db:
        owner = await db.get(BrowserSession, history_db.owner.id)
        owner.revoked = True
    assert await history_routes.revision(history_db.owner.id) is None
