"""Real HTTP and isolated migrated Postgres. No provider calls."""

import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import psycopg
from emer.domain.recap_policy import RECAP_HASH_PREFIX
from test_http_postgres import conversation, visitor
from test_http_postgres import server as _server_fixture

server = _server_fixture


def seed_saved(dsn, owner, cid, index_id, *, question="Question about cancellation", answer="Saved answer"):
    run_id, live_id = str(uuid4()), str(uuid4())
    now = datetime.now(UTC)
    published = {
        "status": "partial", "statements": [{"part_id": "one", "text": answer, "citations": []}],
        "gaps": [{"part_id": "one", "text": "An exact amount remains unknown."}], "next_steps": [],
        "scopes": [], "index_id": index_id, "index_checksum": "fixture", "usage": [],
        "diagnostics": {"private": "must not be shared"},
    }
    with psycopg.connect(dsn) as db:
        db.execute(
            """INSERT INTO runs
            (id,session_id,conversation_id,index_id,idempotency_key,body_hash,question,context_version,
             context,status,answer,metrics,cancel_requested,fence,attempt_started,created_at,completed_at)
             VALUES (%s,%s,%s,%s,%s,'fixture',%s,1,'{}','completed',%s,'{}',false,1,false,%s,%s)""",
            (run_id, owner, cid, index_id, run_id, question, json.dumps(published), now, now),
        )
        db.execute(
            """INSERT INTO live_sessions
            (id,session_id,conversation_id,index_id,context_version,context,idempotency_key,body_hash,
             provider_session_id,snapshot,status,fence,created_at)
             VALUES (%s,%s,%s,%s,1,'{}',%s,'fixture',%s,%s,'closed',1,%s)""",
            (live_id, owner, cid, index_id, live_id, "provider-" + live_id,
             json.dumps({"usage_seconds": 15, "final_usage_confirmed": True}), now),
        )
        for kind, text in [
            ("session.input_transcript.delta", "Tell me about the policy."),
            ("session.output_transcript.delta", "Voice-only telescope phrase"),
        ]:
            db.execute(
                """INSERT INTO live_events(live_session_id,provider_event_id,kind,payload)
                   VALUES (%s,%s,%s,%s)""",
                (live_id, str(uuid4()), kind, json.dumps({"delta": text, "start_ms": 0, "end_ms": 20})),
            )
        db.execute(
            """INSERT INTO provider_attempts
               (id,session_id,run_id,operation,model,status,usage,created_at)
               VALUES (%s,%s,%s,'generation','fixture','completed',%s,%s)""",
            (str(uuid4()), owner, run_id, json.dumps({"cost": 0.005, "total_tokens": 13}), now),
        )
    return run_id, live_id


def test_creation_titles_remain_auto_until_title_is_explicitly_patched(server):
    url, _ = server
    with visitor(url) as client:
        for title in ("New conversation", "Voice conversation", "A first typed question " * 6):
            created = client.post("/api/v1/conversations", json={"title": title[:120]})
            assert created.status_code == 201, created.text
            row = created.json()
            assert row["title"] == title[:120] and row["title_origin"] == "auto"
            path = f"/api/v1/conversations/{row['id']}"
            assert client.patch(path, json={"pinned": True}).json()["title_origin"] == "auto"
            renamed = client.patch(path, json={"title": "My manual title"})
            assert renamed.json()["title_origin"] == "manual"
            assert client.patch(path, json={"pinned": False}).json()["title_origin"] == "manual"
            assert client.get(path).json()["title"] == "My manual title"


def test_persistent_pin_rename_search_and_stable_offset_pagination(server):
    url, dsn = server
    with visitor(url) as client, visitor(url) as other:
        sid = client.post("/api/v1/session").json()["id"]
        ids = [conversation(client) for _ in range(56)]
        index_id = client.get("/api/v1/corpus").json()["index_id"]
        run_id, _ = seed_saved(dsn, sid, ids[0], index_id, question="Buried saffron question")
        with psycopg.connect(dsn) as db:
            db.execute("UPDATE conversations SET updated_at='2026-01-01' WHERE session_id=%s", (sid,))
            db.execute("UPDATE conversations SET summary='Recap-only cobalt', summary_status='ready', "
                       "summary_input_hash=%s WHERE id=%s", (RECAP_HASH_PREFIX + "0" * 60, ids[1]))
        assert client.patch(f"/api/v1/conversations/{ids[0]}", json={"pinned": True}).status_code == 200
        renamed = client.patch(f"/api/v1/conversations/{ids[0]}", json={"title": "  Manual topic  "})
        assert renamed.json()["title"] == "Manual topic" and renamed.json()["title_origin"] == "manual"
        first = client.get("/api/v1/conversations").json()
        assert len(first["items"]) == 50 and first["items"][0]["id"] == ids[0]
        second = client.get("/api/v1/conversations", params={"offset": first["next_offset"]}).json()
        assert second["next_offset"] is None
        ordered = [row["id"] for page in [first, second] for row in page["items"]]
        assert len(set(ordered)) == 56 and ordered[1:] == sorted(set(ids) - {ids[0]}, reverse=True)
        for query, wanted in [
            ("saffron", ids[0]), ("cobalt", ids[1]), ("TELESCOPE", ids[0]), ("Manual topic", ids[0]),
        ]:
            response = client.get("/api/v1/conversations", params={"q": query}).json()
            assert [row["id"] for row in response["items"]] == [wanted]
            assert other.get("/api/v1/conversations", params={"q": query}).json()["items"] == []
        assert client.get("/api/v1/conversations", params={"q": "%"}).json()["items"] == []
        assert client.get("/api/v1/conversations", params={"q": "_"}).json()["items"] == []
        assert client.patch(f"/api/v1/conversations/{ids[0]}", json={"pinned": False}).json()["pinned"] is False
        assert client.patch(f"/api/v1/conversations/{ids[0]}", json={"title": "   "}).status_code == 422
        assert other.patch(f"/api/v1/conversations/{ids[0]}", json={"title": "wrong"}).status_code == 404
        detail = client.get(f"/api/v1/conversations/{ids[0]}").json()
        assert detail["runs"][0]["id"] == run_id and detail["next_cursor"] is None
        assert [part["speaker"] for part in detail["transcripts"]] == ["user", "assistant"]
        assert all(key in detail for key in (
            "pinned", "updated_at", "summary", "summary_status", "summary_error", "title_origin"
        ))
        live_id = detail["transcripts"][0]["live_session_id"]
        with psycopg.connect(dsn) as db:
            for fragment in ("straw", "berry season"):
                db.execute("INSERT INTO live_events(live_session_id,provider_event_id,kind,payload) "
                           "VALUES (%s,%s,'session.input_transcript.delta',%s)",
                           (live_id, str(uuid4()), json.dumps({"delta": fragment})))
        across_fragments = client.get("/api/v1/conversations", params={"q": "strawberry season"}).json()
        assert [item["id"] for item in across_fragments["items"]] == [ids[0]]


def test_retired_recap_is_excluded_from_search_without_hiding_canonical_answers(server):
    url, dsn = server
    with visitor(url) as client:
        sid = client.post("/api/v1/session").json()["id"]
        cid = conversation(client)
        rid, _ = seed_saved(dsn, sid, cid, client.get("/api/v1/corpus").json()["index_id"],
                            question="Canonical saffron question", answer="Canonical harbor answer")
        with psycopg.connect(dsn) as db:
            db.execute("UPDATE conversations SET summary='Retired cobalt recap', summary_status='ready', "
                       "summary_input_hash=%s WHERE id=%s", ("rx7:" + "0" * 60, cid))
        assert client.get("/api/v1/conversations", params={"q": "cobalt"}).json()["items"] == []
        for query in ("saffron", "harbor"):
            rows = client.get("/api/v1/conversations", params={"q": query}).json()["items"]
            assert [row["id"] for row in rows] == [cid]
        detail = client.get(f"/api/v1/conversations/{cid}").json()
        assert detail["runs"][0]["id"] == rid
        assert detail["runs"][0]["answer"]["statements"][0]["text"] == "Canonical harbor answer"
        with psycopg.connect(dsn) as db:
            # Presentation/search retirement is not destructive data cleanup.
            assert db.execute("SELECT summary FROM conversations WHERE id=%s", (cid,)).fetchone()[0] == (
                "Retired cobalt recap"
            )


def test_explicit_share_is_frozen_sanitized_revocable_and_no_owner_bootstrap(server):
    url, dsn = server
    with visitor(url) as client, visitor(url) as other, httpx.Client(base_url=url) as public:
        cid = conversation(client)
        sid = client.post("/api/v1/session").json()["id"]
        rid, _ = seed_saved(dsn, sid, cid, client.get("/api/v1/corpus").json()["index_id"])
        assert public.get("/api/v1/shared/" + "x" * 43).status_code == 404
        response = client.post(f"/api/v1/conversations/{cid}/share")
        assert response.status_code == 201, response.text
        created = response.json()
        token = created["url"].split("/")[-1]
        assert len(token) >= 43 and "Anyone with this link" in created["disclosure"]
        snapshot = public.get("/api/v1/shared/" + token)
        assert snapshot.status_code == 200 and "set-cookie" not in snapshot.headers
        assert snapshot.headers["cache-control"] == "no-store"
        value = snapshot.json()
        assert "Saved answer" in json.dumps(value) and "telescope" in json.dumps(value)
        assert not ({"id", "session_id", "runs", "provider_attempts", "metrics", "diagnostics"} & set(value))
        assert sid not in snapshot.text and rid not in snapshot.text and "must not be shared" not in snapshot.text
        page = public.get(created["url"])
        assert page.status_code == 200 and page.content == public.get("/").content
        assert page.headers["content-type"].startswith("text/html")
        assert page.headers["cache-control"] == "no-store"
        assert page.headers["referrer-policy"] == "no-referrer"
        assert "set-cookie" not in page.headers and not public.cookies
        with psycopg.connect(dsn) as db:
            saved = db.execute("SELECT token_hash,snapshot FROM conversation_shares WHERE conversation_id=%s",
                               (cid,)).fetchone()
            assert saved[0] != token and token not in json.dumps(saved[1])
            db.execute("UPDATE runs SET question='Changed after share' WHERE id=%s", (rid,))
        assert public.get("/api/v1/shared/" + token).json() == value
        assert other.post(f"/api/v1/conversations/{cid}/share").status_code == 404
        assert other.delete(f"/api/v1/conversations/{cid}/share").status_code == 404
        assert client.delete(f"/api/v1/conversations/{cid}/share").status_code == 204
        assert public.get("/api/v1/shared/" + token).status_code == 404
        # Replacement publication rotates the random capability; old copies cannot be revived.
        next_token = client.post(f"/api/v1/conversations/{cid}/share").json()["url"].split("/")[-1]
        assert next_token != token
        client.post("/api/v1/session/reset")
        assert public.get("/api/v1/shared/" + next_token).status_code == 404


def test_delete_rejects_active_work_then_revokes_and_preserves_accounting(server):
    url, dsn = server
    with visitor(url) as client, visitor(url) as other:
        cid = conversation(client)
        sid = client.post("/api/v1/session").json()["id"]
        rid, lid = seed_saved(dsn, sid, cid, client.get("/api/v1/corpus").json()["index_id"])
        token = client.post(f"/api/v1/conversations/{cid}/share").json()["url"].split("/")[-1]
        assert other.delete(f"/api/v1/conversations/{cid}").status_code == 404
        with psycopg.connect(dsn) as db:
            db.execute("UPDATE runs SET status='running',lease_expires_at=%s WHERE id=%s",
                       (datetime.now(UTC) + timedelta(minutes=2), rid))
        assert client.delete(f"/api/v1/conversations/{cid}").status_code == 409
        assert client.post(f"/api/v1/conversations/{cid}/summary").status_code == 409
        with psycopg.connect(dsn) as db:
            db.execute("UPDATE runs SET status='completed',lease_expires_at=NULL WHERE id=%s", (rid,))
            db.execute("UPDATE live_sessions SET status='listening' WHERE id=%s", (lid,))
        assert client.delete(f"/api/v1/conversations/{cid}").status_code == 409
        with psycopg.connect(dsn) as db:
            db.execute("UPDATE live_sessions SET status='closed' WHERE id=%s", (lid,))
        deleted = client.delete(f"/api/v1/conversations/{cid}")
        assert deleted.status_code == 204, deleted.text
        assert client.get(f"/api/v1/conversations/{cid}").status_code == 404
        assert client.get(f"/api/v1/runs/{rid}").status_code == 404
        assert client.get("/api/v1/shared/" + token).status_code == 404
        with psycopg.connect(dsn) as db:
            receipts = db.execute("SELECT operation,usage,run_id,live_session_id FROM provider_attempts "
                                  "WHERE session_id=%s ORDER BY operation", (sid,)).fetchall()
            assert len(receipts) == 2
            assert receipts[0][0] == "generation" and receipts[0][1]["cost"] == 0.005
            assert receipts[1][0] == "live_usage_archive"
            assert receipts[1][1]["duration_seconds"] == 15
            assert receipts[1][1]["final_usage_confirmed"] is True
            assert all(row[2:] == (None, None) for row in receipts)


def test_complete_answer_share_limits_transcript_pages_and_receipt_ownership(server):
    url, dsn = server
    with visitor(url) as client, visitor(url) as other:
        cid = conversation(client)
        sid = client.post("/api/v1/session").json()["id"]
        rid, lid = seed_saved(dsn, sid, cid, client.get("/api/v1/corpus").json()["index_id"],
                             answer="x" * 25_000 + "END OF COMPLETE ANSWER")
        published = client.post(f"/api/v1/conversations/{cid}/share")
        token = published.json()["url"].split("/")[-1]
        assert "END OF COMPLETE ANSWER" in client.get("/api/v1/shared/" + token).text
        with psycopg.connect(dsn) as db:
            for n in range(501):
                db.execute("INSERT INTO live_events(live_session_id,provider_event_id,kind,payload) "
                           "VALUES (%s,%s,'session.output_transcript.delta',%s)",
                           (lid, str(uuid4()), json.dumps({"delta": f"part {n}", "start_ms": n, "end_ms": n + 1})))
        first = client.get(f"/api/v1/conversations/{cid}/transcripts").json()
        assert len(first["items"]) == 500 and first["next_cursor"] is not None
        second = client.get(f"/api/v1/conversations/{cid}/transcripts",
                            params={"after": first["next_cursor"]}).json()
        assert len(second["items"]) == 3 and second["next_cursor"] is None
        assert other.get(f"/api/v1/conversations/{cid}/transcripts").status_code == 404
        assert other.get(f"/api/v1/conversations/{cid}/summary/attempts").status_code == 404
        with psycopg.connect(dsn) as db:
            db.execute("UPDATE runs SET answer=jsonb_set(answer,'{statements,0,text}',%s::jsonb) WHERE id=%s",
                       (json.dumps("x" * 1_000_001), rid))
        assert client.post(f"/api/v1/conversations/{cid}/share").status_code == 422
        assert client.get("/api/v1/shared/" + token).status_code == 200
