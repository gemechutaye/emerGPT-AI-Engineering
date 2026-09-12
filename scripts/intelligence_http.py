"""Bounded real HTTP intelligence checks with durable dispatch records; no silent resumption."""

from __future__ import annotations

import argparse
import json
import time
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

import httpx


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", required=True)
    parser.add_argument("--suite", required=True)
    parser.add_argument("--ids", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    out = Path(args.out)
    if out.exists():
        raise ValueError("Use a new output directory; inspect prior dispatched runs before retrying")
    out.mkdir(parents=True)
    raw = Path(args.suite).read_bytes()
    chosen = set(args.ids.split(","))
    cases = [c for c in json.loads(raw)["cases"] if c["id"] in chosen]
    if len(cases) != len(chosen):
        raise ValueError("Unknown case IDs")
    results, failures = [], 0
    with httpx.Client(base_url=args.base, headers={"Origin": args.base}, timeout=30) as client:
        client.post("/api/v1/session").raise_for_status()
        ready = client.get("/api/v1/health/ready")
        ready.raise_for_status()
        save(
            out / "configuration.json",
            {
                "at": datetime.now(UTC).isoformat(),
                "base": args.base,
                "suite_sha256": sha256(raw).hexdigest(),
                "ids": args.ids,
                "ready": ready.json(),
                "path": "Actual HTTP submission, real provider calls, history and source lookups",
                "latency_method": "HTTP submit to observed terminal response, including embedding and polling",
            },
        )
        for case in cases:
            created = client.post(
                "/api/v1/conversations", json={"title": "Intelligence validation " + case["id"]}
            )
            created.raise_for_status()
            conversation_id = created.json()["id"]
            for number, turn in enumerate(case.get("turns") or [case], 1):
                key = f"{case['id']}.T{number}"
                folder = out / key
                folder.mkdir()
                conversation = client.get(f"/api/v1/conversations/{conversation_id}").json()
                body = {
                    "question": turn["question"],
                    "context_version": conversation["context_version"],
                    "automatic_context": True,
                    "idempotency_key": uuid4().hex,
                }
                save(
                    folder / "dispatch.json",
                    {
                        "at": datetime.now(UTC).isoformat(),
                        "conversation_id": conversation_id,
                        "request": body,
                        "status": "dispatched",
                    },
                )
                started = time.perf_counter()
                for _ in range(6):
                    response = client.post(f"/api/v1/conversations/{conversation_id}/runs", json=body)
                    if response.status_code != 429:
                        break
                    time.sleep(min(float(response.headers.get("Retry-After", "12")), 30))
                response.raise_for_status()
                run_id = response.json()["id"]
                save(folder / "admitted.json", {"run_id": run_id, "response": response.json()})
                deadline = time.monotonic() + 150
                while time.monotonic() < deadline:
                    response = client.get(f"/api/v1/runs/{run_id}")
                    response.raise_for_status()
                    run = response.json()
                    if run["status"] not in {"queued", "running"}:
                        break
                    time.sleep(0.5)
                else:
                    raise TimeoutError(f"Inspect saved run {run_id}; no automatic retry")
                elapsed = round((time.perf_counter() - started) * 1000, 3)
                save(folder / "run.json", run)
                answer = run.get("answer") or {}
                checked, problems = 0, []
                for item in answer.get("statements", []) + answer.get("next_steps", []):
                    for citation in item["citations"]:
                        response = client.get(
                            f"/api/v1/sources/{citation['doc_id']}", params={"index_id": citation["index_id"]}
                        )
                        response.raise_for_status()
                        source = response.json()
                        save(folder / (citation["doc_id"] + "-source.json"), source)
                        exact = (
                            source["text"][citation["start"] : citation["end"]] == citation["quote"]
                            and source["sha256"] == citation["source_sha256"]
                            and all(
                                source[k] == citation[k]
                                for k in ["index_id", "title", "version", "effective_date", "authority"]
                            )
                        )
                        checked += 1
                        if not exact:
                            problems.append(citation)
                history = client.get(f"/api/v1/conversations/{conversation_id}").json()
                save(folder / "history.json", history)
                matching = next(r for r in history["runs"] if r["id"] == run_id)
                if matching["answer"] != run.get("answer"):
                    raise AssertionError("Saved history differs from published answer")
                result = {
                    "id": key,
                    "run_id": run_id,
                    "conversation_id": conversation_id,
                    "run_status": run["status"],
                    "answer_status": answer.get("status"),
                    "citations_checked": checked,
                    "citation_errors": problems,
                    "http_latency_ms": elapsed,
                    "usage": (run.get("metrics") or {}).get("usage", []),
                    "provider_attempts": run.get("provider_attempts", []),
                    "semantic_review": "pending",
                }
                save(folder / "checks.json", result)
                results.append(result)
                save(out / "results.json", results)
                print(
                    json.dumps(
                        {
                            k: result[k]
                            for k in [
                                "id",
                                "run_status",
                                "answer_status",
                                "citations_checked",
                                "http_latency_ms",
                            ]
                        }
                    ),
                    flush=True,
                )
                if run["status"] != "completed":
                    failures += 1
                    break
            if failures >= 3:
                break
    save(
        out / "summary.json",
        {
            "cases": len(results),
            "completed": sum(r["run_status"] == "completed" for r in results),
            "citations_checked": sum(r["citations_checked"] for r in results),
            "citation_errors": sum(len(r["citation_errors"]) for r in results),
            "reported_run_cost": sum(
                u["cost"] for r in results for u in r["usage"] if u.get("cost") is not None
            ),
            "unknown_costs": sum(u.get("cost") is None for r in results for u in r["usage"]),
            "metadata_cost": "Separate conversation title/recap receipts audited from Postgres; not included above",
            "semantic_review": "pending independent review; exact quote checks alone are not semantic correctness",
        },
    )


if __name__ == "__main__":
    main()
