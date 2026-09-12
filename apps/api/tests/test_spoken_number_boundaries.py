"""Complete spoken references must never silently select a smaller patient identifier."""

from pathlib import Path

import pytest
from emer.domain.scope import (
    ScopeError,
    canonicalize_patient_mentions,
    normalize_patient,
    patient_ids,
    patient_reference_clarification,
)
from emer.services.ingestion import ingest
from emer.services.retrieval import RetrievalService


@pytest.mark.parametrize(
    ("reference", "expected"),
    [
        ("patient six", "PT-006"),
        ("PT zero zero six", "PT-006"),
        ("P.T. 006", "PT-006"),
        ("patient twenty three", "PT-023"),
        ("patient twenty-three", "PT-023"),
        ("patient one hundred", "PT-100"),
        ("patient one hundred and six", "PT-106"),
        ("patient one thousand one hundred and six", "PT-1106"),
        ("patient one thousand and six", "PT-1006"),
        ("patient 999999", "PT-999999"),
    ],
)
def test_complete_supported_numbers_keep_their_identity(reference, expected):
    assert normalize_patient(reference) == expected
    assert patient_ids(f"What is {reference}'s status?") == [expected]
    assert patient_reference_clarification(reference) is None


@pytest.mark.parametrize(
    "question",
    [
        "The patient twenty one years old asked about filler.",
        "The patient twenty-three years old asked about filler.",
        "The patient zero zero six weeks after treatment.",
        "The patient one hundred days after treatment.",
        "The patient one hundred and six days after treatment.",
        "The patient 2 weeks after treatment.",
        "The patient one and a half weeks after treatment.",
    ],
)
def test_quantity_suffix_is_checked_after_the_whole_number(question):
    assert canonicalize_patient_mentions(question) == question
    assert patient_ids(question) == []
    assert patient_reference_clarification(question) is None


@pytest.mark.parametrize(
    "reference",
    [
        "patient one million",
        "patient one billion",
        "patient one twenty",
        "patient twenty hundred",
        "patient one and two",
        "patient six two hundred",
        "patient one thousand two thousand",
        "patient 6 two",
        "patient 1000000",
        "patient zero zero zero zero zero zero six",
        "patient 6.5",
        "patient six point five",
        "patient one and a half",
    ],
)
def test_unsupported_complete_numbers_request_digits_instead_of_selecting_a_prefix(reference):
    message = patient_reference_clarification(reference)
    assert message and "complete patient identifier using digits" in message
    with pytest.raises(ScopeError, match="complete patient identifier using digits"):
        patient_ids(reference)


def test_comparison_and_ordinary_following_words_still_preserve_identifiers():
    assert patient_ids("Compare patient one and patient six.") == ["PT-001", "PT-006"]
    assert patient_ids("Tell me about patient six and what they asked about.") == ["PT-006"]
    assert patient_ids("Did patient six schedule a weekend visit?") == ["PT-006"]


def test_newline_starts_a_separate_context_field_not_another_identifier_digit():
    assert patient_ids("PT5\n2026-09-11") == ["PT-005"]
    assert patient_ids("patient six\n2 weeks") == ["PT-006"]


@pytest.fixture(scope="module")
def retriever():
    root = Path(__file__).resolve().parents[3]
    return RetrievalService(ingest(root / "config/corpus.json"))


@pytest.mark.parametrize(
    "reference", ["patient one hundred", "patient one hundred and six", "PT-100", "PT-999999"]
)
def test_complete_unknown_number_never_retrieves_a_smaller_existing_patient(retriever, reference):
    packet = retriever.evidence(f"What is {reference}'s status?")
    assert packet.scopes[0].unknown_patient_ids == [normalize_patient(reference)]
    assert packet.scopes[0].patient_ids == []
    assert not packet.scopes[0].patient_discovery
    assert not any(source.category == "synthetic_patient" for source in packet.sources)
