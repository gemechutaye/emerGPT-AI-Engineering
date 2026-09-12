"""Natural and spoken patient references resolve the same way for typed and Live questions.

Deterministic; original corpus; provider doubles only. No live-model quality claim.
"""

from pathlib import Path

import pytest
from emer.contracts.answer import ProviderUsage
from emer.domain.scope import (
    canonicalize_patient_mentions,
    non_patient_numbers,
    normalize_patient,
    patient_ids,
    resolve_scopes,
)
from emer.providers.openrouter import StructuredResult
from emer.services.answering import locate_quote
from emer.services.ingestion import ingest
from emer.services.retrieval import RetrievalService
from emer.services.text_intent import ResolvedTextIntent, resolve_text_intent, resolved_detail_conflict

ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture(scope="module")
def retriever():
    return RetrievalService(ingest(ROOT / "config/corpus.json"))


def retrieved_patients(packet):
    return {source.doc_id for source in packet.sources if source.category == "synthetic_patient"}


@pytest.mark.parametrize(
    "text",
    [
        "What is PT-006's status?",
        "What is pt006's status?",
        "What is PT 6's status?",
        "What is P.T. 006's status?",
        "What is P T 6's status?",
        "What is patient 6's status?",
        "What is patient #6's status?",
        "What is patient number 6's status?",
        "What is patient no. 006's status?",
        "What is patient ID 6's status?",
        "What is patient six's status?",
        "What is patient number six's status?",
        "What is PT zero zero six's status?",
        "What is PT six's status?",
        "Tell me about patient six.",
    ],
)
def test_spoken_and_loose_forms_resolve_to_one_identifier(text):
    assert patient_ids(text) == ["PT-006"]
    assert "PT-006" in canonicalize_patient_mentions(text)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Compare patient one and patient six.", ["PT-001", "PT-006"]),
        ("patient twenty three", ["PT-023"]),
        ("PT-1234", ["PT-1234"]),
    ],
)
def test_multiple_and_larger_identifiers(text, expected):
    assert patient_ids(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "Follow-up for the patient in 6 weeks",
        "The patient two weeks after treatment",
        "patient 2 weeks",
        "Which of the 8 patients have follow-ups?",
        "All eight patients",
        "the patient's age is 34",
        "Office hours end at 6 PM PT",
        "patient one of the group",
    ],
)
def test_quantities_and_plurals_are_not_identifiers(text):
    assert patient_ids(text) == []


def test_normalize_context_identifier_accepts_natural_form():
    assert normalize_patient("patient six") == "PT-006"
    assert normalize_patient(" PT 6 ") == "PT-006"


def test_non_patient_numbers_ignore_spoken_and_canonical_identifiers():
    assert non_patient_numbers("patient six, follow-up in 6 weeks") == {6}
    assert non_patient_numbers("PT-006 has a 2 week follow-up") == {2}
    assert non_patient_numbers("PT-006") == set()


def test_spoken_identifier_selects_only_that_record(retriever):
    packet = retriever.evidence("What does the record say about patient six?")
    assert packet.scopes[0].patient_ids == ["PT-006"]
    assert not packet.scopes[0].patient_discovery
    assert retrieved_patients(packet) == {"PT-006"}
    assert "PT-006" in packet.scopes[0].question


def test_spoken_unknown_identifier_is_still_never_substituted(retriever):
    packet = retriever.evidence("What is patient nine's status?")
    assert packet.scopes[0].unknown_patient_ids == ["PT-009"]
    assert not packet.scopes[0].patient_ids and not packet.scopes[0].patient_discovery
    assert not retrieved_patients(packet)


@pytest.mark.parametrize("mode", ["full", "lexical", "hybrid_unavailable"])
def test_descriptive_patient_question_discovers_the_matching_record(retriever, mode):
    """W2's reproduced gap: a description without an identifier previously selected zero records."""
    question = "Which patient had their light-based procedure discussion postponed after sun exposure?"
    if mode == "hybrid_unavailable":
        with pytest.raises(ValueError):
            retriever.evidence(question, mode="hybrid")
        return
    packet = retriever.evidence(question, mode=mode, top_k=3)
    scope = packet.scopes[0]
    assert scope.patient_discovery and not scope.all_patients
    assert "PT-003" in retrieved_patients(packet)
    assert "PT-003" in scope.patient_ids and "PT-003" in scope.allowed_source_ids
    if mode == "lexical":
        # Ranking, not enrolment, brought the record in; the packet stays bounded.
        assert len(packet.sources) <= 3 + len(packet.retrieval["policy_family_expansion"])


def test_which_patient_overrides_inherited_context(retriever):
    packet = retriever.evidence("Which patient wants to control breakouts first?", patient_id="PT-006")
    assert packet.scopes[0].patient_discovery
    assert "PT-004" in retrieved_patients(packet)


def test_pronoun_keeps_inherited_context(retriever):
    packet = retriever.evidence("What is their follow-up interval?", patient_id="PT-004")
    assert packet.scopes[0].patient_ids == ["PT-004"] and not packet.scopes[0].patient_discovery


def test_general_question_without_patient_does_not_admit_patient_records(retriever):
    packet = retriever.evidence("What are the office hours?")
    scope = packet.scopes[0]
    assert not scope.patient_discovery and not scope.patient_ids
    assert not retrieved_patients(packet)
    assert "OPS-307" in scope.allowed_source_ids


def test_single_named_patient_applies_to_every_part_without_discovery():
    documents = ingest(ROOT / "config/corpus.json").documents
    scopes = resolve_scopes("What is patient six's status? What is the cancellation window?", documents)
    assert [scope.patient_ids for scope in scopes] == [["PT-006"], ["PT-006"]]
    assert [scope.patient_discovery for scope in scopes] == [False, False]


class ProviderDouble:
    def __init__(self, question="", clarification=None):
        self.value = ResolvedTextIntent(question=question, clarification=clarification)
        self.calls = []

    async def structured(self, instructions, payload, schema, **options):
        self.calls.append(payload)
        return StructuredResult(
            value=self.value,
            usage=ProviderUsage(operation="text_intent_resolution", model="test-double", latency_ms=0),
        )


async def test_first_spoken_question_is_canonicalized_without_provider_call():
    provider = ProviderDouble()
    result = await resolve_text_intent(provider, "What is patient six's current status?", [], None, None)
    assert result == ResolvedTextIntent(question="What is PT-006's current status?", clarification=None)
    assert not provider.calls


async def test_spoken_correction_alone_still_requires_a_task():
    result = await resolve_text_intent(ProviderDouble(), "No, patient five", [], None, None)
    assert not result.question and result.clarification


async def test_resolved_canonical_identifier_matches_spoken_history():
    provider = ProviderDouble("What is PT-006's follow-up interval?")
    result = await resolve_text_intent(
        provider, "and their follow-up interval?", ["Tell me about patient six."], None, None
    )
    assert result.question == "What is PT-006's follow-up interval?"
    assert result.clarification is None
    assert provider.calls[0]["active_patient_ids"] == ["PT-006"]


async def test_resolved_identifier_never_seen_in_any_form_is_rejected():
    provider = ProviderDouble("What is PT-005's follow-up interval?")
    result = await resolve_text_intent(
        provider, "and their follow-up interval?", ["Tell me about patient six."], None, None
    )
    assert not result.question and "patient" in result.clarification.lower()


def test_shared_guard_accepts_spoken_forms_and_rejects_new_details():
    assert resolved_detail_conflict("What is PT-006's status?", "tell me about patient six") is None
    assert resolved_detail_conflict("What is PT-006's status?", "tell me about patient five") is not None
    assert resolved_detail_conflict("Is a 48 hour notice required?", "is forty-eight hours required") is not None
    assert resolved_detail_conflict("Is a 48 hour notice required?", "is 48 hours notice required") is None
    assert resolved_detail_conflict("policy in March 2026", "what about september") is not None


# --- citation quote location: tolerant matching, exact publication ---------------------------------

@pytest.mark.parametrize(
    ("text", "quote", "expected"),
    [
        ("Consultation completed.  Provider discussed a plan.", "completed. Provider discussed", "completed.  Provider discussed"),
        ("Patient’s tolerance for downtime.", "Patient's tolerance", "Patient’s tolerance"),
        ("Fee – waived by management.", "Fee - waived", "Fee – waived"),
        ("Follow-up task due in 2 weeks.", "follow-up task due in 2 weeks", "Follow-up task due in 2 weeks"),
        ("Exact text here.", "Exact text", "Exact text"),
    ],
)
def test_locate_quote_tolerates_copy_differences_but_publishes_original_span(text, quote, expected):
    start, end = locate_quote(text, quote)
    assert text[start:end] == expected


@pytest.mark.parametrize("quote", ["not present", "Exact text here. Extra", "text  here  and more", ""])
def test_locate_quote_rejects_non_passages(quote):
    assert locate_quote("Exact text here.", quote) is None


def test_how_many_patients_is_explicit_enumeration(retriever):
    packet = retriever.evidence("how many patients are there and what are their primary goals", mode="lexical", top_k=2)
    scope = packet.scopes[0]
    assert scope.all_patients and not scope.patient_discovery
    assert len(retrieved_patients(packet)) == 8


# --- review fixes: unresolved pronouns, self-contained follow-ups, empty drafts -------------------

from emer.services.text_intent import self_contained, unresolved_pronoun


@pytest.mark.parametrize(
    "question",
    ["Do they have any allergies documented?", "What is her follow-up interval?", "Has their plan been finalized?"],
)
async def test_first_turn_pronoun_without_any_person_asks_which_patient(question):
    provider = ProviderDouble()
    result = await resolve_text_intent(provider, question, [], None, None)
    assert not result.question and "patient" in result.clarification.lower()
    assert not provider.calls


@pytest.mark.parametrize(
    "question",
    [
        "What should the patient tell us about their sun exposure before IPL?",
        "If a patient has active acne, should they get scar treatment?",
        "Which patient reports sensitive skin, and what is their goal?",
        "What is patient six's status and did they ask about cost?",
    ],
)
def test_pronoun_with_an_antecedent_or_discovery_cue_is_not_unresolved(question):
    assert not unresolved_pronoun(question)


async def test_first_turn_pronoun_with_selected_patient_is_answered_not_clarified():
    result = await resolve_text_intent(ProviderDouble(), "Do they have allergies documented?", [], "PT-004", None)
    assert result.question and result.clarification is None


@pytest.mark.parametrize(
    "question",
    ["What about the cancellation window?", "PT-004's follow-up", "How much is a skincare consultation?"],
)
def test_concrete_follow_ups_are_self_contained(question):
    assert self_contained(question)


@pytest.mark.parametrize(
    "question",
    ["same question for patient 8", "no I meant patient 7", "And what is it now?", "Has their plan been finalized?", "follow-up"],
)
def test_dependent_follow_ups_still_need_resolution(question):
    assert not self_contained(question)


async def test_model_doubt_cannot_block_a_self_contained_follow_up():
    provider = ProviderDouble("", "Which patient do you mean?")
    result = await resolve_text_intent(
        provider, "What about the cancellation window?", ["What did PT-005 come in for?"], None, None
    )
    assert result == ResolvedTextIntent(question="What about the cancellation window?", clarification=None)


async def test_model_doubt_still_clarifies_a_dependent_follow_up():
    provider = ProviderDouble("", "Which patient do you mean?")
    result = await resolve_text_intent(provider, "And what is it now?", ["What did PT-005 come in for?"], None, None)
    assert not result.question and result.clarification


async def test_twice_empty_draft_publishes_unsupported_instead_of_failing(retriever):
    from emer.contracts.answer import ProviderDraft
    from emer.services.answering import AnsweringService

    class EmptyProvider:
        calls = 0

        async def structured(self, system, payload, schema, **kwargs):
            EmptyProvider.calls += 1
            assert schema is ProviderDraft, "the checker must not run for an empty draft"
            return StructuredResult(
                value=ProviderDraft(statements=[], gaps=[], next_steps=[]),
                usage=ProviderUsage(operation=kwargs.get("operation", "generation"), model="test-double", latency_ms=1),
            )

    packet = retriever.evidence("asdf qwerty zxcv lorem")
    answer = await AnsweringService(EmptyProvider()).answer(packet, "asdf qwerty zxcv lorem")
    assert answer.status == "unsupported" and not answer.statements
    assert [gap.part_id for gap in answer.gaps] == [scope.part_id for scope in packet.scopes]
    assert answer.diagnostics["empty_draft_fallback"] is True
    assert answer.diagnostics["support_check"] == "not_run" and answer.diagnostics["repair_count"] == 1
    assert EmptyProvider.calls == 2 and len(answer.usage) == 2
