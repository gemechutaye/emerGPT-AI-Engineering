"""Shared workspace migration and independent-browser HTTP behavior on real Postgres."""

from datetime import timedelta
from uuid import uuid4

import httpx
import pytest
from emer.api import dependencies, history_routes
from emer.services import conversations, shared_workspace
from emer.settings import settings
from emer.storage.models import BrowserSession, Conversation, LiveSession, ProviderAttempt, Run, utcnow
from sqlalchemy import func, select
from starlette.requests import Request
from test_conversation_memory import memory_db as isolated_db
from test_http_postgres import await_run
from test_http_postgres import server as isolated_server

memory_db = isolated_db
server = isolated_server


async def test_migration_preserves_all_records_and_all_browsers_resolve_one_owner(memory_db, monkeypatch):
    monkeypatch.setattr(shared_workspace, "Session", memory_db.session)
    monkeypatch.setattr(history_routes, "Session", memory_db.session)
    monkeypatch.setattr(settings, "shared_workspace", True)
    async with memory_db.session.begin() as db:
        foreign = BrowserSession(token_hash=uuid4().hex, expires_at=utcnow() + timedelta(days=1))
        db.add(foreign)
        await db.flush()
        other = Conversation(session_id=foreign.id, title="Different browser")
        db.add(other)
        await db.flush()
        live = LiveSession(session_id=foreign.id, conversation_id=other.id, index_id="memory-index",
                           context_version=1, context={}, idempotency_key=memory_db.live.idempotency_key,
                           body_hash="fixture", status="closed")
        db.add(live)
        await db.flush()
        db.add(ProviderAttempt(session_id=foreign.id, conversation_id=other.id, live_session_id=live.id,
                               operation="fixture", model="fixture", status="completed"))
        count = await db.scalar(select(func.count()).select_from(Conversation))
    changed = await shared_workspace.initialize()
    assert changed["conversations"] == count
    assert changed["live_sessions"] == 2
    assert not any((await shared_workspace.initialize()).values())
    async with memory_db.session() as db:
        assert await db.scalar(select(func.count()).select_from(Conversation)) == count
        assert (await db.get(Run, memory_db.run.id)).question == memory_db.run.question
        assert (await db.get(Run, memory_db.run.id)).session_id == shared_workspace.WORKSPACE_ID
        assert len(set(await db.scalars(select(LiveSession.idempotency_key)))) == 2
    for headers in ([], [(b"cookie", b"emer_session=old-browser-cookie")]):
        owner = await dependencies.current_session(Request({"type": "http", "headers": headers}))
        assert owner.id == shared_workspace.WORKSPACE_ID and owner.expires_at.year == 9999
    assert await history_routes.revision(shared_workspace.WORKSPACE_ID)


async def test_migration_waits_for_active_work_and_rolls_back(memory_db, monkeypatch):
    monkeypatch.setattr(shared_workspace, "Session", memory_db.session)
    async with memory_db.session.begin() as db:
        run = await db.get(Run, memory_db.run.id)
        run.status, run.lease_expires_at = "running", utcnow() + timedelta(seconds=30)
    with pytest.raises(RuntimeError, match="active work"):
        await shared_workspace.initialize()
    async with memory_db.session() as db:
        assert await db.get(BrowserSession, shared_workspace.WORKSPACE_ID) is None
        assert (await db.get(Run, memory_db.run.id)).session_id == memory_db.owner.id


async def test_shared_recents_hide_empty_containers_but_keep_saved_voice(memory_db, monkeypatch):
    monkeypatch.setattr(settings, "shared_workspace", True)
    async with memory_db.session.begin() as db:
        db.add(Conversation(session_id=memory_db.owner.id, title="New conversation"))
        run = await db.get(Run, memory_db.run.id)
        await db.delete(run)
    page = await conversations.list_owned(memory_db.owner.id)
    assert [item.id for item in page.items] == [memory_db.conversation.id]


@pytest.mark.parametrize("server", [True], indirect=True)
def test_unrelated_browsers_see_identical_history_without_shared_cookie(server):
    url, _ = server
    with httpx.Client(base_url=url, headers={"Origin": url}) as codex, \
            httpx.Client(base_url=url, headers={"Origin": url}) as arc:
        a, b = codex.post("/api/v1/session"), arc.post("/api/v1/session")
        assert a.json()["id"] == b.json()["id"] == shared_workspace.WORKSPACE_ID
        assert not codex.cookies and not arc.cookies
        chat = codex.post("/api/v1/conversations", json={}).json()
        # An empty container is persisted, but is not a conversation in Recent yet.
        assert not arc.get("/api/v1/conversations").json()["items"]
        run = codex.post(f"/api/v1/conversations/{chat['id']}/runs", json={
            "question": "What cancellation guidance is recorded?", "context_version": 1,
            "idempotency_key": str(uuid4()),
        })
        assert run.status_code == 202
        await_run(codex, run.json()["id"])
        assert arc.get("/api/v1/conversations").json() == codex.get("/api/v1/conversations").json()
        assert len(arc.get(f"/api/v1/conversations/{chat['id']}").json()["runs"]) == 1
        assert arc.patch(f"/api/v1/conversations/{chat['id']}", json={"title": "Policy notes"}).status_code == 200
        assert codex.get(f"/api/v1/conversations/{chat['id']}").json()["title"] == "Policy notes"
        assert arc.post("/api/v1/session/reset").json()["id"] == shared_workspace.WORKSPACE_ID
        assert codex.get(f"/api/v1/conversations/{chat['id']}").status_code == 200
    with httpx.Client(base_url=url) as entirely_new_browser:
        assert entirely_new_browser.get("/api/v1/conversations").json()["items"][0]["title"] == "Policy notes"
