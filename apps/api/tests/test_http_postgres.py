"""Actual HTTP and isolated PostgreSQL; generation deliberately unconfigured, never faked."""

import os
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

import httpx
import psycopg
import pytest


@pytest.fixture(scope="module")
def server(request):
    name = "emer_test_" + uuid4().hex[:12]
    admin = "postgresql://localhost:55432/postgres"
    with psycopg.connect(admin, autocommit=True) as db:
        db.execute(f"CREATE DATABASE {name}")
    env = {
        **os.environ,
        "DATABASE_URL": f"postgresql+psycopg://localhost:55432/{name}",
        "OPENROUTER_API_KEY": "",
        "OPENAI_API_KEY": "",
        "RETRIEVAL_MODE": "lexical",
        "RERANKING_ENABLED": "false",
        "SHARED_WORKSPACE": "true" if getattr(request, "param", False) else "false",
        "APP_ORIGIN": "http://localhost:18017",
    }
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 18017))
    log = Path(".local/http-test.log").open("w")  # noqa: SIM115 - subprocess fixture closes it in finally
    process = None
    try:
        subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"], env=env, check=True, stdout=log, stderr=log
        )
        subprocess.run(
            [sys.executable, "-c", "from emer.cli import ingest; ingest()"],
            env=env,
            check=True,
            stdout=log,
            stderr=log,
        )
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "emer.api.app:app",
                "--host",
                "127.0.0.1",
                "--port",
                "18017",
                "--no-access-log",
            ],
            env=env,
            stdout=log,
            stderr=log,
        )
        for _ in range(50):
            try:
                if httpx.get("http://localhost:18017/api/v1/health/ready").status_code == 200:
                    break
            except httpx.TransportError:
                pass
            time.sleep(0.1)
        else:
            pytest.fail("Isolated API startup failed; inspect .local/http-test.log")
        yield "http://localhost:18017", f"postgresql://localhost:55432/{name}"
    finally:
        if process:
            process.terminate()
            process.wait(timeout=10)
        log.close()
        with psycopg.connect(admin, autocommit=True) as db:
            db.execute(f"DROP DATABASE {name} WITH (FORCE)")


@contextmanager
def visitor(url):
    with httpx.Client(base_url=url, headers={"Origin": url}) as client:
        assert client.post("/api/v1/session").status_code == 200
        yield client


def conversation(client):
    result = client.post("/api/v1/conversations", json={})
    assert result.status_code == 201, result.text
    return result.json()["id"]


def await_run(client, run_id):
    for _ in range(50):
        data = client.get("/api/v1/runs/" + run_id).json()
        if data["status"] in {"completed", "failed", "cancelled", "superseded", "interrupted"}:
            return data
        time.sleep(0.1)
    pytest.fail("Run never reached terminal state")


def test_ownership_origin_cookie_and_reset(server):
    url, _ = server
    with visitor(url) as a, visitor(url) as b:
        cid = conversation(a)
        assert a.get("/api/v1/conversations/" + cid).status_code == 200
        assert b.get("/api/v1/conversations/" + cid).status_code == 404
        assert (
            a.post(
                "/api/v1/conversations", json={}, headers={"Origin": "https://malicious.example"}
            ).status_code
            == 403
        )
        response = a.post("/api/v1/session")
        original = response.json()["id"]
        assert response.json()["id"] == a.post("/api/v1/session").json()["id"]
        reset = a.post("/api/v1/session/reset")
        assert reset.status_code == 200, reset.text
        assert reset.json()["id"] != original
        assert "HttpOnly" in reset.headers["set-cookie"] and "SameSite=strict" in reset.headers["set-cookie"]
        assert a.get("/api/v1/conversations/" + cid).status_code == 404


def test_real_failed_run_idempotency_history_sse_and_sources(server):
    url, _ = server
    with visitor(url) as client, visitor(url) as other:
        cid = conversation(client)
        body = {"question": "What is known for PT-006?", "context_version": 1, "idempotency_key": uuid4().hex}
        submitted = client.post(f"/api/v1/conversations/{cid}/runs", json=body)
        assert submitted.status_code == 202, submitted.text
        rid = submitted.json()["id"]
        assert client.post(f"/api/v1/conversations/{cid}/runs", json=body).json()["id"] == rid
        changed = {**body, "question": "Another question"}
        assert client.post(f"/api/v1/conversations/{cid}/runs", json=changed).status_code == 409
        result = await_run(client, rid)
        assert result["status"] == "failed"
        assert result["error"]["code"] == "PROVIDER_NOT_CONFIGURED"
        assert result["answer"] is None
        assert other.get("/api/v1/runs/" + rid).status_code == 404
        assert other.get("/api/v1/runs/" + rid + "/events").status_code == 404
        history = client.get("/api/v1/conversations/" + cid).json()["runs"]
        assert [r["id"] for r in history] == [rid]
        event_ids = [e["sequence"] for e in result["events"]]
        assert event_ids == list(range(1, len(event_ids) + 1))
        replay = client.get("/api/v1/runs/" + rid + "/events", headers={"Last-Event-ID": "1"})
        assert "id: 1\n" not in replay.text and "event: run" in replay.text
        source = client.get("/api/v1/sources/PT-006", params={"index_id": result["index_id"]})
        assert source.status_code == 200 and source.json()["category"] == "synthetic_patient"
        assert client.post("/api/v1/drafts", json={"run_id": rid}).status_code == 409


def test_context_conflict_and_no_patient_substitution(server):
    url, _ = server
    with visitor(url) as client:
        cid = conversation(client)
        path = f"/api/v1/conversations/{cid}/context"
        assert client.patch(path, json={"patient_id": "PT-009", "expected_version": 1}).status_code == 422
        assert client.patch(path, json={"as_of": "2026-02-31", "expected_version": 1}).status_code == 422
        updated = client.patch(path, json={"patient_id": "PT-005", "expected_version": 1})
        assert updated.json()["context_version"] == 2
        assert client.patch(path, json={"patient_id": "PT-006", "expected_version": 1}).status_code == 409
        assert (
            client.post(
                f"/api/v1/conversations/{cid}/runs",
                json={"question": "What is known?", "context_version": 1, "idempotency_key": uuid4().hex},
            ).status_code
            == 409
        )


def test_bundle_rebuild_and_no_static_secret_route(server):
    url, dsn = server
    with visitor(url) as client:
        corpus = client.get("/api/v1/corpus").json()
        assert corpus["count"] == 35
        assert len(client.get("/api/v1/patients").json()["items"]) == 8
        assert client.get("/runtime.env").status_code == 404
        assert client.get("/.local/postgres/PG_VERSION").status_code == 404
        assert client.get("/api/v1/sources/PT-009").status_code == 404
        with psycopg.connect(dsn) as db:
            row = db.execute(
                "SELECT checksum,bundle FROM corpus_indexes WHERE id=%s", (corpus["index_id"],)
            ).fetchone()
        from emer.services.ingestion import Bundle

        assert len(Bundle.from_bytes(bytes(row[1]), row[0]).documents) == 35


def test_chunked_body_limit(server):
    url, _ = server
    with visitor(url) as client:

        def chunks():
            yield b'{"title":"'
            for _ in range(12):
                yield b"x" * 10000
            yield b'"}'

        response = client.post(
            "/api/v1/conversations", content=chunks(), headers={"Content-Type": "application/json"}
        )
        assert response.status_code == 413, response.text
