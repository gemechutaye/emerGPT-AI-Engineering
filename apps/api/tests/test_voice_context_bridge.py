"""Typed history reaches Live startup and server-side resolution, using real isolated Postgres."""

from hashlib import sha256

import pytest
from emer.api import voice_routes
from emer.services.intent_context import live_startup_history
from emer.storage.models import Conversation, LiveSession, Run
from test_discovery_context import discovery as isolated_discovery
from test_voice_postgres import offer
from test_voice_postgres import voice_db as isolated_voice_db

voice_db = isolated_voice_db
source_discovery = isolated_discovery


async def seed_question(fixture, question, *, version=1, answer=None, evidence=None):
    async with fixture.session.begin() as db:
        db.add(
            Run(
                session_id=fixture.owner.id,
                conversation_id=fixture.conversation.id,
                index_id="index-test",
                idempotency_key="saved-typed-question",
                body_hash="fixture",
                question=question,
                context={"resolved_question": question},
                context_version=version,
                status="completed",
                answer=answer or {"status": "answered"},
                evidence=evidence,
            )
        )


@pytest.mark.parametrize("same_version", [True, False])
async def test_voice_startup_inherits_only_current_saved_typed_context(voice_db, same_version):
    await seed_question(voice_db, "Summarize patient six", version=1 if same_version else 2)
    body = offer(voice_db).model_copy(update={"automatic_context": True})
    created = await voice_routes.create_session(body, voice_db.owner)
    async with voice_db.session() as db:
        row = await db.get(LiveSession, created.id)
        assert row.context["patient_id"] == ("PT-006" if same_version else None)
        assert row.context["previous_questions"] == (["Summarize patient six"] if same_version else [])
    history = voice_db.provider.history
    if same_version:
        assert history[0]["content"][0]["text"] == "Summarize patient six"
        assert "PT-006" in history[-1]["content"][0]["text"]
    else:
        assert history is None


async def test_source_validated_discovery_is_available_when_switching_to_voice(voice_db, source_discovery):
    # Rebuilt indexes need their own consistent source fixture, not an archived answer's old index ID.
    answer, packet = source_discovery
    await seed_question(
        voice_db, packet.scopes[0].question, answer=answer.model_dump(mode="json"), evidence=packet.model_dump(mode="json")
    )
    created = await voice_routes.create_session(
        offer(voice_db).model_copy(update={"automatic_context": True}), voice_db.owner
    )
    async with voice_db.session() as db:
        assert (await db.get(LiveSession, created.id)).context["patient_id"] == "PT-003"
    assert "PT-003" in voice_db.provider.history[-1]["content"][0]["text"]


@pytest.mark.parametrize("automatic", [True, False])
async def test_automatic_voice_ignores_hidden_legacy_patient_and_date_pins(voice_db, automatic):
    async with voice_db.session.begin() as db:
        conversation = await db.get(Conversation, voice_db.conversation.id)
        conversation.patient_id, conversation.as_of = "PT-005", "2026-03-01"
    created = await voice_routes.create_session(
        offer(voice_db).model_copy(update={"automatic_context": automatic}), voice_db.owner
    )
    async with voice_db.session() as db:
        context = (await db.get(LiveSession, created.id)).context
        assert context["patient_id"] == (None if automatic else "PT-005")
        assert context["as_of"] == (None if automatic else "2026-03-01")


async def test_preexisting_voice_idempotency_hash_remains_retrievable(voice_db):
    body = offer(voice_db)
    created = await voice_routes.create_session(body, voice_db.owner)
    old_hash = sha256(
        body.model_dump_json(exclude={"idempotency_key", "automatic_context"}).encode()
    ).hexdigest()
    async with voice_db.session() as db:
        assert (await db.get(LiveSession, created.id)).body_hash == old_hash
    assert (await voice_routes.create_session(body, voice_db.owner)).id == created.id


def test_startup_history_is_bounded_and_does_not_truncate_questions():
    text = "界" * 1500
    history = live_startup_history({"previous_questions": ["old", text, "latest"]})
    assert [row["content"][0]["text"] for row in history[:-1]] == ["old", text, "latest"]
    history = live_startup_history({"previous_questions": ["old", "界" * 3000, "latest"]})
    assert [row["content"][0]["text"] for row in history[:-1]] == ["latest"]
