"""Closed-session admission on isolated Postgres; no real model or media calls."""

import asyncio
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from emer.api import voice_routes
from emer.contracts.voice import VoiceAnswer, VoiceIntent
from emer.services import voice_storage
from emer.storage.models import BrowserSession, Conversation, Delegation, LiveSession, utcnow
from sqlalchemy import select
from test_voice import NoCloseAcknowledgment, delegation, eventually, transcript
from test_voice_postgres import offer
from test_voice_postgres import voice_db as isolated_voice_db

voice_db = isolated_voice_db


@pytest.mark.parametrize("invalid", [
    None, "revision", "fence", "controller_owner", "browser_revoked", "browser_expired",
    "context", "conversation", "index", "provider", "unreceived", "other_delegation",
    "owner_reset", "context_changed", "provider_error", "duration_limit", "closing_reset",
])
async def test_closed_user_admission_requires_exact_received_request_and_all_fences(voice_db, invalid):
    created = await voice_routes.create_session(offer(voice_db), voice_db.owner)
    ctl = voice_routes.controllers[created.id]
    hooks = voice_storage.PostgresVoiceHooks()
    intent = VoiceIntent(**ctl.context.model_dump(), revision=1, delegation_id="received-task",
                         question="What deposit is documented?", transcript=[])
    if invalid != "unreceived":
        assert await hooks.claim_event(created.id, 1, "received-event", "session.delegation.created", {
            "delegation": {"id": "received-task", "target": "client"}, "offset_ms": 100,
        })
    ctl.snapshot.revision = 1
    ctl.snapshot.last_delegation_id = "received-task"
    await ctl._persist()
    await ctl.close()
    async with voice_db.session.begin() as db:
        row = await db.get(LiveSession, created.id)
        assert row.status == "closed" and row.lease_expires_at is None
        if invalid == "revision":
            row.snapshot = {**row.snapshot, "revision": 2}
        elif invalid == "fence":
            row.fence += 1
        elif invalid == "controller_owner":
            row.owner_id = "different-controller"
        elif invalid == "browser_revoked":
            (await db.get(BrowserSession, voice_db.owner.id)).revoked = True
        elif invalid == "browser_expired":
            (await db.get(BrowserSession, voice_db.owner.id)).expires_at = utcnow() - timedelta(seconds=1)
        elif invalid == "context":
            (await db.get(Conversation, voice_db.conversation.id)).context_version += 1
        elif invalid == "conversation":
            other = Conversation(session_id=voice_db.owner.id)
            db.add(other)
            await db.flush()
            intent = intent.model_copy(update={"conversation_id": other.id})
        elif invalid == "index":
            intent = intent.model_copy(update={"index_id": "different-index"})
        elif invalid == "provider":
            intent = intent.model_copy(update={"provider_session_id": "different-provider"})
        elif invalid == "other_delegation":
            intent = intent.model_copy(update={"delegation_id": "never-received-task"})
        elif invalid in {"owner_reset", "context_changed", "provider_error", "duration_limit", "closing_reset"}:
            row.snapshot = {**row.snapshot, "close_reason": invalid}
            if invalid == "closing_reset":
                row.status = "closing"
                row.lease_expires_at = utcnow() + timedelta(seconds=20)
    assert await hooks.claim_delegation(intent) is (invalid is None)
    assert not await hooks.claim_delegation(intent)  # Invalid or already admitted; never a duplicate.
    async with voice_db.session() as db:
        saved = list(await db.scalars(select(Delegation)))
        assert len(saved) == (1 if invalid is None else 0)
        if saved:
            assert saved[0].intent == intent.model_dump(mode="json")


@pytest.mark.parametrize("action", [None, "owner_reset", "context_changed"])
async def test_end_registry_retains_admission_until_background_completion_or_revocation(
    voice_db, monkeypatch, action,
):
    created = await voice_routes.create_session(offer(voice_db), voice_db.owner)
    ctl = voice_routes.controllers[created.id]
    ctl.settle_seconds = 0
    entered, release = asyncio.Event(), asyncio.Event()
    original_claim = ctl.hooks.claim_delegation

    async def claim(intent):
        entered.set()
        await release.wait()
        return await original_claim(intent)

    answer = AsyncMock(return_value=VoiceAnswer(
        run_id="declared-answer-double", spoken_text="No deposit is documented.", accepted=True,
    ))
    monkeypatch.setattr(ctl.hooks, "claim_delegation", claim)
    monkeypatch.setattr(ctl.hooks, "answer", answer)
    recovery = None
    try:
        await ctl.handle_event(transcript(text="What deposit has patient four already paid?"))
        await ctl.handle_event(delegation())
        await asyncio.wait_for(entered.wait(), 1)
        assert ctl._admitting and ctl._active_answer is None
        ended = await voice_routes.close_session(created.id, voice_db.owner)
        assert ended.status == "closed" and ended.close_reason == "user"
        assert created.id in voice_routes.controllers
        recovery = asyncio.create_task(voice_routes.recovery_loop())
        await asyncio.sleep(0.03)
        assert created.id in voice_routes.controllers and not ctl._worker.done()
        if action == "owner_reset":
            await voice_routes.close_owner_voice(voice_db.owner.id)
        elif action == "context_changed":
            await voice_routes.close_conversation_voice(voice_db.conversation.id)
        if action:
            assert ctl.snapshot.close_reason == action and ctl.snapshot.revision == 2
        release.set()
        await asyncio.wait_for(ctl._worker, 2)
        await eventually(lambda: not ctl.has_pending_work)
        if action:
            answer.assert_not_awaited()
        else:
            answer.assert_awaited_once()
            assert ctl.snapshot.last_run_id == "declared-answer-double"
        async with voice_db.session() as db:
            row = await db.get(LiveSession, created.id)
            assert row.snapshot == ctl.snapshot.model_dump(mode="json")
            assert row.status == "closed" and row.lease_expires_at is None
            assert len(list(await db.scalars(select(Delegation)))) == (0 if action else 1)
        assert not any(e["type"] == "session.commentary.append" for e in voice_db.provider.connection.sent)
        # Once work drains, a repeated End safely retires the controller.
        await voice_routes.close_session(created.id, voice_db.owner)
        assert created.id not in voice_routes.controllers
    finally:
        release.set()
        if recovery:
            recovery.cancel()
            await asyncio.gather(recovery, return_exceptions=True)
        await ctl.close("test_cleanup", cancel_work=True)
        await asyncio.gather(ctl._worker, return_exceptions=True)


@pytest.mark.parametrize("reset", [False, True])
async def test_unconfirmed_terminal_close_does_not_retire_preserved_admission(voice_db, monkeypatch, reset):
    voice_db.provider.connection = NoCloseAcknowledgment()
    monkeypatch.setattr(voice_db.provider, "hangup", AsyncMock(return_value=False))
    created = await voice_routes.create_session(offer(voice_db), voice_db.owner)
    ctl = voice_routes.controllers[created.id]
    ctl.settle_seconds = 0
    ctl.close_timeout = 0.005
    entered, release = asyncio.Event(), asyncio.Event()
    original_claim = ctl.hooks.claim_delegation

    async def claim(intent):
        entered.set()
        await release.wait()
        return await original_claim(intent)

    answer = AsyncMock(return_value=VoiceAnswer(
        run_id="declared-answer-double", spoken_text="No deposit is documented.", accepted=True,
    ))
    monkeypatch.setattr(ctl.hooks, "claim_delegation", claim)
    monkeypatch.setattr(ctl.hooks, "answer", answer)
    try:
        await ctl.handle_event(transcript())
        await ctl.handle_event(delegation())
        await asyncio.wait_for(entered.wait(), 1)
        ctl.max_duration = 0  # End reaches the existing bounded unconfirmed-cleanup limit.
        ended = await voice_routes.close_session(created.id, voice_db.owner)
        assert ended.error == "live_close_unconfirmed" and ctl._shutdown_complete
        assert created.id in voice_routes.controllers and ctl.has_pending_work
        # Repeated End must use the retained controller, not a fallback hangup
        # that increments its fence and invalidates the preserved request.
        await voice_routes.close_session(created.id, voice_db.owner)
        assert voice_routes.controllers[created.id] is ctl
        if reset:
            await voice_routes.close_owner_voice(voice_db.owner.id)
        release.set()
        await asyncio.wait_for(ctl._worker, 2)
        if reset:
            answer.assert_not_awaited()
            assert ctl.snapshot.close_reason == "owner_reset"
        else:
            answer.assert_awaited_once()
            assert ctl.snapshot.last_run_id == "declared-answer-double"
        async with voice_db.session() as db:
            saved = await db.get(LiveSession, created.id)
            assert saved.fence == 1
            assert saved.snapshot["error"] == "live_close_unconfirmed"
            assert saved.snapshot["final_usage_confirmed"] is False
    finally:
        release.set()
        await ctl.close("test_cleanup", cancel_work=True)
        await asyncio.gather(ctl._worker, return_exceptions=True)
