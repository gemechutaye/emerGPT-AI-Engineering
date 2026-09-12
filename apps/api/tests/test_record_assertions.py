"""Source-aware record-language clarity, with unrelated record identities."""

from hashlib import sha256

import pytest
from emer.contracts.answer import DraftCitation, DraftStatement, SourceDocument
from emer.domain.record_assertions import record_assertion_problems


def source(doc_id, text, *, patient=True):
    return SourceDocument(
        doc_id=doc_id, title=f"Record {doc_id}", category="synthetic_patient" if patient else "procedure",
        version="1", effective_date="2026-01-01", authority="fixture", text=text,
        sha256=sha256(text.encode()).hexdigest(),
    )


def problems(text, *sources):
    statement = DraftStatement(part_id="part-1", text=text, citations=[
        DraftCitation(doc_id=doc.doc_id, quote=doc.text) for doc in sources
    ])
    return record_assertion_problems(statement, sources)


UNKNOWN = source("record-alpha", "Interested in non-surgical options. "
                 "No procedure history relevant to target area documented yet. "
                 "Photography and measurements completed. Provider assessment pending.")
GENERAL = source("guide-beta", "The practice documents baseline appearance and treatment areas. "
                 "The licensed provider determines product choice and dose.", patient=False)
SCHEDULED = source("record-gamma", "Previously received neuromodulator treatment elsewhere. "
                   "New-patient consultation scheduled. No treatment decision documented yet.")


@pytest.mark.parametrize("claim", [
    "The record documents interest in non-surgical options and no procedure history relevant to the target area so far.",
    "There is no relevant procedure history.",
    "The patient has no prior procedures.",
    "They have no surgical history.",
    "They are without any treatment history.",
])
def test_undocumented_history_cannot_be_unqualified_negative(claim):
    assert any("Negative history" in problem for problem in problems(claim, UNKNOWN))


@pytest.mark.parametrize("claim", [
    "No relevant procedure history is documented yet.",
    "There is no documented procedure history.",
    "The procedure history is unknown.",
    "The record documents no known procedure history.",
    "Missing documentation does not mean there is no procedure history.",
    "The record cannot establish that there is no prior treatment history.",
    "The patient is interested in non-surgical options; procedure history is not documented.",
])
def test_explicit_uncertainty_and_denied_inference_are_allowed(claim):
    assert problems(claim, UNKNOWN) == []


@pytest.mark.parametrize("record", [
    "No prior RF microneedling. Educational consultation completed.",
    "The patient reports no prior procedures. Consultation scheduled.",
    "No surgical history. Assessment pending.",
])
def test_explicit_negative_patient_facts_are_not_blanket_rejected(record):
    assert problems("The patient has no prior procedures.", source("unrelated-record", record)) == []


def test_unrelated_retrieved_patient_cannot_supply_or_contaminate_history_support():
    explicit = source("record-delta", "No procedure history. Consultation scheduled.")
    assert problems("record-delta has no procedure history.", UNKNOWN, explicit) == []
    assert problems("record-alpha has no procedure history.", UNKNOWN, explicit)
    statement = DraftStatement(part_id="part-1", text="There is no procedure history.", citations=[
        DraftCitation(doc_id=explicit.doc_id, quote=explicit.text),
    ])
    assert record_assertion_problems(statement, [UNKNOWN, explicit]) == []


@pytest.mark.parametrize("claim", [
    ("The provider's consultation review includes record-gamma's goals, prior response, relevant medical history, "
     "medications, asymmetry, and desired degree of movement, with baseline appearance and treatment areas documented."),
    "For record-gamma, baseline appearance and treatment areas are documented.",
    "Baseline appearance has been recorded.",
    "The consultation was completed.",
    "For record-gamma, the injection was administered.",
    "The provider should assess the patient, with baseline appearance documented.",
])
def test_general_overview_cannot_establish_patient_completed_work(claim):
    assert any("Mixed patient" in problem for problem in problems(claim, SCHEDULED, GENERAL))


@pytest.mark.parametrize("claim", [
    "For record-gamma, the licensed provider must determine product choice and dose.",
    "Baseline appearance and treatment areas should be documented.",
    "Baseline appearance needs to be documented.",
    "No treatment decision is documented yet.",
    "The general overview says baseline appearance and treatment areas are documented.",
    "According to the practice workflow, baseline appearance and treatment areas are documented.",
    "record-gamma has a consultation scheduled. The general guidance says baseline appearance is documented.",
])
def test_general_attribution_prospective_actions_and_missingness_are_allowed(claim):
    assert problems(claim, SCHEDULED, GENERAL) == []


@pytest.mark.parametrize("claim,record", [
    ("Photography and measurements are completed.", UNKNOWN),
    ("Follow-up is documented.", source("another-record", "Follow-up in six weeks.")),
    ("Baseline appearance and treatment areas are documented.", source(
        "completed-record", "Baseline appearance and treatment areas documented. Consultation completed.",
    )),
])
def test_affirmative_patient_record_events_remain_allowed(claim, record):
    assert problems(claim, record, GENERAL) == []


def test_other_patients_completed_event_cannot_support_named_patient():
    completed = source("record-delta", "Baseline appearance and treatment areas documented.")
    assert problems("record-gamma baseline appearance is documented.", SCHEDULED, completed, GENERAL)
    assert problems("record-delta baseline appearance is documented.", SCHEDULED, completed, GENERAL) == []


def test_general_only_claim_does_not_need_patient_evidence():
    assert problems("Baseline appearance and treatment areas are documented.", GENERAL) == []


@pytest.mark.parametrize("claim", [
    ("The definition requires that the first paid treatment be completed, whereas record-alpha only has "
     "photography and measurements completed and provider assessment pending."),
    "The completed photography and measurements meet a documented progress-recording activity.",
    "Converted is defined as the first paid treatment having been completed.",
    "The record documents completed photography and measurements.",
    "The overview says patients should disclose sun exposure, so the patient's documented exposure is relevant.",
    "The status definition is first paid treatment completed, while the record documents completed photography.",
    "What meets the record: photography and measurements are completed.",
    "No injectable plan has been finalized: Consultation incomplete pending prior product/treatment history.",
])
def test_definitions_adjectives_and_different_negative_clauses_do_not_assert_completed_work(claim):
    assert problems(claim, UNKNOWN, GENERAL) == []


def test_explicit_negative_history_is_not_contaminated_by_another_unknown_history_field():
    record = source("new-record", "Surgical history unknown. No allergy history.")
    assert problems("There is no allergy history.", record) == []
    assert problems("There is no surgical history.", record)


def test_guard_is_pure_and_does_not_rewrite_claims_or_evidence():
    statement = DraftStatement(part_id="part-1", text="There is no procedure history.", citations=[
        DraftCitation(doc_id=UNKNOWN.doc_id, quote=UNKNOWN.text),
    ])
    before = statement.model_dump()
    assert record_assertion_problems(statement, [UNKNOWN])
    assert statement.model_dump() == before
