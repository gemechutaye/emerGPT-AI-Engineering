"""Spoken patient references through the Live hook reach the same run pipeline as typed questions.

Real isolated Postgres; the intent resolver, run service and media are declared doubles.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from emer.api import voice_routes
from emer.contracts.answer import ProviderUsage
from emer.services import voice_storage
from emer.storage.models import Run
from test_voice import delegation, transcript
from test_voice_postgres import offer
from test_voice_postgres import voice_db as isolated_voice_db

voice_db = isolated_voice_db


def resolver(question):
    class Resolver:
        def __init__(self, *args, **kwargs):
            pass

        async def structured(self, instructions, payload, schema, **kwargs):
            return SimpleNamespace(
                value=schema(question=question, clarification=None),
                usage=ProviderUsage(operation="voice_intent_resolution", model="test-double", latency_ms=1),
            )

    return Resolver


async def spoken(voice_db, monkeypatch, said: str, resolved: str):
    """Return (create_run mock, commentary sent) after one spoken delegation."""

    async def fake_create_run(owner_id, conversation_id, body, index_id=None, *, intent_resolved=False):
        assert intent_resolved
        async with voice_db.session.begin() as db:
            run = Run(
                session_id=owner_id, conversation_id=conversation_id, index_id=index_id,
                idempotency_key=body.idempotency_key, body_hash="x", question=body.question,
                context_version=body.context_version, status="completed",
                answer={"status": "answered", "statements": [{"text": "Verified statement."}], "gaps": [], "next_steps": []},
            )
            db.add(run)
            await db.flush()
            return SimpleNamespace(id=run.id, question=body.question)

    create_run = AsyncMock(side_effect=fake_create_run)
    monkeypatch.setattr(voice_storage, "TrackedOpenRouterClient", resolver(resolved))
    monkeypatch.setattr(voice_storage.runs, "create_run", create_run)
    created = await voice_routes.create_session(offer(voice_db), voice_db.owner)
    ctl = voice_routes.controllers[created.id]
    ctl.settle_seconds = 0
    await ctl.handle_event(transcript(text=said))
    await ctl.handle_event(delegation())
    async with asyncio.timeout(3):
        while ctl.snapshot.status != "listening" or ctl._active_answer is not None:
            await asyncio.sleep(0.01)
    sent = [e["content"] for e in voice_db.provider.connection.sent if e["type"] == "session.commentary.append"]
    return create_run, sent


async def test_spoken_patient_six_is_answered_not_clarified(voice_db, monkeypatch):
    create_run, sent = await spoken(
        voice_db, monkeypatch, "um, tell me about patient six please", "What is PT-006's current status?"
    )
    create_run.assert_awaited_once()
    assert create_run.await_args.args[2].question == "um, tell me about PT-006 please"
    assert sent == ["Verified statement."]


async def test_spoken_digits_count_as_observed(voice_db, monkeypatch):
    create_run, sent = await spoken(
        voice_db, monkeypatch, "what about P T zero zero four's follow up", "What is PT-004's follow-up interval?"
    )
    create_run.assert_awaited_once()
    assert sent == ["Verified statement."]


@pytest.mark.parametrize(
    ("said", "resolved"),
    [
        ("tell me about patient six", "What is PT-005's current status?"),
        ("what is the cancellation window", "Is a 72 hour cancellation window required?"),
    ],
)
async def test_self_contained_speech_cannot_be_rewritten_by_unneeded_model(voice_db, monkeypatch, said, resolved):
    create_run, sent = await spoken(voice_db, monkeypatch, said, resolved)
    create_run.assert_awaited_once()
    from emer.domain.scope import canonicalize_patient_mentions
    assert create_run.await_args.args[2].question == canonicalize_patient_mentions(said)
    assert resolved != create_run.await_args.args[2].question
    assert sent == ["Verified statement."]
