"""Frozen draft versions and conversation slot exclusion on real Postgres, provider double."""

import asyncio
from types import SimpleNamespace

import pytest
from emer.contracts.answer import ProviderUsage
from emer.services import drafts, runs
from emer.storage.models import Draft, Run
from fastapi import HTTPException
from sqlalchemy import select
from test_run_fencing import create
from test_run_fencing import run_db as isolated_run_db

run_db = isolated_run_db


async def setup_draft(fixture, monkeypatch):
    monkeypatch.setattr(drafts, "Session", fixture.session)
    row = await create(fixture)
    async with fixture.session.begin() as db:
        run = await db.get(Run, row.id)
        run.status = "completed"
        run.evidence = {"sources": [], "scopes": [], "retrieval": {}, "index_id": "test-index",
                        "index_checksum": "test-checksum", "corpus_checksum": "corpus-test"}
        draft = Draft(session_id=fixture.owner.id, run_id=row.id, index_id="test-index", text="Version one")
        db.add(draft)
        await db.flush()
    started, release = asyncio.Event(), asyncio.Event()

    class Provider:
        def __init__(self, *args, **kwargs):
            pass

        async def structured(self, *args, **kwargs):
            started.set()
            await release.wait()
            return SimpleNamespace(
                value=drafts.DraftCheck(supported=True, issues=[]),
                usage=ProviderUsage(operation="draft_recheck", model="double", latency_ms=1),
            )

    monkeypatch.setattr(drafts, "TrackedOpenRouterClient", Provider)
    return draft, started, release


async def test_recheck_blocks_duplicate_and_new_answer_then_releases_slot(run_db, monkeypatch):
    draft, started, release = await setup_draft(run_db, monkeypatch)
    task = asyncio.create_task(drafts.check_draft(draft.id, run_db.owner.id))
    await started.wait()
    with pytest.raises(HTTPException) as duplicate:
        await drafts.check_draft(draft.id, run_db.owner.id)
    assert duplicate.value.status_code == 409
    with pytest.raises(HTTPException) as answer:
        await runs.create_run(
            run_db.owner.id,
            run_db.conversation.id,
            runs.RunCreate(question="Next question", context_version=1, idempotency_key="distinct-next-key"),
        )
    assert answer.value.status_code == 409
    release.set()
    checked = await task
    assert checked.check["version"] == 1
    async with run_db.session() as db:
        stored = await db.get(Draft, draft.id)
        assert stored.check_owner is None and stored.check_lease_expires_at is None


async def test_stale_recheck_cannot_validate_newer_saved_text(run_db, monkeypatch):
    draft, started, release = await setup_draft(run_db, monkeypatch)
    task = asyncio.create_task(drafts.check_draft(draft.id, run_db.owner.id))
    await started.wait()
    async with run_db.session.begin() as db:
        stored = await db.scalar(select(Draft).where(Draft.id == draft.id).with_for_update())
        stored.version = 2
        stored.text = "Newer unreviewed text"
    release.set()
    with pytest.raises(HTTPException) as stale:
        await task
    assert stale.value.detail["code"] == "DRAFT_CHANGED"
    async with run_db.session() as db:
        stored = await db.get(Draft, draft.id)
        assert stored.text == "Newer unreviewed text" and stored.check is None
        assert stored.check_owner is None


async def test_draft_checks_share_global_answer_capacity(run_db, monkeypatch):
    from emer.settings import settings
    from emer.storage.models import Conversation

    draft, started, _ = await setup_draft(run_db, monkeypatch)
    async with run_db.session.begin() as db:
        other = Conversation(session_id=run_db.owner.id)
        db.add(other)
        await db.flush()
    await runs.create_run(
        run_db.owner.id, other.id,
        runs.RunCreate(question="Other active work", context_version=1, idempotency_key="other-work-key"),
    )
    monkeypatch.setattr(settings, "global_concurrency", 1)
    with pytest.raises(HTTPException) as busy:
        await drafts.check_draft(draft.id, run_db.owner.id)
    assert busy.value.status_code == 429
    assert not started.is_set()


async def test_active_draft_blocks_answer_in_other_conversation_at_global_limit(run_db, monkeypatch):
    from emer.settings import settings
    from emer.storage.models import Conversation

    draft, started, release = await setup_draft(run_db, monkeypatch)
    async with run_db.session.begin() as db:
        other = Conversation(session_id=run_db.owner.id)
        db.add(other)
        await db.flush()
    monkeypatch.setattr(settings, "global_concurrency", 1)
    task = asyncio.create_task(drafts.check_draft(draft.id, run_db.owner.id))
    await asyncio.wait_for(started.wait(), 2)
    try:
        with pytest.raises(HTTPException) as busy:
            await runs.create_run(
                run_db.owner.id, other.id,
                runs.RunCreate(question="Other work", context_version=1, idempotency_key="other-work-key"),
            )
        assert busy.value.status_code == 429
    finally:
        release.set()
        await task
