"""Offline assessment integrity; these tests do not judge answer truth."""

import pytest
from emer.evaluation.review_summary import ReviewCoverageError, normalize, validate_coverage


def row():
    return {
        "blind_id": "A001",
        "case_id": "D01",
        "required_fact_assessments": [{"fact": "A qualified source fact", "covered": True}],
        "expected_answerability": "answered",
        "inferred_answerability": "answered",
        "answerability_matches_expected": True,
        "unsupported_claims": [{"text": "An overstatement", "severity": "noncritical"}],
        "critical_fabrication": False,
        "citation_support_issues": [],
    }


def test_unclassified_materiality_is_not_silently_counted_as_zero():
    result = normalize(row())
    assert result["material_claim_count"] is None
    assert result["unsupported_claim_count"] == 1


@pytest.mark.parametrize("field,value", [("required_facts_present", 0), ("required_facts_total", 2)])
def test_summary_rejects_totals_inconsistent_with_fact_judgments(field, value):
    review = row()
    review[field] = value
    with pytest.raises(ReviewCoverageError, match="totals disagree"):
        normalize(review)


def test_summary_rejects_inconsistent_answerability_match():
    review = row()
    review["answerability_matches_expected"] = False
    with pytest.raises(ReviewCoverageError, match="answerability"):
        normalize(review)


@pytest.mark.parametrize(
    "reviews",
    [[], [{"blind_id": "A001"}, {"blind_id": "A001"}], [{"blind_id": "A002"}]],
)
def test_review_count_alone_cannot_hide_missing_duplicate_or_unexpected_ids(reviews):
    with pytest.raises(ReviewCoverageError, match="coverage incomplete"):
        validate_coverage({"A001": {}}, reviews)
