"""Drive real questions through the HTTP answer pipeline and record what came back.

Usage: uv run python scripts/probe_questions.py --base http://localhost:8047 --suite evals/probes/knowledge-integration.json --out artifacts/verification/<run>

Makes bounded live provider calls (one run per case, sequential). Writes per-run JSON, a summary
and a Markdown table. Expectations are author annotations for review, not a quality certification.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx

TERMINAL = {"completed", "failed", "cancelled", "superseded", "interrupted"}


def wait_for_run(client: httpx.Client, run_id: str, timeout: float) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        run = client.get(f"/api/v1/runs/{run_id}")
        run.raise_for_status()
        body = run.json()
        if body["status"] in TERMINAL:
            return body
        time.sleep(0.5)
    raise TimeoutError(run_id)


def cited_ids(answer: dict) -> list[str]:
    ids = []
    for item in answer.get("statements", []) + answer.get("next_steps", []):
        ids.extend(citation["doc_id"] for citation in item.get("citations", []))
    return sorted(set(ids))


def prose(answer: dict) -> str:
    return "\n".join(
        item["text"]
        for item in answer.get("statements", []) + answer.get("gaps", []) + answer.get("next_steps", [])
    )


_source_cache: dict[tuple[str, str], dict] = {}


def verify_citations(client: httpx.Client, answer: dict) -> tuple[int, list[str]]:
    """Every published quote must be the exact original span of the pinned source revision."""
    verified, problems = 0, []
    for item in answer.get("statements", []) + answer.get("next_steps", []):
        for citation in item.get("citations", []):
            key = (citation["doc_id"], citation["index_id"])
            if key not in _source_cache:
                response = client.get(f"/api/v1/sources/{citation['doc_id']}", params={"index_id": citation["index_id"]})
                if response.status_code != 200:
                    problems.append(f"source {citation['doc_id']} HTTP {response.status_code}")
                    continue
                _source_cache[key] = response.json()
            source = _source_cache[key]
            span = source["text"][citation["start"] : citation["end"]]
            if span != citation["quote"] or citation["quote"] not in source["text"]:
                problems.append(f"citation span mismatch {citation['doc_id']}@{citation['start']}-{citation['end']}")
            elif source.get("sha256") and source["sha256"] != citation.get("source_sha256"):
                problems.append(f"citation source hash mismatch {citation['doc_id']}")
            else:
                verified += 1
    return verified, problems


def check(case: dict, run: dict) -> list[str]:
    problems = []
    answer = run.get("answer") or {}
    if run["status"] != "completed":
        return [f"run {run['status']}: {json.dumps(run.get('error'))}"]
    if case.get("expect_status") and answer.get("status") not in case["expect_status"]:
        problems.append(f"status {answer.get('status')} not in {case['expect_status']}")
    scopes = answer.get("scopes") or []
    if "expect_parts" in case and len(scopes) != case["expect_parts"]:
        problems.append(f"{len(scopes)} scope parts, expected {case['expect_parts']}")
    for key, value in (case.get("expect_scope") or {}).items():
        observed = [scope.get(key) for scope in scopes]
        if key == "patient_ids":
            observed = [sorted(v or []) for v in observed]
            value = sorted(value)
        if value not in observed:
            problems.append(f"scope.{key} {observed} lacks {value}")
    cited = set(cited_ids(answer))
    for doc_id in case.get("must_cite", []):
        # "A|B" means any one of the alternatives must be cited.
        if not any(option in cited for option in doc_id.split("|")):
            problems.append(f"missing citation {doc_id}")
    for doc_id in case.get("must_not_cite", []):
        if doc_id in cited:
            problems.append(f"forbidden citation {doc_id}")
    text = prose(answer)
    statements = "\n".join(item["text"] for item in answer.get("statements", []))
    for pattern in case.get("must_mention", []):
        if not re.search(pattern, text, re.IGNORECASE):
            problems.append(f"missing mention /{pattern}/")
    for pattern in case.get("must_not_state", []):
        if re.search(pattern, statements, re.IGNORECASE):
            problems.append(f"statement contains /{pattern}/")
    return problems


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", required=True)
    parser.add_argument("--suite", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--only", nargs="*", default=None, help="case ids to run")
    parser.add_argument("--timeout", type=float, default=150)
    args = parser.parse_args()

    suite = json.loads(Path(args.suite).read_text())
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    results = []
    total_cost = 0.0
    with httpx.Client(base_url=args.base, headers={"Origin": args.base}, timeout=args.timeout + 10) as client:
        client.post("/api/v1/session").raise_for_status()
        ready = client.get("/api/v1/health/ready").json()
        conversations: dict[str, str] = {}
        for case in suite["cases"]:
            if args.only and case["id"] not in args.only:
                continue
            key = case.get("conversation", case["id"])
            if key not in conversations:
                created = client.post("/api/v1/conversations", json={"title": f"probe {key}"})
                created.raise_for_status()
                conversations[key] = created.json()["id"]
            cid = conversations[key]
            conversation = client.get(f"/api/v1/conversations/{cid}").json()
            version = conversation["context_version"]
            if case.get("context"):
                patched = client.patch(
                    f"/api/v1/conversations/{cid}/context",
                    json={**case["context"], "expected_version": version},
                )
                patched.raise_for_status()
                version = patched.json()["context_version"]
            started = time.perf_counter()
            body = {"question": case["question"], "context_version": version, "idempotency_key": uuid4().hex}
            for attempt in range(6):
                submitted = client.post(f"/api/v1/conversations/{cid}/runs", json=body)
                if submitted.status_code != 429:
                    break
                # The app's per-session limit (8 runs/minute) is expected; wait it out rather than fail the suite.
                time.sleep(float(submitted.headers.get("Retry-After", 12)))
            submitted.raise_for_status()
            run = wait_for_run(client, submitted.json()["id"], args.timeout)
            elapsed = round((time.perf_counter() - started) * 1000)
            usage = (run.get("metrics") or {}).get("usage") or []
            cost = sum(u.get("cost") or 0 for u in usage)
            total_cost += cost
            problems = check(case, run)
            answer = run.get("answer") or {}
            verified_citations, citation_problems = verify_citations(client, answer)
            problems.extend(citation_problems)
            record = {
                "id": case["id"],
                "question": case["question"],
                "context": case.get("context"),
                "resolved_question": (run.get("context") or {}).get("resolved_question"),
                "run_id": run["id"],
                "run_status": run["status"],
                "answer_status": answer.get("status"),
                "scopes": [
                    {
                        k: scope.get(k)
                        for k in ("part_id", "patient_ids", "unknown_patient_ids", "all_patients", "patient_discovery", "as_of")
                    }
                    for scope in answer.get("scopes", [])
                ],
                "statements": [item["text"] for item in answer.get("statements", [])],
                "gaps": [item["text"] for item in answer.get("gaps", [])],
                "next_steps": [item["text"] for item in answer.get("next_steps", [])],
                "cited": cited_ids(answer),
                "citations_verified": verified_citations,
                "coverage": [(c.get("part_id"), c.get("status")) for c in (answer.get("diagnostics") or {}).get("answer_coverage", [])],
                "support_check": (answer.get("diagnostics") or {}).get("support_check"),
                "repair_count": (answer.get("diagnostics") or {}).get("repair_count"),
                "provider_calls": len(usage),
                "cost_usd": round(cost, 6),
                "latency_ms": elapsed,
                "error": run.get("error"),
                "expectations": {k: v for k, v in case.items() if k.startswith(("expect", "must"))},
                "problems": problems,
            }
            results.append(record)
            (out / f"{case['id']}.json").write_text(json.dumps(run, indent=2, ensure_ascii=False) + "\n")
            flag = "OK " if not problems else "!! "
            print(f"{flag}{case['id']:<14} {answer.get('status') or run['status']:<13} {elapsed:>6}ms ${cost:.4f}  {'; '.join(problems)}")
            sys.stdout.flush()
    summary = {
        "at": datetime.now(UTC).isoformat(),
        "base": args.base,
        "index_id": ready.get("index_id"),
        "suite": args.suite,
        "cases": len(results),
        "clean": sum(not r["problems"] for r in results),
        "with_problems": sum(bool(r["problems"]) for r in results),
        "total_cost_usd": round(total_cost, 6),
        "citations_verified": sum(r["citations_verified"] for r in results),
        "latency_ms_p50": sorted(r["latency_ms"] for r in results)[len(results) // 2] if results else None,
        "latency_ms_max": max((r["latency_ms"] for r in results), default=None),
        "classification": "real HTTP + live provider; author annotations, not independent review",
        "results": results,
    }
    (out / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n")
    lines = [
        f"# Probe run {summary['at']}",
        "",
        f"Base `{args.base}` · index `{summary['index_id']}` · {summary['clean']}/{summary['cases']} clean · ${summary['total_cost_usd']} · p50 {summary['latency_ms_p50']} ms · max {summary['latency_ms_max']} ms",
        "",
        "| Case | Question | Status | Scope | Cited | ms | Problems |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in results:
        scope = "; ".join(
            ("ALL" if s["all_patients"] else "DISC" if s["patient_discovery"] else ",".join(s["patient_ids"]) or ",".join(s["unknown_patient_ids"]) or "-") + f"@{s['as_of']}"
            for s in r["scopes"]
        )
        lines.append(
            f"| {r['id']} | {r['question'][:70].replace('|', '/')} | {r['answer_status'] or r['run_status']} | {scope} | {', '.join(r['cited'])} | {r['latency_ms']} | {'; '.join(r['problems']).replace('|', '/')} |"
        )
    (out / "SUMMARY.md").write_text("\n".join(lines) + "\n")
    print(json.dumps({k: v for k, v in summary.items() if k != "results"}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
