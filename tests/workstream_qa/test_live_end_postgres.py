"""Real Postgres Live hooks, provider/media doubles: End keeps work; reset cancels it."""

import asyncio
from types import SimpleNamespace

import pytest
from emer.api import voice_routes
from emer.contracts.answer import ProviderUsage
from emer.domain.dialogue_state import build_dialogue_state
from emer.services import voice_storage
from emer.storage.models import BrowserSession, Run
from sqlalchemy import select
from test_voice import delegation, transcript
from test_voice_postgres import offer
from test_voice_postgres import voice_db as isolated_voice_db

voice_db = isolated_voice_db


@pytest.mark.parametrize("action", ["end", "reset"])
async def test_acknowledged_end_preserves_intent_resolution_but_reset_revokes_it(voice_db, monkeypatch, action):
    started, release = asyncio.Event(), asyncio.Event()
    created_runs = []

    async def context(db, conversation_id, context_version):
        state = build_dialogue_state(
            [{"id": "prior", "status": "completed", "question": "Summarize PT-006.",
              "answer": {}, "context": {}}],
            conversation_id=conversation_id, context_version=context_version,
        )
        return {"previous_questions": ["Summarize PT-006."], "patient_id": "PT-006",
                "dialogue_state": state.model_dump()}

    monkeypatch.setattr(voice_storage, "recent_intent_context", context)

    class GatedResolver:
        def __init__(self, *args, **kwargs):
            pass

        async def structured(self, instructions, payload, schema, **kwargs):
            started.set()
            await release.wait()
            return SimpleNamespace(
                value=schema(status="resolved", base_reference_id=None,
                             bindings=[{"mention": "their", "target": "PT-006",
                                        "reference_id": "prior:user"}]),
                usage=ProviderUsage(operation="voice_intent_resolution", model="qa-double", latency_ms=1),
            )

    async def save_completed_run(owner, conversation, body, **kwargs):
        # Generation is replaced, but the mapped background run is really committed.
        async with voice_db.session.begin() as db:
            row = Run(
                session_id=owner, conversation_id=conversation, index_id="index-test",
                idempotency_key=body.idempotency_key, body_hash="qa-lifecycle-only",
                question=body.question, context_version=body.context_version, status="completed",
                answer={"statements": [{"text": "QA lifecycle result; no model generation."}],
                        "gaps": [], "next_steps": []},
            )
            db.add(row)
            await db.flush()
        created_runs.append(row.id)
        return row

    monkeypatch.setattr(voice_storage, "TrackedOpenRouterClient", GatedResolver)
    monkeypatch.setattr(voice_storage.runs, "create_run", save_completed_run)
    created = await voice_routes.create_session(offer(voice_db), voice_db.owner)
    ctl = voice_routes.controllers[created.id]
    ctl.settle_seconds = 0
    try:
        await ctl.handle_event(transcript(text="Summarize their status."))
        await ctl.handle_event(delegation())
        await asyncio.wait_for(started.wait(), 2)
        if action == "reset":
            async with voice_db.session.begin() as db:
                (await db.get(BrowserSession, voice_db.owner.id)).revoked = True
            await ctl.close("session_reset", cancel_work=True)
        else:
            await ctl.close("user", cancel_work=False)
        assert ctl.snapshot.final_usage_confirmed and ctl.snapshot.status == "closed"
        release.set()
        await asyncio.wait_for(asyncio.shield(ctl._worker), 2)
        assert len(created_runs) == (1 if action == "end" else 0), (
            "Provider close acknowledgment must not erase the app's request to keep background work"
        )
        async with voice_db.session() as db:
            assert len(list(await db.scalars(select(Run)))) == len(created_runs)
        assert not any(e["type"] == "session.commentary.append" for e in voice_db.provider.connection.sent)
    finally:
        release.set()
        if ctl._worker and not ctl._worker.done():
            await ctl.close("test_cleanup", cancel_work=True)
