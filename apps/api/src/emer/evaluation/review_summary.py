"""Offline integration of blinded author-subagent reviews; no model calls or automatic fact judge."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any


class ReviewCoverageError(ValueError):
    pass


def digest(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def counted(value: Any) -> int:
    return len(value) if isinstance(value, list) else int(value)


def normalize(row: dict[str, Any]) -> dict[str, Any]:
    review = row.get("review", row)
    facts = review.get("required_fact_assessments", review.get("required_fact_coverage"))
    if not isinstance(facts, list):
        raise ReviewCoverageError("Review has no per-fact assessments")
    booleans = [fact.get("covered", fact.get("present")) for fact in facts]
    if any(type(value) is not bool for value in booleans):
        raise ReviewCoverageError("Fact coverage must be explicit booleans")
    present, total = sum(booleans), len(facts)
    if (
        review.get("required_facts_present", present) != present
        or review.get("required_facts_total", total) != total
    ):
        raise ReviewCoverageError("Review fact totals disagree with its per-fact judgments")
    expected = review.get("expected_answerability", review.get("answerability_expected"))
    inferred = review.get("inferred_answerability", review.get("answerability_inferred"))
    correct = review.get("answerability_correct", review.get("answerability_matches_expected"))
    if type(correct) is not bool or correct != (expected == inferred):
        raise ReviewCoverageError("Review answerability fields are inconsistent")
    claims = review.get("unsupported_claims", [])
    critical_claims = (
        counted(review["unsupported_critical_claims"])
        if "unsupported_critical_claims" in review
        else sum(claim.get("critical", False) or claim.get("severity") == "critical" for claim in claims)
    )
    material_claims = (
        counted(review["material_unsupported_claims"])
        if "material_unsupported_claims" in review
        else 0
        if not claims
        else None
    )
    return {
        "blind_id": row["blind_id"],
        "case_id": row["case_id"],
        "required_facts_present": present,
        "required_facts_total": total,
        "fact_assessments": facts,
        "expected_answerability": expected,
        "inferred_answerability": inferred,
        "answerability_correct": correct,
        "unsupported_claim_count": len(claims),
        "critical_fabrication": bool(review.get("critical_fabrication")),
        "critical_claim_count": critical_claims,
        "material_claim_count": material_claims,
        "citation_support_issue_count": len(review.get("citation_support_issues", [])),
        "review": review,
    }


def validate_coverage(items: dict[str, Any], reviews: list[dict[str, Any]]) -> None:
    counts = Counter(review["blind_id"] for review in reviews)
    missing, unexpected = sorted(set(items) - set(counts)), sorted(set(counts) - set(items))
    duplicate = sorted(key for key, count in counts.items() if count != 1)
    if missing or unexpected or duplicate:
        raise ReviewCoverageError(
            f"Review coverage incomplete: missing={missing}, unexpected={unexpected}, duplicate={duplicate}"
        )


def integrate(directory: Path) -> dict[str, Any]:
    package = json.loads((directory / "blinded-review.json").read_text())
    items = {item["blind_id"]: item for item in package["items"]}
    mapping = json.loads((directory / "blinding-map.json").read_text())
    if set(items) != set(mapping) or len(items) != 119:
        raise ReviewCoverageError("Expected the original 119 completed blinded drafts")
    reviews = []
    review_files = []
    for batch in ("a", "b", "c"):
        path = directory / f"review-batch-{batch}-results.json"
        if not path.exists():
            raise ReviewCoverageError(f"Batch {batch} is not available")
        data = json.loads(path.read_text())
        if data.get("reviewer_type") != "author_subagent_review":
            raise ReviewCoverageError("Unexpected reviewer classification")
        batch_input = directory / f"review-batch-{batch}.json"
        if data.get("input_sha256") and data["input_sha256"] != digest(batch_input):
            raise ReviewCoverageError("Reviewed batch input changed")
        review_files.append(
            {
                "batch": batch,
                "sha256": digest(path),
                "input_sha256": digest(batch_input),
                "method": data.get("method", data.get("methodology")),
                "annotation_caveats": data.get("annotation_caveats", []),
            }
        )
        reviews.extend(normalize(row) for row in data.get("reviews", data.get("items", [])))
    validate_coverage(items, reviews)
    rows = [json.loads(line) for line in (directory / "attempts.jsonl").read_text().splitlines()]
    terminal_rows = [row for row in rows if row["status"] != "dispatched"]
    terminal = {row["attempt_id"]: row for row in terminal_rows}
    dispatched = [row["attempt_id"] for row in rows if row["status"] == "dispatched"]
    if (
        len(terminal) != 120
        or len(terminal_rows) != 120
        or len(dispatched) != 120
        or set(dispatched) != set(terminal)
    ):
        raise ReviewCoverageError("Expected the original 120 dispatched and terminal provider attempts")
    run = json.loads((directory / "run.json").read_text())
    suite_path = Path("evals/development.json")
    if digest(suite_path) != run["config"]["suite_sha256"]:
        raise ReviewCoverageError("Original evaluation suite changed after generation")
    suite = json.loads(suite_path.read_text())
    cases = {case["id"]: case for case in suite["cases"]}
    sources = {source["doc_id"]: source for source in package["sources"]}
    for review in reviews:
        original = items[review["blind_id"]]
        link = mapping[review["blind_id"]]
        if review["case_id"] != original["case_id"] or link["case_id"] != original["case_id"]:
            raise ReviewCoverageError("Blind review case identity mismatch")
        if [fact["fact"] for fact in review["fact_assessments"]] != original["required_facts"]:
            raise ReviewCoverageError("Per-fact review does not cover the complete original rubric")
        if review["expected_answerability"] != original["expected_answerability"]:
            raise ReviewCoverageError("Expected answerability changed during review")
        attempt = terminal[link["attempt_id"]]
        if (
            attempt["status"] != "completed"
            or attempt["job"]["model"] != link["model"]
            or attempt["job"]["case_id"] != original["case_id"]
            or attempt["draft"] != original["draft"]
        ):
            raise ReviewCoverageError("Blinding map or draft identity mismatch")
        scopes = {scope["part_id"]: scope for scope in original["scopes"]}
        quotes = []
        for statement in original["draft"]["statements"] + original["draft"]["next_steps"]:
            for citation in statement["citations"]:
                source = sources.get(citation["doc_id"])
                quotes.append(
                    {
                        "exact": bool(source and citation["quote"] in source["text"]),
                        "in_scope": citation["doc_id"]
                        in scopes.get(statement["part_id"], {}).get("allowed_source_ids", []),
                    }
                )
        review.update(
            {
                "model": link["model"],
                "attempt_id": link["attempt_id"],
                "citation_contract_passed": attempt["citation_validation"]["status"] == "passed",
                "citation_references": len(quotes),
                "exact_quote_matches": sum(q["exact"] for q in quotes),
                "out_of_scope_references": sum(not q["in_scope"] for q in quotes),
            }
        )
    models = []
    for model in sorted({row["job"]["model"] for row in terminal.values()}):
        group = [review for review in reviews if review["model"] == model]
        requested = [row for row in terminal.values() if row["job"]["model"] == model]
        models.append(
            {
                "model": model,
                "requested_drafts": len(requested),
                "reviewed_drafts": len(group),
                "provider_or_schema_failures": sum(row["status"] == "failed" for row in requested),
                "required_facts": {
                    "present": sum(r["required_facts_present"] for r in group),
                    "reviewed_total": sum(r["required_facts_total"] for r in group),
                    "requested_total": sum(
                        len(cases[row["job"]["case_id"]]["required_facts"]) for row in requested
                    ),
                },
                "answerability": {
                    "matches_original_annotation": sum(r["answerability_correct"] for r in group),
                    "reviewed_total": len(group),
                    "disagreements": [r["blind_id"] for r in group if not r["answerability_correct"]],
                },
                "unsupported": {
                    "drafts_with_critical_claims": sum(r["critical_fabrication"] for r in group),
                    "critical_claim_count": sum(r["critical_claim_count"] for r in group),
                    "critical_drafts_passing_citation_contract": sum(
                        r["critical_fabrication"] and r["citation_contract_passed"] for r in group
                    ),
                    "critical_draft_ids": [r["blind_id"] for r in group if r["critical_fabrication"]],
                    "all_flagged_claim_count": sum(r["unsupported_claim_count"] for r in group),
                    "drafts_with_flagged_claims": sum(r["unsupported_claim_count"] > 0 for r in group),
                    "explicitly_material_claim_count": sum(
                        r["material_claim_count"] for r in group if r["material_claim_count"] is not None
                    ),
                    "drafts_with_unclassified_materiality": sum(
                        r["material_claim_count"] is None for r in group
                    ),
                },
                "citations": {
                    "references": sum(r["citation_references"] for r in group),
                    "exact_quote_matches": sum(r["exact_quote_matches"] for r in group),
                    "out_of_scope_references": sum(r["out_of_scope_references"] for r in group),
                    "drafts_with_semantic_support_issues": sum(
                        r["citation_support_issue_count"] > 0 for r in group
                    ),
                    "reviewer_support_issue_count": sum(r["citation_support_issue_count"] for r in group),
                    "citation_contract_passed_drafts": sum(r["citation_contract_passed"] for r in group),
                },
            }
        )
    return {
        "assessment_at": datetime.now(UTC).isoformat(),
        "reviewer_type": "author_subagent_review",
        "independent_human_review": False,
        "stage": "raw_development_drafts_before_repair_or_checker",
        "coverage": {
            "requested_drafts": 120,
            "reviewed_drafts": 119,
            "review_batches": 3,
            "missing_review_ids": [],
            "duplicate_review_ids": [],
            "provider_or_schema_failures": 1,
        },
        "models": models,
        "identities": {
            "generation_config_sha256": run["config_sha256"],
            "suite_sha256": run["config"]["suite_sha256"],
            "corpus_sha256": run["config"]["corpus_sha256"],
            "index_id": run["config"]["index_id"],
            "index_checksum": run["config"]["index_checksum"],
            "generation_source_file_sha256": run["config"].get("source_file_sha256"),
            "generation_prompt_sha256": run["config"]["prompt_sha256"],
            "checker_prompt_sha256": run["config"]["checker_prompt_sha256"],
            "blinded_package_sha256": digest(directory / "blinded-review.json"),
            "blinding_map_sha256": digest(directory / "blinding-map.json"),
            "review_files": review_files,
            "current_scope_code_sha256": digest(Path(__file__).parents[1] / "domain/scope.py"),
        },
        "limitations": [
            "These are raw development drafts, including drafts rejected by deterministic validation; they are not published final answers.",
            "Three blinded author-subagents reviewed semantics. This is not independent-human verification or an automated checker score.",
            "Reviewer critical/material severity classifications are preserved without silently adjudicating differences. Some noncritical claims lack a materiality classification.",
            "Required-fact coverage counts explicit answer prose, including gaps/next steps, rather than facts appearing only in quoted citations. Bundled rubric facts require all substantive components.",
            "Answerability compares each reviewer judgment with the original annotations; ambiguities about meta-questions and incidental gaps are retained. A schema/provider failure has no factual review and is separately counted.",
            "Exact quote membership is a deterministic substring property, not evidence of semantic support. Reviewer issue counts may group multiple citations and are not a citation-level correctness percentage.",
            "The modal-may scope bug affected original D02 evidence. Original outputs are retained; the offline fix has not been re-evaluated with paid providers.",
            "The original generation run captured corpus, index, suite, prompt and provider configuration identities, but no per-code-file hashes. Current code hashes must not be retroactively attributed to those requests.",
        ],
        "reviews": reviews,
    }


def public_summary(result: dict[str, Any]) -> dict[str, Any]:
    selection_path = Path("config/model-selection.json")
    selection = json.loads(selection_path.read_text()) if selection_path.exists() else None
    return {
        "status": "development_review_complete_final_pending",
        "assessment_at": result["assessment_at"],
        "reviewer_type": result["reviewer_type"],
        "independent_human_review": False,
        "stage": result["stage"],
        "current_selection": selection,
        "coverage": result["coverage"],
        "models": result["models"],
        "identities": {key: value for key, value in result["identities"].items() if key != "review_files"},
        "review_batches": [
            {"batch": row["batch"], "sha256": row["sha256"]} for row in result["identities"]["review_files"]
        ],
        "limitations": result["limitations"],
        "flagged_review_notes": [
            {
                "blind_id": row["blind_id"],
                "case_id": row["case_id"],
                "model": row["model"],
                "reviewer_classified_critical": row["critical_fabrication"],
                "citation_contract_passed": row["citation_contract_passed"],
                "expected_answerability": row["expected_answerability"],
                "reviewer_answerability": row["inferred_answerability"],
                "note": row["review"].get("notes", ""),
            }
            for row in result["reviews"]
            if row["critical_fabrication"] or not row["answerability_correct"]
        ],
        "pending_gates": [
            "Source-review every outcome of the selected Luna pipeline across 20 frozen holdout scenarios, three trials each (60 trials).",
            "Meet unchanged factual and answerability thresholds; any holdout-derived tuning needs fresh affected holdout scenarios.",
            "Verify deployed typed flows and natural two-way Live with actual physical microphone/listening checks.",
            "Final-pipeline factual, deployed, and spoken acceptance have not been established.",
        ],
    }


def markdown(result: dict[str, Any]) -> str:
    lines = [
        "# Raw development draft review",
        "",
        f"Assessed {result['assessment_at']}. All 119 completed drafts have exactly one blinded author-subagent review; one additional provider/schema attempt failed. This is not independent-human review or final-pipeline acceptance.",
        "",
        "| Model | Reviewed / requested | Required facts in prose | Answerability matches | Critical drafts / claims | Exact quotes | Drafts with semantic citation issues |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for row in result["models"]:
        facts, ability, unsupported, citations = (
            row["required_facts"],
            row["answerability"],
            row["unsupported"],
            row["citations"],
        )
        lines.append(
            f"| {row['model']} | {row['reviewed_drafts']} / {row['requested_drafts']} | {facts['present']} / {facts['reviewed_total']} reviewed; {facts['requested_total']} requested | {ability['matches_original_annotation']} / {ability['reviewed_total']} | {unsupported['drafts_with_critical_claims']} / {unsupported['critical_claim_count']} | {citations['exact_quote_matches']} / {citations['references']} | {citations['drafts_with_semantic_support_issues']} / {row['reviewed_drafts']} |"
        )
    lines.extend(
        [
            "",
            "Counts preserve the reviewers' classifications. Critical claims in raw drafts are not necessarily published claims: the original pipeline's deterministic rejections are retained, and complete repair/checker behavior has not been assessed by this review.",
            "",
            "## Limitations and disagreements",
            "",
        ]
    )
    lines.extend("- " + value for value in result["limitations"])
    lines.extend(["", "## Flagged cases", ""])
    for review in result["reviews"]:
        if review["critical_fabrication"] or not review["answerability_correct"]:
            lines.append(
                f"- {review['blind_id']} / {review['case_id']} / {review['model']}: critical={review['critical_fabrication']}; answerability={review['inferred_answerability']} vs annotation={review['expected_answerability']}. {review['review'].get('notes', '')}"
            )
    lines.extend(
        [
            "",
            "The detailed JSON retains every per-fact judgment, unsupported-claim explanation, citation issue and reviewer note. Original review files and generation records remain unchanged. Current scope code fixes modal 'may'; the historical D02 evidence remains marked stale. Paid reevaluation is blocked by exhausted account credit.",
            "",
        ]
    )
    return "\n".join(lines)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", default="artifacts/verification/generation-comparison-20260911")
    parser.add_argument("--public-output", default="config/evaluation-summary.json")
    args = parser.parse_args()
    folder = Path(args.directory)
    result = integrate(folder)
    (folder / "author-review-summary.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    )
    (folder / "AUTHOR_REVIEW.md").write_text(markdown(result))
    public = Path(args.public_output)
    public.parent.mkdir(parents=True, exist_ok=True)
    public.write_text(json.dumps(public_summary(result), ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"coverage": result["coverage"], "models": result["models"]}, indent=2))
