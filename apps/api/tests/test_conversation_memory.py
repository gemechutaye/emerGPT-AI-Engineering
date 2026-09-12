"""Finite metadata work on isolated Postgres; declared HTTP fixtures, never paid inference."""

import asyncio
import json
from datetime import timedelta
from hashlib import sha256
from types import SimpleNamespace
from uuid import uuid4

import httpx
import psycopg
import pytest
from emer.contracts.conversations import ConversationUpdate, MetadataDraft
from emer.contracts.http import ConversationView
from emer.contracts.voice import VoiceAnswer, VoiceIntent, VoiceSnapshot
from emer.providers.openrouter import RATE_LIMIT_BACKOFF_SECONDS, ProviderError
from emer.services import conversation_memory as memory
from emer.services import conversations, provider_activity, runs, voice_storage
from emer.settings import settings
from emer.storage import retention
from emer.storage.models import (
    Base,
    BrowserSession,
    Conversation,
    ConversationShare,
    CorpusIndex,
    LiveEvent,
    LiveSession,
    ProviderAttempt,
    Run,
    utcnow,
)
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


def completion(content=None, *, status=200, model="openai/gpt-4.1-nano"):
    if status != 200:
        return httpx.Response(status, json={"error": {"message": "fixture provider failure"}})
    return httpx.Response(200, json={
        "id": "metadata-fixture-request", "model": model, "provider": "fixture",
        "usage": {"prompt_tokens": 120, "completion_tokens": 45, "total_tokens": 165, "cost": 0.00003},
        "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(content or {
            "title": "Cancellation Rules and Remaining Questions",
            "introduction": "The conversation discussed cancellation policy.",
            "points": ["The user asked about cancellation rules.", "The saved answer left exact costs unknown."],
        })}}],
    })


def selection_completion(request, *, title="Cancellation Rules and Remaining Questions", unit_ids=None):
    payload = json.loads(json.loads(request.content)["messages"][1]["content"])
    if unit_ids is None:
        units = payload["recap_units"]
        answers = [unit for unit in units if unit["kind"] in {"answer", "limitation", "next_step"}]
        others = [unit for unit in units if unit not in answers]
        unit_ids = [unit["unit_id"] for unit in (answers + others)[:2]]
    return completion({"title": title, "unit_ids": unit_ids})


def fidelity_completion(request, *, unsupported=None):
    body = json.loads(request.content)
    payload = json.loads(body["messages"][1]["content"])
    items = [*([("title", 0)] if "title" in payload["candidate"] else []), ("introduction", 0),
             *(("point", index) for index in range(len(payload["candidate"]["points"])))]
    return completion({"verdicts": [
        {"item": item, "index": index, "supported": (item, index) != unsupported,
         "reason": "Declared fixture verdict; no real model assessment."}
        for item, index in items
    ]}, model=body["model"])


@pytest.mark.parametrize("title,introduction,points", [
    ("One two three four five six seven eight nine", "A short recap.", ["One", "Two"]),
    ("What did we discuss?", "A short recap.", ["One", "Two"]),
    ("Topic", "A short recap.", ["One"]),
    ("Topic", "A short recap.", ["One", "Two", "Three", "Four", "Five"]),
    ("Topic", " ", ["One", "Two"]),
    ("Topic", "A short recap.", ["One", " "]),
])
def test_presentation_output_is_bounded_and_topic_shaped(title, introduction, points):
    with pytest.raises(ValidationError):
        MetadataDraft(title=title, introduction=introduction, points=points)


def test_structured_recap_serializes_to_the_existing_saved_summary_without_inventing_points():
    draft = MetadataDraft(title="Recovery Questions", introduction="The discussion covered recovery.",
                          points=["The user asked about duration.", "The assistant said it remains unconfirmed."])
    assert draft.summary == (
        "The discussion covered recovery.\n- The user asked about duration.\n"
        "- The assistant said it remains unconfirmed."
    )
    assert len(draft.summary) <= 1200
    longest = MetadataDraft(title="Bounded recap", introduction="i" * 240, points=["p" * 220] * 4)
    assert len(longest.summary) <= 1200


@pytest.mark.parametrize("job_status", ["cancelled", "failed", "completed"])
def test_metadata_projection_separates_empty_jobs_from_actual_clinical_status_losslessly(job_status):
    content = {
        "saved_answers": [
            {"question": "What is the current plan?", "status": job_status,
             "answer": "", "created_at": "2026-09-12T09:00:00Z"},
            {"question": "What happened to the appointment?", "status": "completed",
             "answer": "The saved record says the appointment was cancelled; follow-up is planned in 6 weeks.",
             "created_at": "2026-09-12T09:01:00Z"},
        ],
        "voice_transcript": [{"speaker": "user", "text": "No, I meant the later appointment."}],
    }
    unchanged = json.loads(json.dumps(content))
    projected = memory.metadata_content(content)
    assert content == unchanged
    assert "saved_answers" not in projected
    jobs = projected["answer_jobs"]
    assert jobs[0]["answer_job_status"] == job_status and jobs[0]["answer"] == ""
    assert jobs[1]["answer_job_status"] == "completed"
    assert jobs[1]["answer"] == content["saved_answers"][1]["answer"]
    assert all("status" not in job for job in jobs)
    # Inverting only the labels recovers every input value, including empty
    # answers, genuine clinical cancellation text, corrections and timestamps.
    restored = {
        **{key: value for key, value in projected.items()
           if key not in {"answer_jobs", "answer_job_semantics"}},
        "saved_answers": [
            {"status": job["answer_job_status"],
             **{key: value for key, value in job.items() if key != "answer_job_status"}}
            for job in jobs
        ],
    }
    assert restored == content


@pytest.fixture
async def memory_db(monkeypatch):
    database = "emer_memory_test_" + uuid4().hex[:12]
    with psycopg.connect("postgresql://localhost:55432/postgres", autocommit=True) as admin:
        admin.execute(f"CREATE DATABASE {database}")
    engine = create_async_engine(f"postgresql+psycopg://localhost:55432/{database}")
    session = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    for module in (memory, conversations, provider_activity, voice_storage, runs, retention):
        monkeypatch.setattr(module, "Session", session)
    monkeypatch.setattr(settings, "openrouter_api_key", "fixture-key")
    monkeypatch.setattr(settings, "conversation_metadata_model", "openai/gpt-4.1-nano")
    monkeypatch.setattr(settings, "openrouter_model", "openai/gpt-5.6-luna")
    queued = []
    monkeypatch.setattr(memory, "dispatch", queued.append)
    async with session.begin() as db:
        owner = BrowserSession(token_hash=uuid4().hex, expires_at=utcnow() + timedelta(hours=1))
        db.add(owner)
        db.add(CorpusIndex(id="memory-index", checksum="memory-checksum", corpus_checksum="memory-corpus",
                           bundle=b"fixture", manifest={}))
        await db.flush()
        conversation = Conversation(session_id=owner.id)
        db.add(conversation)
        await db.flush()
        run = Run(
            session_id=owner.id, conversation_id=conversation.id, index_id="memory-index",
            idempotency_key=str(uuid4()), body_hash="fixture", question="What cancellation rules apply?",
            context_version=1, status="completed",
            answer={"status": "partial", "statements": [{"text": "The saved policy answer."}],
                    "gaps": [{"text": "Exact costs are not established."}], "next_steps": []},
        )
        live = LiveSession(
            session_id=owner.id, conversation_id=conversation.id, index_id="memory-index",
            context_version=1, context={}, idempotency_key=str(uuid4()), body_hash="fixture",
            status="closed", owner_id=voice_storage.VOICE_OWNER, fence=1,
            snapshot={"revision": 1, "close_reason": "user"},
        )
        db.add_all([run, live])
        await db.flush()
        for kind, text in [
            ("session.input_transcript.delta", "Actually I meant September."),
            ("session.output_transcript.delta", "We discussed the later policy, not a treatment recommendation."),
        ]:
            db.add(LiveEvent(live_session_id=live.id, provider_event_id=str(uuid4()), kind=kind,
                             payload={"delta": text, "start_ms": 0, "end_ms": 10}))
    fixture = SimpleNamespace(
        session=session, owner=owner, conversation=conversation, run=run, live=live, queued=queued,
        calls=[], handler=None, check_handler=None,
    )

    async def respond(request):
        body = json.loads(request.content)
        fixture.calls.append(body)
        if body["response_format"]["json_schema"]["name"] == "RecapFidelityCheck":
            if fixture.check_handler:
                return await fixture.check_handler(request)
            return fidelity_completion(request)
        if fixture.handler:
            return await fixture.handler(request)
        return selection_completion(request)

    client_type = memory.MetadataClient
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as transport:
        monkeypatch.setattr(memory, "MetadataClient",
                            lambda row, *, activity_version, model=None: client_type(
                                row, activity_version=activity_version, model=model, client=transport))
        try:
            yield fixture
        finally:
            await memory.shutdown()
            await provider_activity.drain_provider_receipts()
    await engine.dispose()
    with psycopg.connect("postgresql://localhost:55432/postgres", autocommit=True) as admin:
        admin.execute(f"DROP DATABASE {database} WITH (FORCE)")


async def stored(fixture):
    async with fixture.session() as db:
        return await db.get(Conversation, fixture.conversation.id)


async def request_and_execute(fixture):
    await memory.request_summary(fixture.owner.id, fixture.conversation.id)
    await memory.execute(fixture.conversation.id)


async def test_real_tracked_call_uses_both_transcript_sides_and_saved_answer_once(memory_db):
    f = memory_db
    await request_and_execute(f)
    row = await stored(f)
    assert row.summary_status == "ready" and row.summary_error is None
    assert row.title == "Cancellation Rules and Remaining Questions" and row.title_generated
    assert row.title != f.run.question and len(row.title.split()) <= 8
    assert len(f.calls) == 2 and f.calls[0]["model"] == "openai/gpt-4.1-nano"
    assert f.calls[0]["max_completion_tokens"] == 650
    assert "max_tokens" not in f.calls[0]
    assert f.calls[0]["provider"]["only"] == ["azure"]
    assert f.calls[0]["provider"]["data_collection"] == "deny"
    payload = json.loads(f.calls[0]["messages"][1]["content"])
    assert payload["answer_jobs"][0]["answer_job_status"] == "completed"
    assert payload["answer_jobs"][0]["answer"] == (
        "The saved policy answer.\n\nUncertainty / not established:\nExact costs are not established."
    )
    assert [part["speaker"] for part in payload["voice_transcript"]] == ["user", "assistant"]
    assert "September" in payload["voice_transcript"][0]["text"]
    assert "UNTRUSTED DATA" in f.calls[0]["messages"][0]["content"]
    checked_payload = json.loads(f.calls[1]["messages"][1]["content"])
    assert checked_payload["conversation"] == payload
    assert f.calls[1]["model"] == "openai/gpt-5.6-luna"
    assert f.calls[1]["max_completion_tokens"] == 1200
    async with f.session() as db:
        attempts = list(await db.scalars(select(ProviderAttempt).order_by(ProviderAttempt.created_at)))
        assert [attempt.operation for attempt in attempts] == ["conversation_metadata", "conversation_metadata_check"]
        assert all(attempt.status == "completed" for attempt in attempts)
        assert all(attempt.metadata_job_id == row.summary_job_id for attempt in attempts)
        attempt = attempts[0]
        assert attempt.conversation_id == row.id and attempt.metadata_job_id == row.summary_job_id
        assert attempt.operation == "conversation_metadata" and attempt.status == "completed"
        assert attempt.usage["total_tokens"] == 165 and attempt.usage["cost"] == 0.00003
    for _ in range(3):
        assert (await memory.request_summary(f.owner.id, row.id)).summary_status == "ready"
        await memory.schedule_auto(row.id, f.owner.id, voice_ended=True)
        await memory.recover()
    assert len(f.calls) == 2 and len(f.queued) == 1


@pytest.mark.parametrize("source,claim", [
    (
        "For treatment consideration, the general overview says the provider considers current skin condition and history.",
        "The provider reviewed patient's skin condition, medical history, and other factors influencing treatment suitability.",
    ),
    ("Prior filler history is uncertain.", "The patient has never received filler."),
    ("No treatment price or absolute follow-up date is supplied.", "The treatment costs $250 and follow-up is overdue."),
    ("The user asked whether a provider had cleared treatment.", "The provider cleared treatment."),
    ("", "The patient's follow-up was cancelled and is unavailable."),
    (
        "The patient is interested in scar procedures, which should be considered only after active acne is controlled.",
        "The patient is awaiting scar procedures.",
    ),
    (
        ("General short-term effects can include redness, mild swelling, pinpoint crusting, tenderness, "
         "and temporary dryness."),
        "The saved answer listed redness, swelling, crusting, tenderness, and dryness.",
    ),
    (
        "General effects can include slight bruising and temporary numbness; no patient symptoms are documented.",
        "The patient experienced bruising and numbness.",
    ),
])
async def test_freeform_claims_cannot_enter_the_excerpt_selector_or_publish_a_title(memory_db, source, claim):
    """Factual rewriting is structurally impossible even if a checker would accept it."""
    f = memory_db
    async with f.session.begin() as db:
        run = await db.get(Run, f.run.id)
        run.answer = {"status": "answered", "statements": [{"text": source}]} if source else None
        if not source:
            run.status = "cancelled"
    candidate = {"title": "Recorded Treatment Discussion", "introduction": "The user discussed treatment.",
                 "points": ["The user asked about treatment.", claim]}

    async def generate(request):
        return completion(candidate)

    async def must_not_check(request):
        pytest.fail("Freeform factual output must fail the selection schema before fidelity checking")

    f.handler, f.check_handler = generate, must_not_check
    await request_and_execute(f)
    row = await stored(f)
    assert row.summary_status == "failed" and row.summary is None
    assert row.title == "New conversation" and not row.title_generated
    await memory.schedule_auto(row.id, f.owner.id, voice_ended=True)
    await memory.recover()
    assert len(f.calls) == 1
    async with f.session() as db:
        attempts = list(await db.scalars(select(ProviderAttempt)))
        assert len(attempts) == 1 and attempts[0].status == "failed"
        assert attempts[0].operation == "conversation_metadata" and attempts[0].usage["cost"] == 0.00003


async def test_exact_recap_keeps_full_qualified_medical_details_and_canonical_answer(memory_db):
    """No summarizer/checker judgment can remove a qualification from the selected unit."""
    f = memory_db
    source = (
        "General short-term effects can include redness, mild swelling, pinpoint crusting, tenderness, "
        "and temporary dryness. No exact procedure price is supplied."
    )
    async with f.session.begin() as db:
        run = await db.get(Run, f.run.id)
        run.question = "What possible effects are listed and what exact procedure price is supplied?"
        run.answer = {"status": "partial", "statements": [{"text": source}]}

    async def generate(request):
        content = json.loads(json.loads(request.content)["messages"][1]["content"])
        assert content["answer_jobs"][0]["answer"] == source
        return selection_completion(request, title="Effects and Pricing Discussion")

    async def check(request):
        content = json.loads(json.loads(request.content)["messages"][1]["content"])
        assert content["conversation"]["answer_jobs"][0]["answer"] == source
        assert content["candidate"]["points"][0] == f"Saved answer to question 1: “{source}”"
        return fidelity_completion(request)

    f.handler, f.check_handler = generate, check
    await request_and_execute(f)
    row = await stored(f)
    assert row.summary_status == "ready" and source in row.summary
    async with f.session() as db:
        run = await db.get(Run, f.run.id)
        assert run.answer["statements"][0]["text"] == source
    assert len(f.calls) == 2


@pytest.mark.parametrize("faithful", [False, True])
async def test_provider_note_and_unanswered_voice_followup_keep_their_original_status(memory_db, faithful):
    """Exact source units retain the note attribution and the user's unfinished follow-up."""
    f = memory_db
    source = (
        "PT-003 is the patient whose record documents a provider note to postpone the light-based "
        "procedure discussion until recent sun exposure is reassessed."
    )
    user_text = " It is not documented Um, could you tell me the current status and follow-up for patient six, please"
    async with f.session.begin() as db:
        run = await db.get(Run, f.run.id)
        run.question = "Which patient had their light-based procedure discussion postponed after recent sun exposure?"
        run.answer = {"status": "answered", "statements": [{"text": source}]}
        for event in await db.scalars(select(LiveEvent).where(LiveEvent.live_session_id == f.live.id)):
            value = user_text if event.kind == "session.input_transcript.delta" else " I'll check."
            event.payload = {"delta": value, "start_ms": 0, "end_ms": 10}

    async def generate(request):
        payload = json.loads(json.loads(request.content)["messages"][1]["content"])
        assert payload["answer_jobs"][0]["answer"] == source
        assert [turn["text"] for turn in payload["voice_transcript"]] == [user_text, " I'll check."]
        if not faithful:
            return completion({"title": "Postponed Procedure", "introduction": "The procedure was postponed.",
                               "points": ["The assistant confirmed postponement.", "The assistant completed a check-in."]})
        selected = [unit["unit_id"] for unit in payload["recap_units"]
                    if unit["kind"] == "answer" or (unit["kind"] == "voice" and unit["speaker"] == "user")]
        return selection_completion(request, title="Procedure Discussion and Follow-Up Questions", unit_ids=selected)

    async def check(request):
        payload = json.loads(json.loads(request.content)["messages"][1]["content"])
        assert faithful, "Freeform factual claims cannot reach the fidelity checker"
        assert payload["candidate"]["points"] == [
            f"Saved answer to question 1: “{source}”", f"You said, voice turn 1: “{user_text}”",
        ]
        return fidelity_completion(request)

    f.handler, f.check_handler = generate, check
    await request_and_execute(f)
    row = await stored(f)
    if faithful:
        assert row.summary_status == "ready" and source in row.summary and user_text in row.summary
        assert len(f.calls) == 2
    else:
        assert row.summary_status == "failed" and row.summary is None and len(f.calls) == 1
        assert row.title == "New conversation" and not row.title_generated


@pytest.mark.parametrize("bad_verdict", ["missing", "duplicate", "unknown_index", "unsupported_title"])
def test_every_recap_item_requires_one_supported_verdict(bad_verdict):
    candidate = MetadataDraft(title="Recorded Discussion", introduction="The discussion concerned recovery.",
                              points=["The user asked about duration.", "The answer left it unknown."])
    verdicts = [
        {"item": item, "index": index, "supported": True, "reason": "Fixture support."}
        for item, index in [("title", 0), ("introduction", 0), ("point", 0), ("point", 1)]
    ]
    if bad_verdict == "missing":
        verdicts.pop()
    elif bad_verdict == "duplicate":
        verdicts[-1] = dict(verdicts[-2])
    elif bad_verdict == "unknown_index":
        verdicts[-1]["index"] = 3
    else:
        verdicts[0]["supported"] = False
    with pytest.raises(ProviderError, match="faithfully preserve"):
        memory.validate_fidelity(candidate, memory.RecapFidelityCheck(verdicts=verdicts))


@pytest.mark.parametrize("title_state", ["generated", "manual", "generated_during_request", "manual_during_request"])
async def test_unused_generated_title_is_omitted_from_fidelity_and_never_overwrites_existing_title(memory_db, title_state):
    f = memory_db
    existing_title = "The saved title"

    async def set_title():
        async with f.session.begin() as db:
            row = await db.get(Conversation, f.conversation.id)
            row.title = existing_title
            row.title_generated = title_state.startswith("generated")
            row.title_origin = "auto" if title_state.startswith("generated") else "manual"
            row.title_revision += 1

    if "during_request" not in title_state:
        await set_title()

    async def generate(request):
        if "during_request" in title_state:
            await set_title()
        return selection_completion(request, title="Unsupported Appointment Was Completed")

    async def check(request):
        payload = json.loads(json.loads(request.content)["messages"][1]["content"])
        assert "title" not in payload["candidate"]
        assert "Unsupported Appointment" not in json.dumps(payload)
        return fidelity_completion(request)

    f.handler, f.check_handler = generate, check
    await request_and_execute(f)
    row = await stored(f)
    assert row.summary_status == "ready" and row.title == existing_title
    assert "Unsupported Appointment" not in row.summary
    assert len(f.calls) == 2


async def test_recap_points_still_require_fidelity_with_an_existing_title(memory_db):
    f = memory_db
    async with f.session.begin() as db:
        row = await db.get(Conversation, f.conversation.id)
        row.title, row.title_generated = "Existing topic", True

    async def reject(request):
        payload = json.loads(json.loads(request.content)["messages"][1]["content"])
        assert "title" not in payload["candidate"]
        return fidelity_completion(request, unsupported=("point", 1))

    f.check_handler = reject
    await request_and_execute(f)
    row = await stored(f)
    assert row.summary_status == "failed" and row.summary is None and row.title == "Existing topic"
    assert "Rejected point 1" in row.summary_error
    assert "Declared fixture verdict" in row.summary_error
    assert len(row.summary_error) <= 240


async def test_manual_title_during_fidelity_cannot_be_overwritten(memory_db):
    f = memory_db

    async def check(request):
        await conversations.update_owned(f.owner.id, f.conversation.id, ConversationUpdate(title="User renamed this"))
        return fidelity_completion(request)

    f.check_handler = check
    await request_and_execute(f)
    row = await stored(f)
    assert row.summary_status == "ready" and row.title == "User renamed this" and row.title_origin == "manual"


@pytest.mark.parametrize("bad_inventory,expected_detail", [
    ("missing", "Missing point 1 verdict"),
    ("duplicate", "Duplicate verdict"),
    ("unexpected_title", "Unexpected title 0 verdict"),
])
def test_recap_only_check_requires_an_exact_inventory(bad_inventory, expected_detail):
    candidate = MetadataDraft(title="Unused topic", introduction="Saved discussion.",
                              points=["First point.", "Second point.", "Third point."])
    verdicts = [
        {"item": item, "index": index, "supported": True, "reason": "Fixture support."}
        for item, index in [("introduction", 0), ("point", 0), ("point", 1), ("point", 2)]
    ]
    if bad_inventory == "missing":
        verdicts.pop(2)
    elif bad_inventory == "duplicate":
        verdicts.append(dict(verdicts[0]))
    else:
        verdicts.append({"item": "title", "index": 0, "supported": True, "reason": "Unused title."})
    with pytest.raises(memory.RecapFidelityError) as caught:
        memory.validate_fidelity(candidate, memory.RecapFidelityCheck(verdicts=verdicts), include_title=False)
    assert expected_detail in memory.fidelity_failure_message(caught.value)


def test_fidelity_reason_is_bounded_and_redacts_credentials_without_persisting_candidate(monkeypatch):
    monkeypatch.setattr(settings, "openrouter_api_key", "private-fixture-credential")
    monkeypatch.setattr(settings, "openai_api_key", "another-private-fixture-credential")
    candidate = MetadataDraft(title="Private rejected title", introduction="Private rejected introduction.",
                              points=["Private rejected point one.", "Private rejected point two."])
    check = memory.RecapFidelityCheck(verdicts=[
        memory.RecapFidelityVerdict(item=item, index=index, supported=item != "point", reason=(
            "private-fixture-credential another-private-fixture-credential sk-pretend012345 "
            "Bearer pretended-token " + "unsupported " * 10
        ) if item == "point" else "Supported fixture.")
        for item, index in [("title", 0), ("introduction", 0), ("point", 0), ("point", 1)]
    ])
    with pytest.raises(memory.RecapFidelityError) as caught:
        memory.validate_fidelity(candidate, check)
    message = memory.fidelity_failure_message(caught.value)
    assert len(message) <= 240 and "Rejected point 0" in message and "[redacted]" in message
    assert "private-fixture-credential" not in message and "sk-pretend" not in message
    assert "pretended-token" not in message and "Private rejected" not in message
    assert "Your messages are saved" in message and "retry" in message


async def test_failed_replacement_keeps_previous_summary_stored_but_never_presents_it_as_current(memory_db):
    f = memory_db
    await request_and_execute(f)
    previous = (await stored(f)).summary
    async with f.session.begin() as db:
        run = await db.get(Run, f.run.id)
        run.question = "What changed in this new discussion?"
        row = await db.get(Conversation, f.conversation.id)
        conversations.touch(row)

    async def reject(request):
        return fidelity_completion(request, unsupported=("introduction", 0))

    f.check_handler = reject
    await request_and_execute(f)
    row = await stored(f)
    assert row.summary == previous and row.summary_status == "failed"
    assert ConversationView.model_validate(row).summary is None
    await conversations.create_share(f.owner.id, row.id)
    async with f.session() as db:
        snapshot = await db.scalar(select(ConversationShare))
        assert snapshot.snapshot["summary"] is None
    async with f.session.begin() as db:
        row = await db.get(Conversation, f.conversation.id)
        conversations.touch(row)
    await memory.schedule_auto(row.id, f.owner.id, voice_ended=True)
    assert (await stored(f)).summary_status == "failed" and len(f.calls) == 4


async def test_pre_check_policy_cache_is_not_reused_by_an_explicit_request(memory_db):
    f = memory_db
    async with f.session.begin() as db:
        row = await db.get(Conversation, f.conversation.id)
        content = await conversations.conversation_content(db, row.id, max_bytes=memory.INPUT_MAX_BYTES)
        row.summary_input_hash = sha256(json.dumps(content, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        row.summary, row.summary_status = "Older unchecked metadata.", "ready"
    await request_and_execute(f)
    assert (await stored(f)).summary != "Older unchecked metadata."
    assert (await stored(f)).summary_status == "ready" and len(f.calls) == 2


@pytest.mark.parametrize("setting,model", [
    ("conversation_metadata_model", "openai/gpt-5.6-luna"),
    ("openrouter_model", "openai/gpt-4.1-nano"),
])
async def test_changing_either_model_requires_a_fresh_checked_recap(memory_db, monkeypatch, setting, model):
    f = memory_db
    await request_and_execute(f)
    previous = await stored(f)
    assert previous.summary_status == "ready" and len(f.calls) == 2
    monkeypatch.setattr(settings, setting, model)
    await request_and_execute(f)
    current = await stored(f)
    assert current.summary_status == "ready" and len(f.calls) == 4
    assert current.summary_input_hash != previous.summary_input_hash
    assert f.calls[2]["model"] == settings.conversation_metadata_model
    assert f.calls[3]["model"] == settings.openrouter_model
    assert json.loads(f.calls[2]["messages"][1]["content"]) == json.loads(f.calls[0]["messages"][1]["content"])
    await request_and_execute(f)
    assert (await stored(f)).summary_input_hash == current.summary_input_hash
    assert len(f.calls) == 4  # The same content and model pair still reuse the cache.


@pytest.mark.parametrize("failure", ["rate_limit", "timeout"])
async def test_fidelity_request_failure_is_bounded_and_never_automatically_repeated(memory_db, monkeypatch, failure):
    f = memory_db

    async def unavailable(request):
        if failure == "timeout":
            await asyncio.sleep(10)
        return completion(status=429)

    f.check_handler = unavailable
    if failure == "timeout":
        monkeypatch.setattr(settings, "conversation_metadata_timeout_seconds", 0.5)
    await request_and_execute(f)
    row = await stored(f)
    assert row.summary_status == "failed" and row.summary is None and len(f.calls) == 2
    await memory.schedule_auto(row.id, f.owner.id, voice_ended=True)
    await memory.recover()
    assert len(f.calls) == 2
    async with f.session() as db:
        attempts = list(await db.scalars(select(ProviderAttempt).order_by(ProviderAttempt.created_at)))
        assert attempts[0].status == "completed"
        assert attempts[1].operation == "conversation_metadata_check"
        assert attempts[1].status == ("failed" if failure == "rate_limit" else "uncertain")


@pytest.mark.parametrize("change", ["content", "reset"])
async def test_late_fidelity_verdict_cannot_publish_after_conversation_or_owner_changes(memory_db, change):
    f = memory_db
    started, release = asyncio.Event(), asyncio.Event()

    async def delayed_check(request):
        started.set()
        await release.wait()
        return fidelity_completion(request)

    f.check_handler = delayed_check
    await memory.request_summary(f.owner.id, f.conversation.id)
    task = asyncio.create_task(memory.execute(f.conversation.id))
    await asyncio.wait_for(started.wait(), 3)
    async with f.session.begin() as db:
        if change == "reset":
            owner = await db.get(BrowserSession, f.owner.id)
            owner.revoked = True
        else:
            row = await db.get(Conversation, f.conversation.id)
            conversations.touch(row)
            run = await db.get(Run, f.run.id)
            run.question = "A corrected question arrived during checking."
    release.set()
    await asyncio.wait_for(task, 3)
    row = await stored(f)
    assert row.summary_status == "failed" and row.summary is None and len(f.calls) == 2
    async with f.session() as db:
        attempts = list(await db.scalars(select(ProviderAttempt)))
        assert len(attempts) == 2 and all(attempt.status == "completed" for attempt in attempts)


async def test_automatic_first_title_and_no_historic_bulk_or_manual_title_overwrite(memory_db):
    f = memory_db
    await memory.recover()
    assert f.queued == []
    await memory.schedule_auto(f.conversation.id, f.owner.id)
    assert (await stored(f)).summary_status == "pending"
    await memory.execute(f.conversation.id)
    assert (await stored(f)).title_generated
    await memory.schedule_auto(f.conversation.id, f.owner.id)
    assert len(f.queued) == 1
    await conversations.update_owned(f.owner.id, f.conversation.id, ConversationUpdate(title="My manual title"))
    async with f.session.begin() as db:
        run = await db.get(Run, f.run.id)
        run.question = "A changed saved question"
    await request_and_execute(f)
    row = await stored(f)
    assert row.title == "My manual title" and row.title_origin == "manual" and row.summary_status == "ready"


async def test_gemini_metadata_uses_bounded_high_reasoning_and_private_vertex_route(memory_db, monkeypatch):
    f = memory_db
    monkeypatch.setattr(settings, "conversation_metadata_model", "google/gemini-3.8-flash")
    await request_and_execute(f)
    assert (await stored(f)).summary_status == "ready"
    body = f.calls[0]
    assert body["model"] == "google/gemini-3.8-flash"
    assert body["max_tokens"] == 4096
    assert body["reasoning"] == {"effort": "high", "exclude": True}
    assert body["provider"]["only"] == ["google-vertex/global"]
    assert body["provider"]["data_collection"] == "deny"
    assert body["provider"]["zdr"] is True
    assert "Do not generate an introduction" in body["messages"][0]["content"]


async def test_run_publication_schedules_first_meaningful_title_only_after_commit(memory_db):
    f = memory_db
    async with f.session.begin() as db:
        run = await db.get(Run, f.run.id)
        run.status, run.owner_id, run.fence = "running", runs.OWNER, 7
        run.lease_expires_at = utcnow() + timedelta(seconds=20)
    await runs.finish(f.run.id, 7, "completed", answer=f.run.answer)
    assert (await stored(f)).summary_status == "pending" and f.queued == [f.conversation.id]
    await memory.execute(f.conversation.id)
    assert (await stored(f)).summary_status == "ready"


async def test_clarification_is_not_a_meaningful_first_answer(memory_db):
    f = memory_db
    async with f.session.begin() as db:
        run = await db.get(Run, f.run.id)
        run.answer = {"status": "clarification", "gaps": [{"text": "Which month did you mean?"}]}
    await memory.schedule_auto(f.conversation.id, f.owner.id)
    assert (await stored(f)).summary_status == "idle" and f.queued == []


async def test_unchanged_content_reuses_cached_recap_after_a_new_empty_voice_session(memory_db):
    f = memory_db
    await request_and_execute(f)
    async with f.session.begin() as db:
        row = await db.get(Conversation, f.conversation.id)
        conversations.touch(row)
    row = await stored(f)
    assert row.summary_status == "idle"
    assert ConversationView.model_validate(row).summary is None
    assert (await memory.request_summary(f.owner.id, row.id)).summary_status == "ready"
    assert len(f.calls) == 2


@pytest.mark.parametrize("change", ["rename", "auto_title", "new_content", "delete", "reset"])
async def test_late_completion_is_fenced_and_actual_usage_survives(memory_db, change):
    f = memory_db
    started, release = asyncio.Event(), asyncio.Event()

    async def response(request):
        started.set()
        await release.wait()
        return selection_completion(request)

    f.handler = response
    await memory.request_summary(f.owner.id, f.conversation.id)
    task = asyncio.create_task(memory.execute(f.conversation.id))
    await asyncio.wait_for(started.wait(), 3)
    if change == "rename":
        await conversations.update_owned(f.owner.id, f.conversation.id, ConversationUpdate(title="Human name"))
    elif change == "auto_title":
        async with f.session.begin() as db:
            row = await db.get(Conversation, f.conversation.id)
            row.title = "Independently generated title"
            row.title_generated = True
            row.title_revision += 1
    elif change == "delete":
        await conversations.delete_owned(f.owner.id, f.conversation.id)
    else:
        async with f.session.begin() as db:
            if change == "reset":
                owner = await db.get(BrowserSession, f.owner.id)
                owner.revoked = True
            else:
                row = await db.get(Conversation, f.conversation.id)
                conversations.touch(row)
                run = await db.get(Run, f.run.id)
                run.question = "Latest corrected question"
    release.set()
    await asyncio.wait_for(task, 3)
    row = await stored(f)
    if change == "rename":
        assert row.title == "Human name" and row.summary_status == "ready"
    elif change == "auto_title":
        assert row.title == "Independently generated title" and row.summary_status == "ready"
    elif change == "delete":
        assert row is None
    else:
        assert row.summary is None and row.summary_status == "failed"
    async with f.session() as db:
        attempt = await db.scalar(select(ProviderAttempt).where(ProviderAttempt.operation == "conversation_metadata"))
        assert attempt.status == "completed" and attempt.usage["cost"] == 0.00003
        assert attempt.metadata_job_id
        assert attempt.conversation_id is None if change == "delete" else attempt.conversation_id == row.id


async def test_pending_voice_work_blocks_request_delete_and_generation_until_final_answer(memory_db, monkeypatch):
    f = memory_db
    hooks = memory.ConversationVoiceHooks()
    async with f.session.begin() as db:
        live = await db.get(LiveSession, f.live.id)
        live.status = "working"
        live.provider_session_id = "fixture-provider"
        live.snapshot = {"revision": 1}
        live.lease_expires_at = utcnow() + timedelta(seconds=30)
    intent = VoiceIntent(
        session_id=f.live.id, provider_session_id="fixture-provider", conversation_id=f.conversation.id,
        context_version=1, index_id="memory-index", fence=1, revision=1, delegation_id="fixture-delegation",
        question="What was concluded?", transcript=[],
    )
    assert await hooks.claim_delegation(intent)
    assert await hooks.save_snapshot(f.live.id, 1, VoiceSnapshot(id=f.live.id, status="closed",
                                                               revision=1, close_reason="user"))
    assert (await stored(f)).summary_status == "pending"
    for action in (memory.request_summary, conversations.delete_owned):
        with pytest.raises(HTTPException) as error:
            await action(f.owner.id, f.conversation.id)
        assert error.value.status_code == 409
    await memory.execute(f.conversation.id)
    assert f.calls == [] and (await stored(f)).summary_status == "pending"

    async def final_answer(self, task):
        async with f.session.begin() as db:
            run = await db.get(Run, f.run.id)
            run.answer = {"status": "answered", "statements": [{"text": "The final saved conclusion."}]}
        return VoiceAnswer(run_id=f.run.id, accepted=True, spoken_text="The final saved conclusion.")

    monkeypatch.setattr(voice_storage.PostgresVoiceHooks, "answer", final_answer)
    await hooks.answer(intent)
    await memory.execute(f.conversation.id)
    assert (await stored(f)).summary_status == "ready"
    assert "final saved conclusion" in f.calls[0]["messages"][1]["content"]


async def test_active_run_blocks_end_recap_and_server_auto_waits(memory_db):
    f = memory_db
    async with f.session.begin() as db:
        row = await db.get(Run, f.run.id)
        row.status = "running"
    await memory.schedule_auto(f.conversation.id, f.owner.id, voice_ended=True)
    await memory.execute(f.conversation.id)
    assert (await stored(f)).summary_status == "pending" and f.calls == []
    async with f.session.begin() as db:
        row = await db.get(Run, f.run.id)
        row.status = "completed"
    await memory.execute(f.conversation.id)
    assert (await stored(f)).summary_status == "ready" and len(f.calls) == 2


async def test_concurrent_duplicate_requests_and_claims_bill_each_operation_at_most_once(memory_db):
    f = memory_db
    responses = await asyncio.gather(*(memory.request_summary(f.owner.id, f.conversation.id) for _ in range(4)))
    assert len({row.summary_job_id for row in responses}) == 1
    await asyncio.gather(*(memory.execute(f.conversation.id) for _ in range(3)))
    assert len(f.calls) == 2 and (await stored(f)).summary_status == "ready"


async def test_global_metadata_limit_leaves_durable_pending_work(memory_db):
    f = memory_db
    async with f.session.begin() as db:
        for _ in range(2):
            db.add(Conversation(session_id=f.owner.id, summary_status="generating", summary_owner="other-worker",
                                summary_lease_expires_at=utcnow() + timedelta(seconds=30)))
    await memory.request_summary(f.owner.id, f.conversation.id)
    await memory.execute(f.conversation.id)
    assert (await stored(f)).summary_status == "pending" and f.calls == []


async def test_newer_summary_request_fences_old_completion_and_keeps_new_work_pending(memory_db):
    f = memory_db
    started, release = asyncio.Event(), asyncio.Event()

    async def delayed(request):
        started.set()
        await release.wait()
        return selection_completion(request)

    f.handler = delayed
    await memory.request_summary(f.owner.id, f.conversation.id)
    task = asyncio.create_task(memory.execute(f.conversation.id))
    await asyncio.wait_for(started.wait(), 3)
    async with f.session.begin() as db:
        row = await db.get(Conversation, f.conversation.id)
        conversations.touch(row)
        run = await db.get(Run, f.run.id)
        run.question = "A newer question after voice ended"
    newer = await memory.request_summary(f.owner.id, f.conversation.id, automatic=True)
    assert newer.summary_status == "pending"
    release.set()
    await task
    assert (await stored(f)).summary_status == "pending"
    f.handler = None
    await memory.execute(f.conversation.id)
    assert (await stored(f)).summary_status == "ready" and len(f.calls) == 3
    assert "newer question" in f.calls[1]["messages"][1]["content"]


async def test_failed_calls_have_bounded_explicit_retry_and_no_automatic_success_fallback(memory_db):
    f = memory_db

    async def unavailable(request):
        return completion(status=429)

    f.handler = unavailable
    for attempt in range(3):
        await request_and_execute(f)
        row = await stored(f)
        assert row.summary_status == "failed" and row.summary is None
        assert row.summary_attempts == attempt + 1 and row.title == "New conversation"
        await memory.schedule_auto(row.id, f.owner.id, voice_ended=True)
        await memory.recover()
    with pytest.raises(HTTPException) as error:
        await memory.request_summary(f.owner.id, f.conversation.id)
    assert error.value.detail["code"] == "SUMMARY_RETRY_LIMIT"
    assert len(f.calls) == 3 * (1 + len(RATE_LIMIT_BACKOFF_SECONDS))
    async with f.session() as db:
        attempts = list(await db.scalars(select(ProviderAttempt)))
        assert len(attempts) == 3 and all(a.status == "failed" for a in attempts)
        assert all(a.usage is None for a in attempts)


async def test_timeout_cancellation_and_expired_generation_are_honest_recoverable_failures(memory_db, monkeypatch):
    f = memory_db
    started = asyncio.Event()

    async def slow(request):
        started.set()
        await asyncio.sleep(10)
        return selection_completion(request)

    f.handler = slow
    monkeypatch.setattr(settings, "conversation_metadata_timeout_seconds", 0.03)
    await request_and_execute(f)
    assert (await stored(f)).summary_status == "failed"
    async with f.session() as db:
        attempt = await db.scalar(select(ProviderAttempt))
        # The deadline can expire during the database claim before any HTTP
        # request. Only a dispatched attempt should have an uncertain receipt.
        if attempt is None:
            assert f.calls == []
        else:
            assert attempt.status == "uncertain" and attempt.error_code == "provider_cancelled"
    monkeypatch.setattr(settings, "conversation_metadata_timeout_seconds", 30)
    await memory.request_summary(f.owner.id, f.conversation.id)
    started.clear()
    task = asyncio.create_task(memory.execute(f.conversation.id))
    await asyncio.wait_for(started.wait(), 3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (await stored(f)).summary_status == "failed"
    async with f.session.begin() as db:
        dispatched = await db.scalar(select(ProviderAttempt).order_by(ProviderAttempt.created_at.desc()).limit(1))
        assert dispatched.status == "uncertain" and dispatched.error_code == "provider_cancelled"
        row = await db.get(Conversation, f.conversation.id)
        row.summary_status, row.summary_owner = "generating", "lost-process"
        row.summary_lease_expires_at = utcnow() - timedelta(seconds=1)
    calls = len(f.calls)
    await memory.recover()
    assert (await stored(f)).summary_status == "failed" and len(f.calls) == calls


async def test_pending_deadline_and_oversize_input_do_not_generate_partial_recaps(memory_db):
    f = memory_db
    await memory.request_summary(f.owner.id, f.conversation.id)
    async with f.session.begin() as db:
        row = await db.get(Conversation, f.conversation.id)
        row.summary_requested_at = utcnow() - timedelta(minutes=4)
    await memory.recover()
    assert (await stored(f)).summary_status == "failed" and f.calls == []
    async with f.session.begin() as db:
        run = await db.get(Run, f.run.id)
        run.answer = {"statements": [{"text": "x" * 60_001}]}
    result = await memory.request_summary(f.owner.id, f.conversation.id)
    assert result.summary_status == "failed" and "nothing was truncated" in result.summary_error
    assert f.calls == []


async def test_metadata_dispatch_rejects_stale_claim_before_any_provider_request(memory_db):
    f = memory_db
    async with f.session.begin() as db:
        row = await db.get(Conversation, f.conversation.id)
        row.summary_status, row.summary_owner = "generating", memory.OWNER
        row.summary_fence, row.summary_job_id = 5, str(uuid4())
        row.summary_lease_expires_at = utcnow() + timedelta(seconds=30)
    provider = memory.MetadataClient(row, activity_version=row.activity_version)
    async with f.session.begin() as db:
        current = await db.get(Conversation, row.id)
        conversations.touch(current)
    with pytest.raises(ProviderError) as error:
        await provider.structured("fixture", {}, MetadataDraft, operation="conversation_metadata")
    assert error.value.code == "provider_claim_inactive" and f.calls == []
    async with f.session() as db:
        assert await db.scalar(select(func.count()).select_from(ProviderAttempt)) == 0


async def test_retention_prunes_new_snapshot_and_metadata_receipts_but_protects_live_lease(memory_db):
    f = memory_db
    await request_and_execute(f)
    await conversations.create_share(f.owner.id, f.conversation.id)
    async with f.session.begin() as db:
        owner = await db.get(BrowserSession, f.owner.id)
        owner.expires_at = utcnow() - timedelta(days=2)
        row = await db.get(Conversation, f.conversation.id)
        row.summary_lease_expires_at = utcnow() + timedelta(seconds=30)
    assert (await retention.prune_expired_sessions(apply=True))["deleted_sessions"] == 0
    async with f.session.begin() as db:
        row = await db.get(Conversation, f.conversation.id)
        row.summary_lease_expires_at = None
    result = await retention.prune_expired_sessions(apply=True)
    assert result["deleted_sessions"] == 1
    assert result["rows"]["conversation_shares"] == 1 and result["rows"]["provider_attempts"] == 2
    async with f.session() as db:
        assert await db.scalar(select(func.count()).select_from(ConversationShare)) == 0
        assert await db.get(CorpusIndex, "memory-index")
