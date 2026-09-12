"""Shared-browser Live seams on isolated Postgres; provider/media are doubles, not audio proof."""

import asyncio
from types import SimpleNamespace

import httpx
import pytest
from emer.api import app as app_routes
from emer.api import conversation_routes, history_routes, voice_routes
from emer.contracts.answer import ProviderUsage
from emer.contracts.voice import VoiceIntent, VoiceSnapshot
from emer.services import conversations, shared_workspace, voice_storage
from emer.settings import settings
from emer.storage.models import BrowserSession, Conversation, Delegation, LiveEvent, LiveSession, Run
from sqlalchemy import select
from test_voice import FakeConnection, delegation, eventually, transcript
from test_voice_postgres import SessionProvider, offer
from test_voice_postgres import voice_db as isolated_voice_db

voice_db = isolated_voice_db


class SharedSessionProvider(SessionProvider):
    async def create(self, sdp, *, history=None):
        self.connection = FakeConnection()
        return await super().create(sdp, history=history)


@pytest.fixture
async def shared_voice_db(voice_db, monkeypatch):
    monkeypatch.setattr(settings, "shared_workspace", True)
    monkeypatch.setattr(settings, "app_origin", "http://shared-voice.test")
    for module in (shared_workspace, app_routes, conversation_routes, history_routes, conversations):
        monkeypatch.setattr(module, "Session", voice_db.session)

    class NoUnconfiguredInference:
        def __init__(self, *args, **kwargs):
            raise AssertionError("Shared voice tests must explicitly supply a provider double")

    monkeypatch.setattr(voice_storage, "TrackedOpenRouterClient", NoUnconfiguredInference)
    provider = SharedSessionProvider()
    monkeypatch.setattr(voice_routes, "provider", provider)
    async with voice_db.session.begin() as db:
        saved = Run(
            session_id=voice_db.owner.id, conversation_id=voice_db.conversation.id,
            index_id="index-test", idempotency_key="legacy-saved-run", body_hash="fixture",
            question="Earlier saved question", context_version=1, status="completed",
        )
        db.add(saved)
        await db.flush()
        saved_id = saved.id
    changed = await shared_workspace.initialize()
    assert changed["conversations"] == 1 and changed["runs"] == 1
    assert not any((await shared_workspace.initialize()).values())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_routes.app), base_url=settings.app_origin,
        headers={"Origin": settings.app_origin, "Cookie": "emer_session=unrelated-old-browser"},
    ) as browser_a, httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_routes.app), base_url=settings.app_origin,
        headers={"Origin": settings.app_origin},
    ) as browser_b:
        for browser in (browser_a, browser_b):
            boot = await browser.post("/api/v1/session")
            assert boot.status_code == 200
            assert boot.json()["id"] == shared_workspace.WORKSPACE_ID
            assert "set-cookie" not in boot.headers
        yield SimpleNamespace(
            session=voice_db.session, provider=provider, conversation=voice_db.conversation,
            legacy_run_id=saved_id, a=browser_a, b=browser_b,
        )


async def test_two_shared_browsers_admit_one_voice_and_either_can_end_it(shared_voice_db):
    fixture = shared_voice_db
    bodies = [offer(fixture, key).model_dump() for key in ("browser-a-offer", "browser-b-offer")]
    clients = [fixture.a, fixture.b]
    responses = await asyncio.gather(*(
        client.post("/api/v1/voice/sessions", json=body)
        for client, body in zip(clients, bodies, strict=True)
    ))
    assert sorted(response.status_code for response in responses) == [201, 409]
    winner = next(index for index, response in enumerate(responses) if response.status_code == 201)
    other = 1 - winner
    assert responses[other].json()["detail"]["code"] == "LIVE_ACTIVE"
    live_id = responses[winner].json()["id"]
    assert fixture.provider.creates == 1
    repeated = await clients[other].post("/api/v1/voice/sessions", json=bodies[winner])
    assert repeated.status_code == 201 and repeated.json()["id"] == live_id
    assert fixture.provider.creates == 1
    for client in clients:
        assert (await client.get(f"/api/v1/voice/sessions/{live_id}")).json()["controller_connected"]
        assert (await client.get("/api/v1/voice/sessions")).json()["items"][0]["id"] == live_id

    ended = await clients[other].post(f"/api/v1/voice/sessions/{live_id}/close")
    assert ended.status_code == 200
    assert ended.json()["status"] == "closed" and ended.json()["final_usage_confirmed"]
    assert ended.json()["playback_blocked"] and ended.json()["close_reason"] == "user"
    seen = await clients[winner].get(f"/api/v1/voice/sessions/{live_id}")
    assert seen.json() == ended.json()
    async with fixture.session() as db:
        row = await db.get(LiveSession, live_id)
        assert row.session_id == shared_workspace.WORKSPACE_ID and row.status == "closed"
        assert row.snapshot["provider_close_reason"] == "close_requested"

    # End frees the shared admission slot; a new browser offer is a new provider session.
    next_voice = await clients[other].post(
        "/api/v1/voice/sessions", json=offer(fixture, "after-end-offer").model_dump(),
    )
    assert next_voice.status_code == 201 and next_voice.json()["id"] != live_id
    assert fixture.provider.creates == 2
    assert (await clients[winner].post(
        f"/api/v1/voice/sessions/{next_voice.json()['id']}/close",
    )).json()["status"] == "closed"


async def test_shared_reset_retains_active_voice_and_end_preserves_pending_work(shared_voice_db, monkeypatch):
    fixture = shared_voice_db
    started, release, cancelled = asyncio.Event(), asyncio.Event(), asyncio.Event()

    class GatedResolver:
        def __init__(self, *args, **kwargs):
            assert kwargs["session_id"] == shared_workspace.WORKSPACE_ID

        async def structured(self, instructions, payload, schema, **kwargs):
            started.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise
            return SimpleNamespace(
                value=schema(status="ambiguous", base_reference_id=None, bindings=[]),
                usage=ProviderUsage(operation="voice_intent_resolution", model="fixture-double", latency_ms=1),
            )

    monkeypatch.setattr(voice_storage, "TrackedOpenRouterClient", GatedResolver)
    response = await fixture.a.post("/api/v1/voice/sessions", json=offer(fixture).model_dump())
    assert response.status_code == 201
    live_id = response.json()["id"]
    ctl = voice_routes.controllers[live_id]
    ctl.settle_seconds = 0
    task = None
    try:
        await ctl.handle_event(transcript(text="Could you explain that?"))
        await ctl.handle_event({
            **transcript("assistant-text", "Let me check the sources.", 30),
            "type": "session.output_transcript.delta",
        })
        await ctl.handle_event(delegation())
        await asyncio.wait_for(started.wait(), 2)
        task = ctl._active_answer
        assert task is not None
        reset = await fixture.b.post("/api/v1/session/reset")
        assert reset.status_code == 200 and reset.json()["id"] == shared_workspace.WORKSPACE_ID
        active = (await fixture.a.get(f"/api/v1/voice/sessions/{live_id}")).json()
        assert active["controller_connected"] and active["status"] == "working"
        assert not cancelled.is_set() and not task.done()

        ended = await fixture.b.post(f"/api/v1/voice/sessions/{live_id}/close")
        assert ended.status_code == 200 and ended.json()["status"] == "closed"
        assert ended.json()["playback_blocked"] and ended.json()["final_usage_confirmed"]
        assert not task.done() and not cancelled.is_set()
        assert (await fixture.a.post("/api/v1/session/reset")).json()["id"] == shared_workspace.WORKSPACE_ID
        async with fixture.session() as db:
            row = await db.get(LiveSession, live_id)
            assert row.answer_pending and row.snapshot["close_reason"] == "user"
            owner = await db.get(BrowserSession, shared_workspace.WORKSPACE_ID)
            assert not owner.revoked and owner.expires_at.year == 9999
            saved = await db.get(Run, fixture.legacy_run_id)
            assert saved.session_id == owner.id and saved.question == "Earlier saved question"
            assert not saved.cancel_requested
            assert (await db.get(Conversation, fixture.conversation.id)).session_id == owner.id

        release.set()
        await asyncio.wait_for(asyncio.gather(task, ctl._worker), 2)
        await eventually(lambda: ctl._active_answer is None)
        assert not cancelled.is_set()
        assert not any(event["type"] == "session.commentary.append" for event in fixture.provider.connection.sent)
        async with fixture.session() as db:
            row = await db.get(LiveSession, live_id)
            assert row.status == "closed" and not row.answer_pending
            assert row.answer_pending_until is None
            events = list(await db.scalars(select(LiveEvent).where(LiveEvent.live_session_id == live_id)))
            assert {event.payload["delta"] for event in events if "delta" in event.payload} == {
                "Could you explain that?", "Let me check the sources.",
            }
        for client in (fixture.a, fixture.b):
            page = await client.get(f"/api/v1/conversations/{fixture.conversation.id}/transcripts")
            assert page.status_code == 200
            assert "Could you explain that?" in page.text and "Let me check the sources." in page.text
    finally:
        release.set()
        await ctl.close("test_cleanup", cancel_work=True)
        if task is not None:
            await asyncio.wait_for(asyncio.gather(task, ctl._worker, return_exceptions=True), 2)


async def test_shared_workspace_identity_does_not_bypass_voice_fences(shared_voice_db):
    fixture = shared_voice_db
    response = await fixture.a.post("/api/v1/voice/sessions", json=offer(fixture).model_dump())
    assert response.status_code == 201
    live_id = response.json()["id"]
    hooks = voice_storage.PostgresVoiceHooks()
    first = VoiceSnapshot(id=live_id, status="working", revision=1, controller_connected=True)
    assert await hooks.save_snapshot(live_id, 1, first)
    intent = VoiceIntent(
        session_id=live_id, provider_session_id=response.json()["provider_session_id"],
        conversation_id=fixture.conversation.id, context_version=1, index_id="index-test",
        fence=1, revision=1, delegation_id="shared-delegation", question="Original question", transcript=[],
    )
    assert await hooks.claim_delegation(intent)
    async with fixture.session.begin() as db:
        row = await db.get(LiveSession, live_id)
        row.fence += 1  # Simulate replacement of the controller lease, not a change of browser.
    current = first.model_copy(update={"revision": 2})
    assert await hooks.save_snapshot(live_id, 2, current)
    assert await hooks.claim_event(live_id, 2, "current-event", "session.usage.updated", {"usage": {"seconds": 1}})
    assert await hooks.claim_delegation(intent.model_copy(update={"fence": 2, "revision": 2, "question": "Current question"}))
    assert not await hooks.renew(live_id, 1)
    assert not await hooks.save_snapshot(live_id, 1, first.model_copy(update={"revision": 99}))
    assert not await hooks.claim_event(live_id, 1, "stale-event", "session.usage.updated", {"usage": {"seconds": 999}})
    assert not await hooks.claim_delegation(intent.model_copy(update={"revision": 99}))
    for client in (fixture.a, fixture.b):
        snapshot = (await client.get(f"/api/v1/voice/sessions/{live_id}")).json()
        assert snapshot["revision"] == 2
    async with fixture.session() as db:
        row = await db.get(LiveSession, live_id)
        assert row.session_id == shared_workspace.WORKSPACE_ID and row.fence == 2
        stored = await db.scalar(select(Delegation).where(Delegation.live_session_id == live_id))
        assert stored.revision == 2 and stored.intent["question"] == "Current question"
        assert await db.scalar(select(LiveEvent).where(LiveEvent.provider_event_id == "stale-event")) is None
