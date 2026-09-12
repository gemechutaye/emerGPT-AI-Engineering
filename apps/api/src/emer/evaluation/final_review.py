"""Package frozen published holdout outcomes for source review without model/checker identity."""

from __future__ import annotations

import argparse
import json
import random
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

from emer.evaluation.__main__ import load_suite
from emer.services.ingestion import canonical

INSTRUCTIONS = """Review only this package and its embedded source texts; do not inspect sibling ledgers, maps, prompts, or model identity. You are a blinded author-subagent reviewer, not an independent human. Review every actual published statement, gap, next step, and citation, not the checker verdict (which is hidden). A failed operational outcome has no published answer and must remain a failed trial, with zero covered facts and incorrect answerability; it is not a fabricated claim.

For each required fact, mark covered only when substantively present in answer prose (including gaps/next steps); appearance only in a citation does not count. Preserve source qualifications and distinguish unknown from confirmed absent. Bundled rubric facts require all substantive components. Audit each factual clause and quoted passage, full source, and per-part scope. Check numbers, source-event anchors, historical applicability, negation, fictional price qualification, exclusive/all/none claims, and whether an action/status transition or clinical permission was inferred. Quoted text being exact does not establish semantic support. Record every unsupported material or critical claim, and note annotation ambiguities without silently changing the rubric. Do not use keyword matching as the factual judge.

Write one result per blind_id using the supplied output schema. Expected answerability is fixed; inferred_answerability should match the actually published response status and explain any semantic disagreement with the fixture. Every source-support issue needs the item kind and index and a concrete reason. Report severity as critical for patient/price/deadline/policy/action fabrication, material for other meaning-changing unsupported content, and minor for nonmaterial wording. Do not treat a correctly rejected unpublished draft as a published claim. Preserve failures and disagreements. Review repeated trials separately."""


def package_batch(directory: Path, batch: str) -> Path:
    run = json.loads((directory / "run.json").read_text())
    suite, suite_sha = load_suite(run["config"]["suite"])
    if run["config"]["suite_sha256"] != suite_sha or run["config"]["trials"] != 3:
        raise ValueError("Final run does not match the frozen holdout and three-trial declaration")
    cases = {case["id"]: case for case in suite["cases"]}
    expected = [(case["id"], trial) for case in suite["cases"] for trial in range(1, 4)]
    offset = "abc".index(batch) * 20
    selected = expected[offset : offset + 20]
    records = [json.loads(line) for line in (directory / "attempts.jsonl").read_text().splitlines()]
    terminal = {
        (row["job"]["case_id"], row["job"]["trial"]): row
        for row in records
        if row["status"] in {"completed", "failed"}
    }
    if any(key not in terminal for key in selected):
        raise ValueError("This review batch still has unfinished or unissued trials")
    rng = random.Random(sha256(canonical({"config": run["config_sha256"], "batch": batch})).hexdigest())
    rng.shuffle(selected)
    items, mapping, sources = [], {}, {}
    for index, key in enumerate(selected, offset + 1):
        row = terminal[key]
        case = cases[key[0]]
        blind_id = f"F{index:03}"
        mapping[blind_id] = {"attempt_id": row["attempt_id"], "case_id": key[0], "trial": key[1]}
        answer = row.get("answer")
        if answer:
            for source in row["evidence"]["sources"]:
                if source["doc_id"] in sources and sources[source["doc_id"]] != source:
                    raise ValueError("A source changed within the declared final configuration")
                sources[source["doc_id"]] = source
        items.append(
            {
                "blind_id": blind_id,
                "case_id": case["id"],
                "family": case["family"],
                "question": case["question"],
                "required_facts": case["required_facts"],
                "required_sources": case["required_sources"],
                "forbidden_claims": case["forbidden_claims"],
                "expected_answerability": case["expected_answerability"],
                "scopes": row.get("evidence", {}).get("scopes", []),
                "answer": {key: answer[key] for key in ("status", "statements", "gaps", "next_steps")}
                if answer
                else None,
                "operational_failure": row.get("error"),
            }
        )
    result = {
        "created_at": datetime.now(UTC).isoformat(),
        "reviewer_type": "author_subagent_review",
        "stage": "published_final_holdout_outcomes",
        "batch": batch,
        "suite_sha256": suite_sha,
        "instructions": INSTRUCTIONS,
        "output_schema": {
            "reviewer_type": "author_subagent_review",
            "input_sha256": "SHA256 of this exact input file",
            "reviews": [
                {
                    "blind_id": "F...",
                    "case_id": "H...",
                    "required_fact_assessments": [
                        {
                            "fact": "exact original rubric text",
                            "covered": True,
                            "reason": "source and answer evidence",
                        }
                    ],
                    "expected_answerability": "copy original",
                    "inferred_answerability": "answered / partial / unsupported / operational_failure",
                    "answerability_matches_expected": True,
                    "critical_fabrication": False,
                    "unsupported_claims": [
                        {
                            "kind": "statement/gap/next_step",
                            "index": 0,
                            "text": "claim",
                            "severity": "critical/material/minor",
                            "source_ids": [],
                            "reason": "concrete source comparison",
                        }
                    ],
                    "citation_support_issues": [
                        {
                            "kind": "statement/next_step",
                            "index": 0,
                            "citation_index": 0,
                            "reason": "exact reason",
                        }
                    ],
                    "notes": "annotation disagreements and relevant limitations",
                }
            ],
        },
        "items": items,
        "sources": sorted(sources.values(), key=lambda source: source["doc_id"]),
    }
    path = directory / f"final-review-batch-{batch}.json"
    map_path = directory / f"final-review-map-{batch}.json"
    if path.exists() or map_path.exists():
        raise ValueError("Review package already exists; never replace a reviewed input")
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    map_path.write_text(json.dumps(mapping, indent=2) + "\n")
    return path


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", required=True)
    parser.add_argument("--batch", choices=list("abc"), required=True)
    args = parser.parse_args()
    path = package_batch(Path(args.directory), args.batch)
    print(json.dumps({"path": str(path), "sha256": sha256(path.read_bytes()).hexdigest()}))
