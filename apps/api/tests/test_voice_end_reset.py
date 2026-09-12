"""Real isolated Postgres fencing after End; resolver and media are declared doubles."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from emer.api import app as app_routes
from emer.api import voice_routes
from emer.contracts.answer import ProviderUsage
from emer.contracts.http import ContextUpdate
from emer.domain.dialogue_state import build_dialogue_state
from emer.services import voice_storage
from emer.storage.models import BrowserSession, Conversation, LiveSession, Run
from fastapi import Request, Response
from sqlalchemy import select
from test_voice import delegation, eventually, transcript
from test_voice_postgres import offer
from test_voice_postgres import voice_db as isolated_voice_db

voice_db = isolated_voice_db


@pytest.fixture(autouse=True)
def declared_prior_intent(monkeypatch):
    async def context(db, conversation_id, context_version):
        state = build_dialogue_state([{"id": "prior", "status": "completed", "question": "Summarize PT-006.",
                                      "answer": {}, "context": {}}], conversation_id=conversation_id,
                                     context_version=context_version)
        return {"previous_questions": ["Summarize PT-006."], "patient_id": "PT-006", "dialogue_state": state.model_dump()}
    monkeypatch.setattr(voice_storage, "recent_intent_context", context)



async def test_unchecked_resolver_clarification_cannot_publish_facts(voice_db, monkeypatch):
    clarification = "The fee is $900 and PT-006 is cleared. Which month did you mean?"

    class Resolver:
        def __init__(self, *args, **kwargs):
            pass

        async def structured(self, instructions, payload, schema, **kwargs):
            assert "not a rewritten question" in instructions
            return SimpleNamespace(
                value=schema(status="resolved", base_reference_id=None,
                             bindings=[{"mention": "that policy", "target": clarification, "reference_id": "prior:user"}]),
                usage=ProviderUsage(operation="voice_intent_resolution", model="test-double", latency_ms=1),
            )

    monkeypatch.setattr(voice_storage, "TrackedOpenRouterClient", Resolver)
    created = await voice_routes.create_session(offer(voice_db), voice_db.owner)
    ctl = voice_routes.controllers[created.id]
    ctl.settle_seconds = 0
    try:
        await ctl.handle_event(transcript(text="What was that policy?"))
        await ctl.handle_event(delegation())
        await eventually(lambda: any(
            event["type"] == "session.commentary.append"
            for event in voice_db.provider.connection.sent
        ))
        messages = [
            event["content"] for event in voice_db.provider.connection.sent
            if event["type"] == "session.commentary.append"
        ]
        assert messages == ["Could you restate the complete question, including the patient or policy date you mean?"]
        assert "$900" not in messages[0] and "cleared" not in messages[0]
    finally:
        await ctl.close()


@pytest.mark.parametrize("action", ["owner_reset", "context_changed"])
async def test_end_then_reset_or_context_change_fences_a_cancellation_resistant_resolver(
    voice_db, monkeypatch, action,
):
    # This case exercises anonymous-browser revocation. Shared-demo reset intentionally
    # retains the shared owner and is covered by the separate shared-workspace tests.
    monkeypatch.setattr(app_routes.settings, "shared_workspace", False)
    started, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    cancellation_count = 0
    resolver_calls = 0

    class GatedResolver:
        def __init__(self, *args, **kwargs):
            assert kwargs["live_revision"] == 1
            assert kwargs["live_context_version"] == 1
            assert kwargs["live_fence"] == 1

        async def structured(self, instructions, payload, schema, **kwargs):
            nonlocal cancellation_count, resolver_calls
            resolver_calls += 1
            started.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                # Returning a late result despite cancellation makes the persisted
                # fence, rather than cooperative cancellation, enforce safety.
                cancellation_count += 1
                cancelled.set()
                await release.wait()
            return SimpleNamespace(
                value=schema(status="resolved", base_reference_id=None,
                             bindings=[{"mention": "their", "target": "PT-006", "reference_id": "prior:user"}]),
                usage=ProviderUsage(operation="voice_intent_resolution", model="test-double", latency_ms=1),
            )

    async def bundle():
        return SimpleNamespace(documents=[])

    create_run = AsyncMock(side_effect=AssertionError("A revoked voice intent reached run creation"))
    monkeypatch.setattr(voice_storage, "TrackedOpenRouterClient", GatedResolver)
    monkeypatch.setattr(voice_storage.runs, "create_run", create_run)
    monkeypatch.setattr(app_routes, "Session", voice_db.session)
    monkeypatch.setattr(app_routes, "load_bundle", bundle)
    created = await voice_routes.create_session(offer(voice_db), voice_db.owner)
    ctl = voice_routes.controllers[created.id]
    ctl.settle_seconds = 0
    answer_task = None
    try:
        await ctl.handle_event(transcript(text="Summarize their status."))
        await ctl.handle_event(delegation())
        await asyncio.wait_for(started.wait(), 2)
        answer_task = ctl._active_answer
        ended = await voice_routes.close_session(created.id, voice_db.owner)
        ended_revision = ended.revision
        assert ended.status == "closed" and ended.final_usage_confirmed
        assert ended.close_reason == "user" and ended.provider_close_reason == "close_requested"
        assert answer_task is not None and not answer_task.done()
        assert not cancelled.is_set() and created.id in voice_routes.controllers

        if action == "owner_reset":
            replacement = await app_routes.reset(
                Request({"type": "http", "headers": []}), Response(), owner=voice_db.owner,
            )
            assert replacement.id != voice_db.owner.id
        else:
            updated = await app_routes.update_context(
                voice_db.conversation.id,
                ContextUpdate(expected_version=1, as_of="2026-09-12"),
                owner=voice_db.owner,
            )
            assert updated.context_version == 2
        await asyncio.wait_for(cancelled.wait(), 0.5)
        assert not answer_task.done()
        assert cancellation_count == 1

        async with voice_db.session() as db:
            saved = await db.get(LiveSession, created.id)
            assert saved.status == "closed"
            assert saved.snapshot["close_reason"] == action
            assert saved.snapshot["provider_close_reason"] == "close_requested"
            assert saved.snapshot["revision"] == ended_revision + 1
            if action == "owner_reset":
                assert (await db.get(BrowserSession, voice_db.owner.id)).revoked
            else:
                assert (await db.get(Conversation, voice_db.conversation.id)).context_version == 2

        release.set()
        await asyncio.wait_for(asyncio.gather(answer_task, ctl._worker, return_exceptions=True), 2)
        assert answer_task.cancelled() and ctl._active_answer is None
        create_run.assert_not_awaited()
        assert resolver_calls == 1 and cancellation_count == 1
        async with voice_db.session() as db:
            assert list(await db.scalars(select(Run))) == []
        assert not any(e["type"] == "session.commentary.append" for e in voice_db.provider.connection.sent)
    finally:
        release.set()
        await ctl.close("test_cleanup", cancel_work=True)
        if answer_task is not None:
            await asyncio.wait_for(asyncio.gather(answer_task, ctl._worker, return_exceptions=True), 2)
