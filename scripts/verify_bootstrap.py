"""Exercise the production entrypoint on a disposable local Postgres database, no paid calls."""
import json
import os
import socket
import subprocess
import time
from pathlib import Path
from uuid import uuid4

import httpx
import psycopg

root = Path(__file__).resolve().parents[1]
name = "emer_bootstrap_test_" + uuid4().hex[:12]
url = "http://localhost:18019"
env = {
    **os.environ,
    "DATABASE_URL": "postgresql+psycopg://localhost:55432/" + name,
    "OPENROUTER_API_KEY": "",
    "OPENAI_API_KEY": "",
    "APP_ORIGIN": url,
    "COOKIE_SECURE": "false",
    "PORT": "18019",
}
report = {"classification": "actual local production entrypoint; not hosted cold-wake or provider proof", "starts": []}
with socket.socket() as sock:
    sock.bind(("127.0.0.1", 18019))
with psycopg.connect("postgresql://localhost:55432/postgres", autocommit=True) as admin:
    admin.execute(f"CREATE DATABASE {name}")
try:
    for number in range(2):
        with (root / ".local/bootstrap-test.log").open("a") as log:
            began = time.monotonic()
            process = subprocess.Popen(["bash", "deployment/start.sh"], cwd=root, env=env, stdout=log, stderr=log, start_new_session=True)
            try:
                with httpx.Client(base_url=url, headers={"Origin": url}, timeout=2) as client:
                    for _ in range(150):
                        if process.poll() is not None:
                            raise RuntimeError("Entrypoint exited; inspect .local/bootstrap-test.log")
                        try:
                            if client.get("/api/v1/health/ready").status_code == 200:
                                break
                        except httpx.TransportError:
                            pass
                        time.sleep(0.1)
                    else:
                        raise RuntimeError("Readiness deadline exceeded")
                    corpus = client.get("/api/v1/corpus").json()
                    assert corpus["count"] == 35
                    assert client.get("/").status_code == 200
                    session = client.post("/api/v1/session")
                    session.raise_for_status()
                    conversation = client.post("/api/v1/conversations", json={"title": "Entrypoint verification"})
                    assert conversation.status_code == 201
                    report["starts"].append({"number": number + 1, "ready_ms": round((time.monotonic()-began)*1000), "documents": corpus["count"], "index_id": corpus["index_id"], "static_page": True, "session_and_conversation": True})
            finally:
                # Only this disposable process group, including uv's server child.
                import signal
                os.killpg(process.pid, signal.SIGTERM)
                process.wait(timeout=10)
    with psycopg.connect("postgresql://localhost:55432/" + name) as db:
        report["index_count_after_restart"] = db.execute("SELECT count(*) FROM corpus_indexes").fetchone()[0]
        report["conversation_count_after_restart"] = db.execute("SELECT count(*) FROM conversations").fetchone()[0]
    assert report["index_count_after_restart"] == 1 and report["conversation_count_after_restart"] == 2
    out = root / "artifacts/verification/2026-09-11-reproducibility/entrypoint.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report))
finally:
    with psycopg.connect("postgresql://localhost:55432/postgres", autocommit=True) as admin:
        admin.execute(f"DROP DATABASE {name} WITH (FORCE)")
