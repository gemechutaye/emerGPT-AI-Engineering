"""Live admission/fencing on isolated real Postgres; provider transport is a declared double."""

import asyncio
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import psycopg
import pytest
from emer.api import voice_routes
from emer.contracts.voice import VoiceIntent, VoiceOffer, VoiceSnapshot
from emer.providers.openai_live import LiveProviderError, ProviderSession
from emer.services import conversation_memory, voice_storage
from emer.storage.models import (
    Base,
    BrowserSession,
    Conversation,
    CorpusIndex,
    Delegation,
    LiveEvent,
    LiveSession,
    Run,
    utcnow,
)
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from test_voice import FakeProvider, NoCloseAcknowledgment


class SessionProvider(FakeProvider):
    def __init__(self):
        super().__init__()
        self.creates = 0
        self.uncertain = False

    async def create(self, sdp, *, history=None):
        self.history = history
        self.creates += 1
        if self.uncertain:
            raise LiveProviderError("live_creation_outcome_unknown", uncertain=True)
        return ProviderSession("live_test_" + uuid4().hex, "v=0\r\nanswer")


@pytest.fixture
async def voice_db(monkeypatch):
    database = "emer_voice_test_" + uuid4().hex[:12]
    with psycopg.connect("postgresql://localhost:55432/postgres", autocommit=True) as admin:
        admin.execute(f"CREATE DATABASE {database}")
    engine = create_async_engine(f"postgresql+psycopg://localhost:55432/{database}")
    session = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    monkeypatch.setattr(voice_routes, "Session", session)
    monkeypatch.setattr(voice_storage, "Session", session)
    monkeypatch.setattr(conversation_memory, "Session", session)

    async def no_metadata(*args, **kwargs):
        pass

    monkeypatch.setattr(conversation_memory, "schedule_auto", no_metadata)
    monkeypatch.setattr(voice_routes, "schedule_auto", no_metadata)
    monkeypatch.setattr(voice_routes.settings, "openai_api_key", "fixture-key-not-live")
    provider = SessionProvider()
    monkeypatch.setattr(voice_routes, "provider", provider)
    monkeypatch.setattr(voice_routes, "controllers", {})

    async def bundle():
        return SimpleNamespace(id="index-test")

    monkeypatch.setattr(voice_routes, "load_bundle", bundle)
    async with session.begin() as db:
        owner = BrowserSession(token_hash=uuid4().hex, expires_at=utcnow() + timedelta(hours=1))
        other = BrowserSession(token_hash=uuid4().hex, expires_at=utcnow() + timedelta(hours=1))
        db.add_all([owner, other])
        db.add(CorpusIndex(id="index-test", checksum="test-checksum", corpus_checksum="corpus-test",
                           bundle=b"test-only", manifest={}))
        await db.flush()
        conversation = Conversation(session_id=owner.id)
        db.add(conversation)
        await db.flush()
    try:
        yield SimpleNamespace(session=session, provider=provider, owner=owner, other=other, conversation=conversation)
    finally:
        await asyncio.gather(*(ctl.close("test_cleanup", cancel_work=True)
                               for ctl in list(voice_routes.controllers.values())), return_exceptions=True)
        await engine.dispose()
        with psycopg.connect("postgresql://localhost:55432/postgres", autocommit=True) as admin:
            admin.execute(f"DROP DATABASE {database} WITH (FORCE)")


def offer(fixture, key="voice-test-key"):
    return VoiceOffer(conversation_id=fixture.conversation.id, context_version=1,
                      sdp="v=0\r\nfixture offer", idempotency_key=key)


async def test_persisted_create_idempotency_owner_isolation_and_close(voice_db):
    created = await voice_routes.create_session(offer(voice_db), voice_db.owner)
    duplicate = await voice_routes.create_session(offer(voice_db), voice_db.owner)
    assert duplicate.id == created.id
    assert voice_db.provider.creates == 1
    with pytest.raises(HTTPException) as changed:
        await voice_routes.create_session(offer(voice_db).model_copy(update={"sdp": "v=0\r\nother offer"}), voice_db.owner)
    assert changed.value.status_code == 409
    with pytest.raises(HTTPException) as other:
        await voice_routes.get_session(created.id, voice_db.other)
    assert other.value.status_code == 404
    closed = await voice_routes.close_session(created.id, voice_db.owner)
    assert closed.status == "closed" and closed.final_usage_confirmed
    persisted = await voice_routes.get_session(created.id, voice_db.owner)
    assert persisted.usage_seconds == 3.5
    assert persisted.close_reason == "user"
    assert persisted.provider_close_reason == "close_requested"
    async with voice_db.session() as db:
        event = await db.scalar(select(LiveEvent).where(LiveEvent.live_session_id == created.id))
        assert event.kind == "session.closed" and event.payload["usage"]["seconds"] == 3.5


async def test_uncertain_creation_remains_blocking_and_is_not_retried(voice_db):
    voice_db.provider.uncertain = True
    with pytest.raises(HTTPException) as error:
        await voice_routes.create_session(offer(voice_db), voice_db.owner)
    assert error.value.status_code == 503
    with pytest.raises(HTTPException) as blocked:
        await voice_routes.create_session(offer(voice_db, "new-attempt-key"), voice_db.owner)
    assert blocked.value.status_code == 409
    assert voice_db.provider.creates == 1
    async with voice_db.session() as db:
        row = await db.scalar(select(LiveSession))
        assert row.status == "uncertain"
    closed = await voice_routes.close_session(row.id, voice_db.owner)
    assert not closed.final_usage_confirmed
    async with voice_db.session() as db:
        assert (await db.get(LiveSession, row.id)).status == "uncertain"


async def test_fenced_atomic_event_and_delegation_claims_with_revisions(voice_db):
    created = await voice_routes.create_session(offer(voice_db), voice_db.owner)
    hooks = voice_storage.PostgresVoiceHooks()
    assert await hooks.save_snapshot(created.id, 1, VoiceSnapshot(id=created.id, status="working", revision=1))
    assert await hooks.claim_event(created.id, 1, "transcript-event", "session.input_transcript.delta",
                                   {"delta": "original"})
    assert not await hooks.claim_event(created.id, 1, "transcript-event", "session.input_transcript.delta",
                                       {"delta": "forged duplicate"})
    intent = VoiceIntent(session_id=created.id, provider_session_id=created.provider_session_id,
                         conversation_id=voice_db.conversation.id, context_version=1, index_id="index-test",
                         fence=1, revision=1, delegation_id="item_stable", question="Question", transcript=[])
    claimed = await asyncio.gather(hooks.claim_delegation(intent), hooks.claim_delegation(intent))
    assert sorted(claimed) == [False, True]
    assert await hooks.save_snapshot(created.id, 1, VoiceSnapshot(id=created.id, status="working", revision=2))
    assert await hooks.claim_delegation(intent.model_copy(update={"revision": 2, "question": "Corrected question"}))
    assert not await hooks.claim_delegation(intent)
    async with voice_db.session() as db:
        delegation = await db.scalar(select(Delegation))
        assert delegation.revision == 2 and delegation.intent["question"] == "Corrected question"
        original = await db.scalar(select(LiveEvent).where(LiveEvent.provider_event_id == "transcript-event"))
        assert original.payload == {"delta": "original"}
    async with voice_db.session.begin() as db:
        row = await db.get(LiveSession, created.id)
        row.fence += 1
    assert not await hooks.renew(created.id, 1)
    assert not await hooks.save_snapshot(created.id, 1, VoiceSnapshot(id=created.id))


async def test_recovery_does_not_interrupt_a_renewed_owner(voice_db):
    created = await voice_routes.create_session(offer(voice_db), voice_db.owner)
    await voice_routes.recover_voice()
    assert voice_db.provider.hangups == []
    assert (await voice_routes.get_session(created.id, voice_db.owner)).controller_connected
    async with voice_db.session.begin() as db:
        row = await db.get(LiveSession, created.id)
        row.lease_expires_at = utcnow() - timedelta(seconds=1)
    await voice_routes.recover_voice()
    snapshot = await voice_routes.get_session(created.id, voice_db.owner)
    assert snapshot.playback_blocked and not snapshot.controller_connected
    assert snapshot.error == "live_controller_expired"


async def test_expired_browser_cannot_renew_a_live_controller(voice_db):
    created = await voice_routes.create_session(offer(voice_db), voice_db.owner)
    async with voice_db.session.begin() as db:
        owner = await db.get(BrowserSession, voice_db.owner.id)
        owner.revoked = True
    assert not await voice_storage.PostgresVoiceHooks().renew(created.id, 1)


async def test_unconfirmed_cleanup_persists_and_blocks_new_voice_until_confirmed(voice_db, monkeypatch):
    voice_db.provider.connection = NoCloseAcknowledgment()
    confirmed = False

    async def hangup(session_id):
        voice_db.provider.hangups.append(session_id)
        return confirmed

    monkeypatch.setattr(voice_db.provider, "hangup", hangup)
    created = await voice_routes.create_session(offer(voice_db), voice_db.owner)
    ctl = voice_routes.controllers[created.id]
    ctl.close_timeout = 0.001
    ctl._start_time -= 301  # the maximum lifetime has already elapsed
    closed = await voice_routes.close_session(created.id, voice_db.owner)
    assert closed.status == "failed" and closed.error == "live_close_unconfirmed"
    async with voice_db.session() as db:
        assert (await db.get(LiveSession, created.id)).status == "uncertain"
    with pytest.raises(HTTPException) as blocked:
        await voice_routes.create_session(offer(voice_db, "different-attempt"), voice_db.owner)
    assert blocked.value.detail["code"] == "LIVE_ACTIVE"
    assert voice_db.provider.creates == 1
    confirmed = True
    reconciled = await voice_routes.close_session(created.id, voice_db.owner)
    assert reconciled.status == "closed" and reconciled.error is None
    assert not reconciled.final_usage_confirmed
    await voice_routes.close_session(created.id, voice_db.owner)
    assert len(voice_db.provider.hangups) == 2  # already-closed repeats do not dispatch again


async def test_pending_cleanup_keeps_only_its_lease_after_browser_revocation(voice_db, monkeypatch):
    voice_db.provider.connection = NoCloseAcknowledgment()

    async def hangup(_session_id):
        return False

    monkeypatch.setattr(voice_db.provider, "hangup", hangup)
    created = await voice_routes.create_session(offer(voice_db), voice_db.owner)
    ctl = voice_routes.controllers[created.id]
    ctl.close_timeout = 0.001
    result = await voice_routes.close_session(created.id, voice_db.owner)
    assert result.status == "closing" and created.id in voice_routes.controllers
    async with voice_db.session.begin() as db:
        (await db.get(BrowserSession, voice_db.owner.id)).revoked = True
        (await db.get(Conversation, voice_db.conversation.id)).context_version += 1
    assert await voice_storage.PostgresVoiceHooks().renew(created.id, 1)
    await ctl._finalize_close(confirmed=False)


async def test_attach_failure_does_not_erase_unconfirmed_provider_cleanup(voice_db, monkeypatch):
    async def attach(_session_id):
        raise LiveProviderError("live_control_connection_failed")

    async def hangup(_session_id):
        return False

    monkeypatch.setattr(voice_db.provider, "attach", attach)
    monkeypatch.setattr(voice_db.provider, "hangup", hangup)
    with pytest.raises(HTTPException) as failed:
        await voice_routes.create_session(offer(voice_db), voice_db.owner)
    assert failed.value.detail["code"] == "LIVE_CONTROL_FAILED"
    live_id = failed.value.detail["voice_session_id"]
    async with voice_db.session() as db:
        stored = await db.get(LiveSession, live_id)
        assert stored.status == "closing"
        assert stored.snapshot["error"] == "live_close_unconfirmed"
        assert stored.lease_expires_at > utcnow()
    assert await voice_storage.PostgresVoiceHooks().renew(live_id, 1)
    with pytest.raises(HTTPException) as blocked:
        await voice_routes.create_session(offer(voice_db, "attach-retry-key"), voice_db.owner)
    assert blocked.value.detail["code"] == "LIVE_ACTIVE"
    assert voice_db.provider.creates == 1
    await voice_routes.controllers[live_id]._finalize_close(confirmed=False)


async def test_old_run_cancellation_timeout_is_not_a_retryable_answer_timeout(voice_db):
    async with voice_db.session.begin() as db:
        run = Run(
            session_id=voice_db.owner.id, conversation_id=voice_db.conversation.id,
            index_id="index-test", idempotency_key="old-voice-run", body_hash="fixture",
            question="Check the supplied sources.", context_version=1, status="running",
        )
        db.add(run)
        await db.flush()
        run_id = run.id
    hooks = voice_storage.PostgresVoiceHooks()
    with pytest.raises(RuntimeError, match="live_run_cancellation_unconfirmed"):
        await hooks._cancel_run(run_id, timeout_seconds=0.02)
    async with voice_db.session() as db:
        saved = await db.get(Run, run_id)
        assert saved.status == "running" and saved.cancel_requested
    # A later confirmed terminal update is sufficient; no provider dispatch is involved.
    async with voice_db.session.begin() as db:
        (await db.get(Run, run_id)).status = "cancelled"
    await hooks._cancel_run(run_id, timeout_seconds=0.02)
