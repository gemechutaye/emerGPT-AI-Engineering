"""Retention against isolated real Postgres; no provider/network calls or working-data purge."""

import asyncio
import json
import os
import subprocess
import sys
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import psycopg
import pytest
from emer.storage import retention
from emer.storage.models import (
    ActiveIndex,
    Base,
    BrowserSession,
    Conversation,
    CorpusIndex,
    Delegation,
    Draft,
    LiveEvent,
    LiveSession,
    ProviderAttempt,
    Run,
    RunEvent,
    utcnow,
)
from sqlalchemy import func, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


@pytest.fixture
async def retention_db(monkeypatch):
    database = "emer_retention_test_" + uuid4().hex[:12]
    url = f"postgresql+psycopg://localhost:55432/{database}"
    with psycopg.connect("postgresql://localhost:55432/postgres", autocommit=True) as admin:
        admin.execute(f"CREATE DATABASE {database}")
    engine = create_async_engine(url)
    session = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    monkeypatch.setattr(retention, "Session", session)
    now = utcnow()
    monkeypatch.setattr(retention, "utcnow", lambda: now)
    async with session.begin() as db:
        db.add(CorpusIndex(id="retention-index", checksum="retention-checksum", corpus_checksum="corpus",
                           bundle=b"immutable source and retrieval bytes", manifest={"documents": ["PT-001"]}))
        await db.flush()
        db.add(ActiveIndex(id=1, index_id="retention-index"))
    try:
        yield SimpleNamespace(session=session, now=now, url=url)
    finally:
        await engine.dispose()
        with psycopg.connect("postgresql://localhost:55432/postgres", autocommit=True) as admin:
            admin.execute(f"DROP DATABASE {database} WITH (FORCE)")


async def seed_work(fixture, *, expires_at=None, revoked=False, run_status="completed", live_status="closed",
                    run_lease=None, live_lease=None, draft_lease=None):
    """A full owned graph, including receipts referencing run, draft and Live simultaneously."""
    async with fixture.session.begin() as db:
        owner = BrowserSession(token_hash=uuid4().hex, created_at=fixture.now - timedelta(days=16),
                               expires_at=expires_at or fixture.now - timedelta(days=2), revoked=revoked)
        db.add(owner)
        await db.flush()
        conversation = Conversation(session_id=owner.id, title="Retained synthetic conversation")
        db.add(conversation)
        await db.flush()
        run = Run(session_id=owner.id, conversation_id=conversation.id, index_id="retention-index",
                  idempotency_key=uuid4().hex, body_hash="body", question="What is documented?",
                  context_version=1, status=run_status, answer={"status": "answered"},
                  lease_expires_at=run_lease)
        live = LiveSession(session_id=owner.id, conversation_id=conversation.id, index_id="retention-index",
                           context_version=1, context={}, idempotency_key=uuid4().hex, body_hash="offer",
                           status=live_status, snapshot={"final_usage_confirmed": False},
                           lease_expires_at=live_lease)
        db.add_all([run, live])
        await db.flush()
        draft = Draft(session_id=owner.id, run_id=run.id, index_id="retention-index", text="Internal draft",
                      checking_version=1 if draft_lease else None,
                      check_owner="checker" if draft_lease else None, check_lease_expires_at=draft_lease)
        db.add(draft)
        await db.flush()
        db.add_all([
            RunEvent(run_id=run.id, sequence=1, type="completed", data={"status": "answered"}),
            LiveEvent(live_session_id=live.id, provider_event_id=uuid4().hex,
                      kind="session.input_transcript.delta", payload={"delta": "synthetic words"}),
            Delegation(live_session_id=live.id, delegation_id=uuid4().hex, run_id=run.id,
                       intent={"question": "What is documented?"}),
            ProviderAttempt(session_id=owner.id, run_id=run.id, draft_id=draft.id, live_session_id=live.id,
                            operation="generation", model="fixture", status="uncertain", usage=None),
            ProviderAttempt(session_id=owner.id, run_id=run.id, operation="generation", model="fixture",
                            status="completed", usage={"total_tokens": 17, "cost": None}),
            ProviderAttempt(session_id=owner.id, run_id=run.id, operation="generation", model="fixture",
                            status="completed", usage={"total_tokens": 19, "cost": 0.0001}),
        ])
    return SimpleNamespace(owner=owner, conversation=conversation, run=run, live=live, draft=draft)


async def table_counts(fixture):
    async with fixture.session() as db:
        return {table.name: await db.scalar(select(func.count()).select_from(table))
                for table in Base.metadata.sorted_tables}


async def test_preview_then_delete_full_owned_graph_preserves_other_owner_and_indexes(retention_db):
    expired = await seed_work(retention_db, live_status="uncertain")
    current = await seed_work(retention_db, expires_at=retention_db.now + timedelta(days=4))
    before = await table_counts(retention_db)
    preview = await retention.prune_expired_sessions()
    assert preview["mode"] == "dry_run" and preview["selected_sessions"] == 1
    assert preview["deleted_sessions"] == 0
    assert preview["unknown_provider_usage_receipts"] == 2
    assert preview["unconfirmed_live_usage_sessions"] == 1
    assert await table_counts(retention_db) == before

    result = await retention.prune_expired_sessions(apply=True)
    assert result["rows"] == preview["rows"]
    assert result["deleted_sessions"] == 1
    async with retention_db.session() as db:
        assert await db.get(BrowserSession, expired.owner.id) is None
        assert await db.get(Conversation, expired.conversation.id) is None
        assert await db.get(Run, expired.run.id) is None
        assert await db.get(LiveSession, expired.live.id) is None
        assert await db.get(Draft, expired.draft.id) is None
        assert (await db.get(BrowserSession, current.owner.id)).expires_at == current.owner.expires_at
        assert (await db.get(Run, current.run.id)).answer == {"status": "answered"}
        assert (await db.get(Draft, current.draft.id)).text == "Internal draft"
        index = await db.get(CorpusIndex, "retention-index")
        assert index.bundle == b"immutable source and retrieval bytes"
        assert index.checksum == "retention-checksum" and index.manifest == {"documents": ["PT-001"]}
        assert (await db.get(ActiveIndex, 1)).index_id == index.id
    after = await table_counts(retention_db)
    assert all(after[name] == count - result["rows"].get(name, 0) for name, count in before.items())
    assert (await retention.prune_expired_sessions(apply=True))["deleted_sessions"] == 0


async def test_strict_original_expiry_grace_and_reset_does_not_accelerate_deletion(retention_db):
    cutoff = retention_db.now - timedelta(hours=24)
    delete_me = await seed_work(retention_db, expires_at=cutoff - timedelta(microseconds=1))
    at_boundary = await seed_work(retention_db, expires_at=cutoff)
    recent = await seed_work(retention_db, expires_at=cutoff + timedelta(microseconds=1))
    reset_early = await seed_work(retention_db, expires_at=retention_db.now + timedelta(days=10), revoked=True)
    result = await retention.prune_expired_sessions(apply=True)
    assert result["deleted_sessions"] == 1
    async with retention_db.session() as db:
        assert await db.get(BrowserSession, delete_me.owner.id) is None
        for work in (at_boundary, recent, reset_early):
            assert await db.get(BrowserSession, work.owner.id) is not None
        assert (await db.get(BrowserSession, reset_early.owner.id)).revoked


async def test_active_work_and_unexpired_leases_are_protected_without_starving_eligible_sessions(retention_db):
    future = retention_db.now + timedelta(minutes=1)
    protected = [
        await seed_work(retention_db, run_status="queued"),
        await seed_work(retention_db, run_status="running", run_lease=retention_db.now - timedelta(seconds=1)),
        await seed_work(retention_db, run_lease=future),
        await seed_work(retention_db, live_status="working"),
        await seed_work(retention_db, live_status="closing"),
        await seed_work(retention_db, live_lease=future),
        await seed_work(retention_db, draft_lease=future),
    ]
    eligible = await seed_work(retention_db, expires_at=retention_db.now - timedelta(hours=25),
                               draft_lease=retention_db.now - timedelta(seconds=1))
    result = await retention.prune_expired_sessions(apply=True, batch_size=1)
    assert result["protected_sessions"] == len(protected)
    assert result["deleted_sessions"] == 1
    async with retention_db.session() as db:
        assert await db.get(BrowserSession, eligible.owner.id) is None
        for work in protected:
            assert await db.get(BrowserSession, work.owner.id) is not None
            assert await db.get(Draft, work.draft.id) is not None


async def test_locked_owner_is_skipped_and_reconsidered_on_next_pass(retention_db):
    locked = await seed_work(retention_db)
    free = await seed_work(retention_db)
    async with retention_db.session.begin() as db:
        await db.scalar(select(BrowserSession).where(BrowserSession.id == locked.owner.id).with_for_update())
        result = await asyncio.wait_for(retention.prune_expired_sessions(apply=True), timeout=3)
        assert result["deleted_sessions"] == 1
        assert await db.get(BrowserSession, locked.owner.id) is not None
        assert await db.get(BrowserSession, free.owner.id) is None
    assert (await retention.prune_expired_sessions(apply=True))["deleted_sessions"] == 1


async def test_two_concurrent_passes_do_not_double_delete_or_exceed_batch(retention_db):
    for _ in range(3):
        await seed_work(retention_db)
    results = await asyncio.gather(*(retention.prune_expired_sessions(apply=True, batch_size=1)
                                    for _ in range(2)))
    assert [r["deleted_sessions"] for r in results] == [1, 1]
    async with retention_db.session() as db:
        assert await db.scalar(select(func.count()).select_from(BrowserSession)) == 1
    assert (await retention.prune_expired_sessions(apply=True))["deleted_sessions"] == 1


async def test_child_write_lock_times_out_and_rolls_back_the_whole_deletion(retention_db):
    work = await seed_work(retention_db)
    before = await table_counts(retention_db)
    async with retention_db.session.begin() as db:
        await db.scalar(select(Draft).where(Draft.id == work.draft.id).with_for_update())
        # Provider receipts and Live children precede Draft in deletion order. A
        # blocked child write must restore those earlier deletions on rollback.
        with pytest.raises(DBAPIError):
            await asyncio.wait_for(retention.prune_expired_sessions(apply=True), timeout=5)
        assert await table_counts(retention_db) == before
    assert (await retention.prune_expired_sessions(apply=True))["deleted_sessions"] == 1


async def test_cli_defaults_to_preview_and_requires_apply_for_actual_deletion(retention_db):
    await seed_work(retention_db)
    command = str(Path(sys.executable).with_name("emer"))

    async def invoke(*args):
        completed = await asyncio.to_thread(subprocess.run, [command, "prune", *args],
                                            env={**os.environ, "DATABASE_URL": retention_db.url},
                                            capture_output=True, text=True, timeout=15, check=True)
        return json.loads(completed.stdout)

    assert (await invoke())["mode"] == "dry_run"
    async with retention_db.session() as db:
        assert await db.scalar(select(func.count()).select_from(BrowserSession)) == 1
    assert (await invoke("--apply"))["deleted_sessions"] == 1
    async with retention_db.session() as db:
        assert await db.scalar(select(func.count()).select_from(BrowserSession)) == 0


async def test_hourly_loop_waits_before_deleting_and_survives_a_failed_pass(monkeypatch):
    calls = []

    async def wait(interval):
        calls.append(("wait", interval))
        if len(calls) == 5:
            raise asyncio.CancelledError

    async def prune(*, apply):
        calls.append(("prune", apply))
        if len(calls) == 2:
            raise TimeoutError
        return {"deleted_sessions": 0}

    monkeypatch.setattr(retention.asyncio, "sleep", wait)
    monkeypatch.setattr(retention, "prune_expired_sessions", prune)
    with pytest.raises(asyncio.CancelledError):
        await retention.retention_loop()
    assert calls == [("wait", 3600), ("prune", True), ("wait", 3600), ("prune", True), ("wait", 3600)]
