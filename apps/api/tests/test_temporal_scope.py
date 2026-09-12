"""Point-date scope cannot stand in for temporal conditions or ranges."""

from datetime import date
from pathlib import Path

import pytest
from emer.domain.scope import ScopeError, date_scope_clarification, resolve_scopes
from emer.services.ingestion import ingest
from emer.services.retrieval import RetrievalService

ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.parametrize(
    "relation",
    [
        "before",
        "after",
        "since",
        "until",
        "through",
        "between",
        "from",
        "prior to",
        "later than",
        "earlier than",
        "on or before",
        "on or after",
    ],
)
@pytest.mark.parametrize("calendar_date", ["July 1, 2026", "2026-07-01", "May"])
def test_relative_date_requires_exact_date_before_resolving_scope(relation, calendar_date):
    question = f"Which cancellation policy applied {relation} {calendar_date}?"
    message = date_scope_clarification(question)
    assert message and "Which exact date" in message
    with pytest.raises(ScopeError) as error:
        resolve_scopes(question, [], as_of="2026-06-30")
    assert str(error.value) == message


@pytest.mark.parametrize(
    "question",
    [
        "Cancellation policy between March and September 2026?",
        "Cancellation policy from March to September 2026?",
        "Cancellation policy March through September 2026?",
        "Cancellation policy March to September 2026?",
        "Cancellation policy March–September 2026?",
        "Cancellation policy July 1, 2026 - September 30, 2026?",
        "Cancellation policy 2026-06-30 to 2026-07-01?",
        "Cancellation policy 2026-06-30—2026-07-01?",
        "Cancellation policy July 1–3, 2026?",
        "Cancellation policy July 1 to 3, 2026?",
        "Cancellation policy July 1st through 3rd, 2026?",
        "Compare policy throughout the period from March to September 2026.",
        "Compare policy for the interval from June 1, 2026 to July 1, 2026.",
        "Compare policy throughout March to September 2026.",
        "Compare policy from June 1, 2026 through July 1, 2026.",
        "Which policy is effective from July 1, 2026 to September 1, 2026?",
        "Which policy is effective from July 1, 2026 through September 1, 2026?",
    ],
)
def test_date_range_is_not_reinterpreted_as_independent_dates(question):
    assert date_scope_clarification(question)
    with pytest.raises(ScopeError, match="exact date"):
        resolve_scopes(question, [], today=date(2026, 9, 11))


@pytest.mark.parametrize(
    "question,expected",
    [
        ("Cancellation as of July 1, 2026?", ["2026-07-01"]),
        ("Cancellation on 2026-06-30?", ["2026-06-30"]),
        ("Cancellation on May 1, 2026?", ["2026-05-01"]),
        ("Compare March and September 2026.", ["2026-03-01", "2026-09-01"]),
        ("Compare May and September 2026.", ["2026-05-01", "2026-09-01"]),
        ("Compare March to September 2026.", ["2026-03-01", "2026-09-01"]),
        ("Compare 2026-06-30 and 2026-07-01.", ["2026-06-30", "2026-07-01"]),
        ("Compare 2026-06-30 to 2026-07-01.", ["2026-06-30", "2026-07-01"]),
        ("Compare March 1 versus September 1, 2026.", ["2026-03-01", "2026-09-01"]),
        (
            "Compare the cancellation policy from June 1, 2026 to July 1, 2026.",
            ["2026-06-01", "2026-07-01"],
        ),
        ("Compare cancellation from 2026-06-01 to 2026-07-01.", ["2026-06-01", "2026-07-01"]),
        ("Which policy is effective from July 1, 2026?", ["2026-07-01"]),
        ("Which policy is effective from 2026-07-01?", ["2026-07-01"]),
    ],
)
def test_exact_dates_and_explicit_comparisons_keep_their_scope(question, expected):
    assert date_scope_clarification(question) is None
    scopes = resolve_scopes(question, [], today=date(2026, 9, 11))
    assert [scope.as_of for scope in scopes] == expected
    assert all(scope.date_origin == "question" for scope in scopes)


@pytest.mark.parametrize(
    "question",
    [
        "What may the assistant do before treatment?",
        "May I cancel or reschedule my appointment?",
        "What symptoms may occur after treatment?",
        "Before treatment, may a patient request a consultation?",
        "What happens after a consultation?",
        "What changed since the previous policy?",
        "Which sources should I compare before answering?",
    ],
)
def test_modal_may_and_noncalendar_conditions_do_not_trigger_date_clarification(question):
    assert date_scope_clarification(question) is None
    scopes = resolve_scopes(question, [], as_of="2026-09-11")
    assert [scope.as_of for scope in scopes] == ["2026-09-11"]
    assert scopes[0].date_origin == "context"


def test_relative_date_cannot_reach_policy_selection(monkeypatch):
    retriever = RetrievalService(ingest(ROOT / "config/corpus.json"))

    def unexpected_policy_selection(*args, **kwargs):
        pytest.fail("An unresolved temporal condition reached policy selection")

    monkeypatch.setattr("emer.services.retrieval.apply_policies", unexpected_policy_selection)
    with pytest.raises(ScopeError, match="exact date"):
        retriever.evidence("Which policy applied before July 1, 2026?", as_of="2026-06-30")


@pytest.mark.parametrize(
    "question,expected_dates",
    [
        ("Compare cancellation on 2026-06-30 and 2026-07-01.", ["2026-06-30", "2026-07-01"]),
        (
            "Compare the cancellation policy from June 1, 2026 to July 1, 2026.",
            ["2026-06-01", "2026-07-01"],
        ),
    ],
)
def test_exact_policy_boundary_comparison_keeps_both_applicable_revisions(question, expected_dates):
    retriever = RetrievalService(ingest(ROOT / "config/corpus.json"))
    packet = retriever.evidence(question)
    assert [(scope.as_of, scope.applicable_policy_ids) for scope in packet.scopes] == [
        (expected_dates[0], ["OPS-306-V1"]),
        (expected_dates[1], ["OPS-306-V2"]),
    ]
