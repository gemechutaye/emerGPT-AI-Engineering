"""ASGI HTTP routes + isolated real Postgres; seeded answer/provider double, no live-quality claim."""

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import psycopg
import pytest
from emer.api import app as api
from emer.api import dependencies, voice_routes
from emer.contracts.answer import ProviderDraft, ProviderUsage, PublishedAnswer
from emer.services import conversation_memory, conversation_titles, drafts, provider_activity, runs
from emer.services.answering import validate_draft
from emer.services.retrieval import RetrievalService
from emer.settings import settings
from emer.storage import bundles
from emer.storage.models import Base, BrowserSession, Draft, Run, RunEvent
from psycopg import sql
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


@pytest.fixture
async def isolated_http(monkeypatch, tmp_path):
    name = "emer_w5_qa_" + uuid4().hex[:12]
    admin_dsn = "postgresql://localhost:55432/postgres"
    with psycopg.connect(admin_dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    engine = create_async_engine(f"postgresql+psycopg://localhost:55432/{name}")
    session = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        for module in [api, dependencies, voice_routes, bundles, drafts, provider_activity, runs,
                       conversation_memory, conversation_titles]:
            monkeypatch.setattr(module, "Session", session)
        monkeypatch.setattr(bundles, "_cache", {})
        monkeypatch.setattr(settings, "cache_dir", str(tmp_path / "indexes"))
        monkeypatch.setattr(settings, "app_origin", "http://qa.local")
        monkeypatch.setattr(settings, "cookie_secure", False)
        # This fixture tests browser ownership; shared demo reset has its own suite.
        monkeypatch.setattr(settings, "shared_workspace", False)
        bundle = await bundles.activate(str(Path(__file__).resolve().parents[2] / "config/corpus.json"))

        @asynccontextmanager
        async def visitor(cookies=None):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=api.app), base_url="http://qa.local",
                headers={"Origin": "http://qa.local"}, cookies=cookies,
            ) as client:
                yield client

        yield SimpleNamespace(visitor=visitor, session=session, engine=engine, bundle=bundle)
    finally:
        await engine.dispose()
        with psycopg.connect(admin_dsn, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


async def saved_answer(fixture, client):
    owner = (await client.post("/api/v1/session")).json()["id"]
    conversation = (await client.post("/api/v1/conversations", json={})).json()["id"]
    packet = RetrievalService(fixture.bundle).evidence("What did PT-006 complete?", as_of="2026-09-11")
    draft = ProviderDraft.model_validate({
        "statements": [{"part_id": "part-1", "text": "PT-006 completed an educational consultation.",
                        "citations": [{"doc_id": "PT-006", "quote": "Educational consultation completed."}]}],
        "gaps": [], "next_steps": [],
    })
    statements, _ = validate_draft(packet, draft)
    answer = PublishedAnswer(
        status="answered", statements=statements, gaps=[], next_steps=[], scopes=packet.scopes,
        index_id=packet.index_id, index_checksum=packet.index_checksum, usage=[],
        diagnostics={"classification": "W5 manually seeded source-backed answer; no generation"},
    )
    async with fixture.session.begin() as db:
        run = Run(session_id=owner, conversation_id=conversation, index_id=packet.index_id,
                  idempotency_key=uuid4().hex, body_hash="qa-seeded", question="What did PT-006 complete?",
                  context_version=1, status="completed", answer=answer.model_dump(mode="json"),
                  evidence=packet.model_dump(mode="json"))
        db.add(run)
        await db.flush()
        db.add(RunEvent(run_id=run.id, sequence=1, type="completed", data={"fixture": "seeded"}))
    response = await client.post("/api/v1/drafts", json={"run_id": run.id})
    assert response.status_code == 201, response.text
    return SimpleNamespace(owner=owner, conversation=conversation, run=run.id, draft=response.json())


def gated_checker(monkeypatch):
    started, release = asyncio.Event(), asyncio.Event()

    class Checker:
        def __init__(self, *args, **kwargs):
            pass

        async def structured(self, prompt, payload, schema, **kwargs):
            started.set()
            await release.wait()
            return SimpleNamespace(
                value=schema(supported=True, issues=[]),
                usage=ProviderUsage(operation="draft_recheck", model="qa-provider-double", latency_ms=1),
            )

    monkeypatch.setattr(drafts, "TrackedOpenRouterClient", Checker)
    return started, release


async def test_saved_objects_stay_owned_after_reload_and_session_reset(isolated_http):
    fixture = isolated_http
    async with fixture.visitor() as owner, fixture.visitor() as other:
        saved = await saved_answer(fixture, owner)
        await other.post("/api/v1/session")
        same = await owner.post("/api/v1/drafts", json={"run_id": saved.run})
        assert same.json()["id"] == saved.draft["id"]
        did, cid, rid = saved.draft["id"], saved.conversation, saved.run
        forbidden = [
            ("GET", f"/api/v1/drafts/{did}", None),
            ("PATCH", f"/api/v1/drafts/{did}", {"text": "Foreign edit", "expected_version": 1}),
            ("POST", f"/api/v1/drafts/{did}/recheck", None),
            ("POST", "/api/v1/drafts", {"run_id": rid}),
            ("GET", f"/api/v1/runs/{rid}", None),
            ("GET", f"/api/v1/runs/{rid}/events", None),
            ("POST", f"/api/v1/runs/{rid}/cancel", None),
            ("GET", f"/api/v1/conversations/{cid}", None),
            ("PATCH", f"/api/v1/conversations/{cid}/context", {"expected_version": 1}),
        ]
        for method, path, body in forbidden:
            result = await other.request(method, path, json=body)
            assert result.status_code == 404, (method, path, result.text)
        assert (await other.get("/api/v1/drafts", params={"run_id": rid})).json()["items"] == []
        assert (await other.get("/api/v1/diagnostics")).json()["items"] == []
        # New connections and reconstructed source cache: these remain real persisted records.
        bundles._cache.clear()
        await fixture.engine.dispose()
        async with fixture.visitor(cookies=owner.cookies) as reloaded:
            history = (await reloaded.get(f"/api/v1/conversations/{cid}")).json()
            assert [run["id"] for run in history["runs"]] == [rid]
            citation = history["runs"][0]["answer"]["statements"][0]["citations"][0]
            source = (await reloaded.get("/api/v1/sources/PT-006", params={"index_id": citation["index_id"]})).json()
            assert source["text"][citation["start"]:citation["end"]] == citation["quote"]
            assert (await reloaded.get(f"/api/v1/drafts/{did}")).json()["text"] == saved.draft["text"]
        old_cookies = httpx.Cookies(owner.cookies)
        reset = await owner.post("/api/v1/session/reset")
        assert reset.status_code == 200 and reset.json()["id"] != saved.owner
        for path in [f"/api/v1/drafts/{did}", f"/api/v1/runs/{rid}", f"/api/v1/conversations/{cid}"]:
            assert (await owner.get(path)).status_code == 404
        async with fixture.visitor(cookies=old_cookies) as old_session:
            assert (await old_session.get(f"/api/v1/drafts/{did}")).status_code == 401
        assert (await owner.get("/api/v1/sources/PT-006")).status_code == 200


@pytest.mark.parametrize("interruption", ["edit", "reset"])
async def test_in_flight_recheck_cannot_validate_after_http_edit_or_reset(isolated_http, monkeypatch, interruption):
    fixture = isolated_http
    async with fixture.visitor() as client:
        saved = await saved_answer(fixture, client)
        path = "/api/v1/drafts/" + saved.draft["id"]
        started, release = gated_checker(monkeypatch)
        checking = asyncio.create_task(client.post(path + "/recheck"))
        try:
            await asyncio.wait_for(started.wait(), 2)
            if interruption == "edit":
                # Two browser edits of one frozen version: one wins, one receives a conflict.
                updates = await asyncio.gather(*(
                    client.patch(path, json={"text": text, "expected_version": 1})
                    for text in ["New saved text A", "New saved text B"]
                ))
                assert sorted(r.status_code for r in updates) == [200, 409]
                winning_text = next(r.json()["text"] for r in updates if r.status_code == 200)
            else:
                assert (await client.post("/api/v1/session/reset")).status_code == 200
            release.set()
            result = await asyncio.wait_for(checking, 2)
            assert result.status_code == 409, result.text
            expected = "DRAFT_CHANGED" if interruption == "edit" else "CHECK_INTERRUPTED"
            assert result.json()["detail"]["code"] == expected
            async with fixture.session() as db:
                stored = await db.get(Draft, saved.draft["id"])
                assert stored.check is None and stored.check_owner is None
                if interruption == "edit":
                    assert stored.version == 2 and stored.text == winning_text
                else:
                    assert (await db.get(BrowserSession, saved.owner)).revoked
        finally:
            release.set()
            await asyncio.gather(checking, return_exceptions=True)


async def test_temporal_range_returns_saved_clarification_without_provider_dispatch(isolated_http):
    async with isolated_http.visitor() as client:
        assert (await client.post("/api/v1/session")).status_code == 200
        cid = (await client.post("/api/v1/conversations", json={})).json()["id"]
        result = await client.post(f"/api/v1/conversations/{cid}/runs", json={
            "question": "What was the cancellation policy before July 1, 2026?",
            "context_version": 1, "idempotency_key": uuid4().hex,
        })
        assert result.status_code == 202, result.text
        rid = result.json()["id"]
        async with asyncio.timeout(3):
            while True:
                result = (await client.get(f"/api/v1/runs/{rid}")).json()
                if result["status"] in runs.TERMINAL:
                    break
                await asyncio.sleep(0.02)
        assert result["status"] == "completed", result.get("error")
        assert result["answer"]["status"] == "clarification"
        assert "exact date" in result["answer"]["gaps"][0]["text"].lower()
        assert result["answer"]["statements"] == [] and result["provider_attempts"] == []
