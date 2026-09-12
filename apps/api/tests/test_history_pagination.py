"""Real HTTP/Postgres pagination over seeded archival rows; no provider or audio calls."""

from datetime import UTC, datetime
from uuid import UUID

import psycopg
from test_http_postgres import conversation, visitor
from test_http_postgres import server as _server_fixture

server = _server_fixture


def seed_archive(dsn, owner, conversation_id, index_id, count=121, start=0):
    run_ids = [str(UUID(int=1000 + start + n)) for n in range(count)]
    draft_ids = [str(UUID(int=200000 + start + n)) for n in range(count)]
    # All timestamps intentionally tie. IDs must provide deterministic ordering.
    created = datetime(2026, 1, 1, tzinfo=UTC)
    with psycopg.connect(dsn) as db:
        for n, (run_id, draft_id) in enumerate(zip(run_ids, draft_ids, strict=True)):
            db.execute(
                """INSERT INTO runs
                (id,session_id,conversation_id,index_id,idempotency_key,body_hash,question,context_version,
                 context,status,metrics,cancel_requested,fence,attempt_started,created_at,completed_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,1,'{}','completed','{}',false,1,false,%s,%s)""",
                (
                    run_id,
                    owner,
                    conversation_id,
                    index_id,
                    run_id,
                    "0" * 64,
                    f"Seeded archival question {start + n}",
                    created,
                    created,
                ),
            )
            db.execute(
                """INSERT INTO drafts (id,session_id,run_id,index_id,text,version,created_at)
                VALUES (%s,%s,%s,%s,%s,1,%s)""",
                (draft_id, owner, run_id, index_id, f"Seeded archival draft {start + n}", created),
            )
    return run_ids, draft_ids


def test_earlier_answers_are_stable_across_new_insertions_and_tied_timestamps(server):
    url, dsn = server
    with visitor(url) as client:
        owner = client.post("/api/v1/session").json()["id"]
        cid = conversation(client)
        index_id = client.get("/api/v1/corpus").json()["index_id"]
        ids, _ = seed_archive(dsn, owner, cid, index_id)
        first = client.get(f"/api/v1/conversations/{cid}").json()
        assert [run["id"] for run in first["runs"]] == ids[71:]
        assert first["next_cursor"] == ids[71]
        # A newer row arrives between page requests. Earlier cursor pages do not shift.
        new_ids, _ = seed_archive(dsn, owner, cid, index_id, count=1, start=1000)
        second = client.get(f"/api/v1/conversations/{cid}", params={"before": first["next_cursor"]}).json()
        third = client.get(f"/api/v1/conversations/{cid}", params={"before": second["next_cursor"]}).json()
        assert [run["id"] for run in second["runs"]] == ids[21:71]
        assert [run["id"] for run in third["runs"]] == ids[:21]
        assert third["next_cursor"] is None
        assert len({run["id"] for page in (first, second, third) for run in page["runs"]}) == 121
        latest = client.get(f"/api/v1/conversations/{cid}").json()
        assert latest["runs"][-1]["id"] == new_ids[0]
        past_end = client.get(f"/api/v1/conversations/{cid}", params={"before": ids[0]}).json()
        assert past_end["runs"] == [] and past_end["next_cursor"] is None


def test_draft_pages_reach_old_work_and_run_filter_does_not_scan_newest_fifty(server):
    url, dsn = server
    with visitor(url) as client:
        owner = client.post("/api/v1/session").json()["id"]
        cid = conversation(client)
        index_id = client.get("/api/v1/corpus").json()["index_id"]
        runs, ids = seed_archive(dsn, owner, cid, index_id, start=2000)
        first = client.get("/api/v1/drafts").json()
        assert [item["id"] for item in first["items"]] == list(reversed(ids[71:]))
        seed_archive(dsn, owner, cid, index_id, count=1, start=4000)
        second = client.get("/api/v1/drafts", params={"before": first["next_cursor"]}).json()
        third = client.get("/api/v1/drafts", params={"before": second["next_cursor"]}).json()
        assert [item["id"] for item in second["items"]] == list(reversed(ids[21:71]))
        assert [item["id"] for item in third["items"]] == list(reversed(ids[:21]))
        assert third["next_cursor"] is None
        assert len({item["id"] for page in (first, second, third) for item in page["items"]}) == 121
        old_draft = client.get("/api/v1/drafts", params={"run_id": runs[0]}).json()
        assert [item["id"] for item in old_draft["items"]] == [ids[0]]
        assert old_draft["next_cursor"] is None


def test_empty_missing_foreign_and_expired_pagination_stays_owned(server):
    url, dsn = server
    with visitor(url) as owner, visitor(url) as other:
        sid = owner.post("/api/v1/session").json()["id"]
        cid = conversation(owner)
        empty = owner.get(f"/api/v1/conversations/{cid}").json()
        assert empty["runs"] == [] and empty["next_cursor"] is None
        assert owner.get("/api/v1/drafts").json() == {"items": [], "next_cursor": None}
        index_id = owner.get("/api/v1/corpus").json()["index_id"]
        runs, drafts = seed_archive(dsn, sid, cid, index_id, count=1, start=6000)
        other_cid = conversation(other)
        assert other.get(f"/api/v1/conversations/{cid}", params={"before": runs[0]}).status_code == 404
        assert other.get(f"/api/v1/conversations/{other_cid}", params={"before": runs[0]}).status_code == 404
        assert other.get("/api/v1/drafts", params={"before": drafts[0]}).status_code == 404
        assert other.get("/api/v1/drafts", params={"run_id": runs[0]}).json()["items"] == []
        assert owner.get(f"/api/v1/conversations/{cid}", params={"before": "missing"}).status_code == 404
        assert owner.get("/api/v1/drafts", params={"before": "missing"}).status_code == 404
        with psycopg.connect(dsn) as db:
            db.execute("UPDATE browser_sessions SET expires_at = '2000-01-01' WHERE id = %s", (sid,))
        assert owner.get(f"/api/v1/conversations/{cid}", params={"before": runs[0]}).status_code == 401
        assert owner.get("/api/v1/drafts", params={"before": drafts[0]}).status_code == 401
