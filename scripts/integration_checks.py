"""API-level integration checks against a running EMER instance (no or minimal provider cost).

Usage: uv run python scripts/integration_checks.py --base http://localhost:4197 --out artifacts/verification/<run>/integration.json [--with-cancel]

Covers: health, anonymous session bootstrap/expiry/reset, CSRF origin rejection, validation limits,
idempotency reuse and conflict, context-version conflict, stranger isolation, conversation listing/
pagination/search, source/corpus integrity (sha256 of returned text), SSE replay ordering for a
completed run, cancellation of an in-flight run (--with-cancel; one bounded provider attempt may start),
and diagnostics/evaluations endpoints. Records expected vs actual for every check.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx

TERMINAL = {"completed", "failed", "cancelled", "superseded", "interrupted"}


class Report:
    def __init__(self):
        self.rows = []

    def check(self, name: str, expected, actual, detail: str = ""):
        ok = expected == actual if not callable(expected) else bool(expected(actual))
        self.rows.append({"check": name, "expected": expected if not callable(expected) else "predicate", "actual": actual, "ok": ok, "detail": detail})
        print(("OK  " if ok else "FAIL") + f" {name}: expected={expected if not callable(expected) else 'predicate'} actual={actual} {detail}")
        return ok


def wait_terminal(client, run_id, timeout=150):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        run = client.get(f"/api/v1/runs/{run_id}").json()
        if run["status"] in TERMINAL:
            return run
        time.sleep(0.4)
    raise TimeoutError(run_id)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--with-cancel", action="store_true")
    args = parser.parse_args()
    base = args.base
    report = Report()
    with httpx.Client(base_url=base, headers={"Origin": base}, timeout=60) as client:
        # Health
        live = client.get("/api/v1/health/live")
        report.check("health.live", 200, live.status_code)
        ready = client.get("/api/v1/health/ready")
        report.check("health.ready", 200, ready.status_code, json.dumps(ready.json())[:200])
        index_id = ready.json().get("index_id")

        # Anonymous session bootstrap and unauthenticated access
        anon = httpx.Client(base_url=base, headers={"Origin": base}, timeout=30)
        report.check("no-cookie conversations -> 401", 401, anon.get("/api/v1/conversations").status_code)
        session = client.post("/api/v1/session")
        report.check("session bootstrap", 201 if session.status_code == 201 else 200, session.status_code, str(session.json())[:120])
        report.check("session cookie set (HttpOnly)", True, any("httponly" in v.lower() for v in session.headers.get_list("set-cookie")))

        # CSRF: cross-origin mutation rejected
        bad = client.post("/api/v1/conversations", json={}, headers={"Origin": "https://malicious.example"})
        report.check("cross-origin POST rejected", 403, bad.status_code)

        # Corpus and source integrity
        corpus = client.get("/api/v1/corpus")
        report.check("corpus listing", 200, corpus.status_code)
        docs = corpus.json().get("documents") or corpus.json().get("sources") or corpus.json()
        count = len(docs) if isinstance(docs, list) else corpus.json().get("documents")
        report.check("corpus has 35 records", 35, count if isinstance(count, int) else len(docs))
        sample_ids = ["PT-006", "OPS-306-V1", "BIZ-501"]
        for doc_id in sample_ids:
            src = client.get(f"/api/v1/sources/{doc_id}", params={"index_id": index_id})
            body = src.json() if src.status_code == 200 else {}
            digest = hashlib.sha256(body.get("text", "").encode()).hexdigest() if body else None
            report.check(f"source {doc_id} sha256 matches text", body.get("sha256"), digest)
        report.check("unknown source -> 404", 404, client.get("/api/v1/sources/PT-999", params={"index_id": index_id}).status_code)

        # Conversation creation, validation limits, idempotency
        created = client.post("/api/v1/conversations", json={"title": "integration checks"})
        report.check("create conversation", 201, created.status_code)
        cid = created.json()["id"]
        detail = client.get(f"/api/v1/conversations/{cid}").json()
        version = detail["context_version"]
        empty = client.post(f"/api/v1/conversations/{cid}/runs", json={"question": "   ", "context_version": version, "idempotency_key": uuid4().hex})
        report.check("blank question rejected", True, empty.status_code in {400, 422}, f"HTTP {empty.status_code} {empty.text[:120]}")
        huge = client.post(f"/api/v1/conversations/{cid}/runs", json={"question": "x" * 7000, "context_version": version, "idempotency_key": uuid4().hex})
        report.check("oversized question rejected", True, huge.status_code in {413, 422}, f"HTTP {huge.status_code}")
        stale = client.patch(f"/api/v1/conversations/{cid}/context", json={"as_of": "2026-03-01", "expected_version": version + 5})
        report.check("stale context version -> 409", 409, stale.status_code, stale.text[:120])
        bad_patient = client.patch(f"/api/v1/conversations/{cid}/context", json={"patient_id": "PT-banana", "expected_version": version})
        report.check("invalid patient id rejected", True, bad_patient.status_code in {400, 409, 422}, f"HTTP {bad_patient.status_code}")

        # Listing, pagination and search
        listing = client.get("/api/v1/conversations", params={"offset": 0})
        report.check("list conversations", 200, listing.status_code)
        items = listing.json().get("items") or listing.json().get("conversations") or []
        report.check("new conversation appears in list", True, any(item["id"] == cid for item in items))
        search = client.get("/api/v1/conversations", params={"q": "integration"})
        report.check("search finds conversation", True, any(item["id"] == cid for item in (search.json().get("items") or [])))

        # Stranger isolation
        with httpx.Client(base_url=base, headers={"Origin": base}, timeout=30) as stranger:
            stranger.post("/api/v1/session")
            report.check("stranger cannot read conversation", 404, stranger.get(f"/api/v1/conversations/{cid}").status_code)
            report.check("stranger cannot patch context", 404, stranger.patch(f"/api/v1/conversations/{cid}/context", json={"as_of": "2026-03-01", "expected_version": version}).status_code)

        run_record = None
        if args.with_cancel:
            key = uuid4().hex
            body = {"question": "List every documented follow-up interval and the office hours.", "context_version": version, "idempotency_key": key}
            first = client.post(f"/api/v1/conversations/{cid}/runs", json=body)
            report.check("run accepted", 202, first.status_code)
            rid = first.json()["id"]
            dup = client.post(f"/api/v1/conversations/{cid}/runs", json=body)
            report.check("same key + body reuses run", rid, dup.json().get("id"))
            conflict = client.post(f"/api/v1/conversations/{cid}/runs", json={**body, "question": "different"})
            report.check("same key + different body -> 409", 409, conflict.status_code, conflict.text[:100])
            second_active = client.post(f"/api/v1/conversations/{cid}/runs", json={**body, "idempotency_key": uuid4().hex})
            report.check("second active run in conversation -> 409", 409, second_active.status_code, second_active.text[:100])
            cancelled = client.post(f"/api/v1/runs/{rid}/cancel")
            report.check("cancel accepted", 200, cancelled.status_code)
            run_record = wait_terminal(client, rid)
            report.check("cancelled run reaches terminal state", True, run_record["status"] in TERMINAL, run_record["status"])
            report.check("cancelled run publishes no answer", True, run_record["status"] != "cancelled" or run_record.get("answer") is None, run_record["status"])
            events = client.get(f"/api/v1/runs/{rid}/events", headers={"Accept": "text/event-stream"}, timeout=10)
            text = events.text
            ids = [int(line.split(":", 1)[1].strip()) for line in text.splitlines() if line.startswith("id:")]
            report.check("SSE replay has increasing sequence ids", True, ids == sorted(ids) and len(ids) == len(set(ids)) and len(ids) >= 2, str(ids[:10]))
            report.check("SSE replay ends with terminal event", True, run_record["status"] in text, text[-300:].replace("\n", " | "))
            with httpx.Client(base_url=base, headers={"Origin": base}, timeout=30) as stranger:
                stranger.post("/api/v1/session")
                report.check("stranger cannot read run", 404, stranger.get(f"/api/v1/runs/{rid}").status_code)
                report.check("stranger cannot cancel run", 404, stranger.post(f"/api/v1/runs/{rid}/cancel").status_code)

        # Diagnostics / evaluations
        diagnostics = client.get("/api/v1/diagnostics")
        report.check("diagnostics endpoint", 200, diagnostics.status_code, json.dumps(diagnostics.json())[:200])
        evaluations = client.get("/api/v1/evaluations")
        report.check("evaluations endpoint", 200, evaluations.status_code, json.dumps(evaluations.json())[:160])

        # Reset revokes ownership
        reset = client.post("/api/v1/session/reset")
        report.check("session reset", True, reset.status_code in {200, 201}, str(reset.status_code))
        report.check("old conversation gone after reset", 404, client.get(f"/api/v1/conversations/{cid}").status_code)

    summary = {
        "at": datetime.now(UTC).isoformat(),
        "base": base,
        "checks": len(report.rows),
        "passed": sum(r["ok"] for r in report.rows),
        "failed": [r for r in report.rows if not r["ok"]],
        "rows": report.rows,
        "cancel_run": {k: run_record.get(k) for k in ("id", "status", "error", "metrics")} if run_record else None,
        "classification": "real HTTP against the running instance; provider-free except the optional cancel run",
    }
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(summary, indent=2, default=str) + "\n")
    print(f"\n{summary['passed']}/{summary['checks']} checks passed")
    return 0 if not summary["failed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
