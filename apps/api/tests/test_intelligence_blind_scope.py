"""Regressions consumed from the final blind HTTP evaluation; no provider calls."""

from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest
from emer.domain.scope import (
    ScopeError,
    date_scope_clarification,
    dismissed_context_question,
    requested_patient_ids,
    resolve_scopes,
)
from emer.services.ingestion import ingest
from emer.services.retrieval import RetrievalService
from emer.services.text_intent import general_lookup, record_topic_followup, resolve_text_intent

ROOT = Path(__file__).resolve().parents[3]


class NoProvider:
    async def structured(self, *args, **kwargs):
        pytest.fail("An explicit point comparison or context dismissal needs no model resolution")


@pytest.fixture(scope="module")
def retrieval():
    return RetrievalService(ingest(ROOT / "config/corpus.json"))


BLIND_DATES = (
    "I'm reviewing routine appointment cancellations from June 18 and August 18, 2026. "
    "What notice window applied on each date, and was a cancellation fee automatic under either version?"
)


async def test_blind_point_comparison_keeps_both_dates_and_policies_without_provider(retrieval):
    result = await resolve_text_intent(NoProvider(), BLIND_DATES, [], None, None)
    assert result.question == BLIND_DATES and result.clarification is None
    packet = retrieval.evidence(result.question, mode="lexical", top_k=3, as_of="2027-01-01")
    assert [(s.as_of, s.applicable_policy_ids) for s in packet.scopes] == [
        ("2026-06-18", ["OPS-306-V1"]), ("2026-08-18", ["OPS-306-V2"]),
    ]
    assert {"OPS-306-V1", "OPS-306-V2"} <= {source.doc_id for source in packet.sources}
    assert all(s.date_origin == "question" for s in packet.scopes)


@pytest.mark.parametrize("question,expected", [
    ("Records from April 9 and November 7, 2028: which value held on each date?", {"2028-04-09", "2028-11-07"}),
    ("Records from June 18, 2025 and August 18, 2026: what applied under either version?", {"2025-06-18", "2026-08-18"}),
    ("Records from 2028-04-09 and November 7, 2028: what applied on both dates?", {"2028-04-09", "2028-11-07"}),
    ("Records from April 9 and 2028-11-07: what applied on each date?", {"2028-04-09", "2028-11-07"}),
    ("Compare records from April 9, 2028 and November 7, 2028.", {"2028-04-09", "2028-11-07"}),
    ("Records from April 9, and November 7, 2028: what applied on each date?", {"2028-04-09", "2028-11-07"}),
])
def test_point_comparisons_use_only_supplied_years(question, expected):
    assert date_scope_clarification(question) is None
    scopes = resolve_scopes(question, [], today=date(2030, 1, 1))
    assert {s.as_of for s in scopes} == expected


@pytest.mark.parametrize("question", [
    "Which policy applied from June 18 and August 18, 2026?",
    "Which policy applied throughout the period from June 18 and August 18, 2026, on each date?",
    "What applied between June 18 and August 18, 2026, on each date?",
    "What applied before June 18 and August 18, 2026, under either version?",
    "What applied after June 18 and August 18, 2026, on each date?",
    "What applied from June 18 through August 18, 2026, under either version?",
    "What applied from June 18 to August 18, 2026, under either version?",
])
def test_point_comparison_markers_do_not_convert_ranges_or_conditions(question):
    assert date_scope_clarification(question)
    with pytest.raises(ScopeError, match="exact date"):
        resolve_scopes(question, [], today=date(2030, 1, 1))


@pytest.mark.parametrize("question", [
    "Compare June 18 and August 18, 2026 against November 7, 2027.",
    "Compare June 18 against 2025-08-18 and 2026-11-07.",
    "PT-001 on June 18 and PT-002 on August 18, 2026; PT-003 on November 7, 2027?",
])
async def test_multiple_explicit_years_do_not_arbitrarily_fill_missing_year(question):
    assert "year for each" in date_scope_clarification(question)
    with pytest.raises(ScopeError, match="year for each"):
        resolve_scopes(question, [], today=date(2030, 1, 1))
    result = await resolve_text_intent(NoProvider(), question, [], None, None)
    assert result.clarification and not result.question


def test_shared_year_survives_patient_date_clause_partition(retrieval):
    scopes = resolve_scopes(
        "Summarize PT-001 on June 18 and PT-002 on August 18, 2026.",
        retrieval.bundle.documents, today=date(2030, 1, 1),
    )
    assert [(s.patient_ids, s.as_of) for s in scopes] == [
        (["PT-001"], "2026-06-18"), (["PT-002"], "2026-08-18"),
    ]


NEW_CALLER = (
    "A different caller says swelling is rapidly worsening after a procedure. "
    "Can the assistant reassure them this is routine, or what does our communication guidance say to do?"
)


@pytest.mark.parametrize("dismissal", [
    "Leave that patient aside. ", "Leave PT-003 aside. ", "Put the previous case aside; ",
    "Set this topic aside: ", "Please leave case three aside for now, ",
    "Leave patient three aside and ",
])
async def test_explicit_dismissal_cannot_carry_old_patient_into_new_caller(retrieval, dismissal):
    question = dismissal + NEW_CALLER
    history = ["What medication was listed for PT-003, and what follow-up is recorded?"]
    assert general_lookup(question)
    source = next(d for d in retrieval.bundle.documents if d.doc_id == "PT-003")
    assert not record_topic_followup(question, source.text)
    assert requested_patient_ids(question) == []
    result = await resolve_text_intent(NoProvider(), question, history, "PT-003", None)
    assert result.question == NEW_CALLER and result.clarification is None
    for lookup in [question, result.question]:
        # Direct service callers supplying stale context must get the same scope boundary.
        packet = retrieval.evidence(lookup, mode="lexical", top_k=4, patient_id="PT-003" if lookup == question else None)
        assert all(not s.patient_ids and not s.source_reference_ids for s in packet.scopes)
        assert all(source.category != "synthetic_patient" for source in packet.sources)
    assert history == ["What medication was listed for PT-003, and what follow-up is recorded?"]


async def test_dismissal_allows_an_explicit_replacement_and_later_topic_return(retrieval):
    replacement = "Leave PT-003 aside. What follow-up is documented for PT-008?"
    assert requested_patient_ids(replacement) == ["PT-008"]
    result = await resolve_text_intent(NoProvider(), replacement, ["Summarize PT-003."], "PT-003", None)
    packet = retrieval.evidence(result.question, mode="lexical", patient_id="PT-008")
    assert all(s.patient_ids == ["PT-008"] for s in packet.scopes)
    returned = await resolve_text_intent(NoProvider(), "Summarize PT-003's medication.", [], None, None)
    assert requested_patient_ids(returned.question) == ["PT-003"]


@pytest.mark.parametrize("remainder", ["What about their follow-up?", "And their medication?", ""])
async def test_dismissal_does_not_resolve_an_unidentified_remainder_from_old_record(remainder):
    result = await resolve_text_intent(NoProvider(), "Leave that patient aside. " + remainder,
                                       ["Summarize PT-003."], "PT-003", None)
    assert result.clarification and not result.question


@pytest.mark.parametrize("question", [
    "PT-003 has no medication listed; what about follow-up?",
    "Leave PT-003's medication aside and summarize their current status.",
    "Compare PT-003 and PT-008; leave scheduling aside.",
    "What does 'leave that patient aside' mean in this note for PT-003?",
])
def test_ordinary_negative_facts_subtopics_and_quoted_text_do_not_dismiss_identity(question):
    assert dismissed_context_question(question) is None
    assert "PT-003" in requested_patient_ids(question)


async def test_malformed_dismissed_identifier_still_requires_complete_number():
    result = await resolve_text_intent(NoProvider(), "Leave case one million aside. " + NEW_CALLER,
                                       ["Summarize PT-003."], "PT-003", None)
    assert result.clarification and not result.question


async def test_later_followup_cannot_resurrect_an_identifier_from_before_dismissal():
    class ContaminatingDouble:
        async def structured(self, instructions, payload, schema, **kwargs):
            assert payload["active_patient_ids"] == []
            assert all("PT-003" not in previous for previous in payload["previous_questions"])
            return SimpleNamespace(value=schema.model_validate({
                "question": "What medication does PT-003 take?", "clarification": None,
            }))

    result = await resolve_text_intent(ContaminatingDouble(), "What about their medication?", [
        "What medication does PT-003 take?", "Leave PT-003 aside. " + NEW_CALLER,
    ], None, None)
    assert result.clarification and not result.question
