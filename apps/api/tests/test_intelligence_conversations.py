"""Consumed holdout regressions for intent; deterministic transformations are not source answers."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from emer.domain.scope import canonicalize_patient_mentions, patient_ids
from emer.services.ingestion import ingest
from emer.services.retrieval import RetrievalService
from emer.services.text_intent import (
    general_lookup,
    record_topic_followup,
    resolve_text_intent,
    resolved_detail_conflict,
    self_contained,
)

ROOT = Path(__file__).resolve().parents[3]


class ClarifyingDouble:
    def __init__(self, question="", clarification="Which record detail do you mean?"):
        self.calls = []
        self.value = {"question": question, "clarification": clarification}

    async def structured(self, instructions, payload, schema, **kwargs):
        self.calls.append(payload)
        return SimpleNamespace(value=schema.model_validate(self.value))


@pytest.mark.parametrize("text,expected", [
    ("Pull up case one and tell me whether a procedure is currently scheduled.", ["PT-001"]),
    ("case zero zero seven", ["PT-007"]),
    ("Case number 8", ["PT-008"]),
    ("case 9", ["PT-009"]),
    ("case 2 weeks after treatment", []),
    ("case twenty one years later", []),
])
def test_case_identifiers_use_complete_numbers_and_keep_quantities(text, expected):
    assert patient_ids(text) == expected
    if not expected:
        assert canonicalize_patient_mentions(text) == text


@pytest.mark.parametrize("text", ["case one million", "case seven point five", "case 7.5"])
async def test_unsafe_case_number_cannot_be_truncated_to_another_patient(text):
    provider = ClarifyingDouble()
    result = await resolve_text_intent(provider, "Summarize " + text, [], None, None)
    assert result.clarification and not result.question and not provider.calls


async def test_relative_next_day_is_derived_from_latest_complete_date_and_retrieves_new_policy():
    provider = ClarifyingDouble()
    result = await resolve_text_intent(provider, "And the very next day—what is the requested notice then?", [
        "What cancellation notice is requested for September 2026?",
        "As of June 30, 2026, could providing less than 24 hours' notice for a cancellation lead to a fee?",
    ], None, None)
    assert result.clarification is None and "2026-07-01" in result.question
    assert "2026-07-01" in provider.calls[0]["question"]
    packet = RetrievalService(ingest(ROOT / "config/corpus.json")).evidence(result.question, mode="lexical", top_k=3)
    assert all(scope.as_of == "2026-07-01" and scope.applicable_policy_ids == ["OPS-306-V2"] for scope in packet.scopes)


async def test_bare_relative_day_keeps_the_previous_lookup_topic():
    result = await resolve_text_intent(ClarifyingDouble(), "What about the next day?", [
        "What cancellation notice applied on June 30, 2026?",
    ], None, None)
    assert not result.clarification and "2026-07-01" in result.question
    assert "cancellation notice" in result.question and "June 30" not in result.question


@pytest.mark.parametrize("anchor,current,expected", [
    ("What policy applied on 2026-07-01?", "The previous day?", "2026-06-30"),
    ("What policy applied on January 1, 2027?", "The Previous day?", "2026-12-31"),
    ("What policy applied on February 28, 2028?", "One day later?", "2028-02-29"),
    ("What policy applied on March 1, 2028?", "One day earlier?", "2028-02-29"),
])
async def test_relative_day_calendar_arithmetic_handles_boundaries(anchor, current, expected):
    result = await resolve_text_intent(ClarifyingDouble(), current, [anchor], None, None)
    assert not result.clarification and expected in result.question


@pytest.mark.parametrize("history,as_of", [
    (["What policy applied in June 2026?"], None),
    (["What policy applied on June 30?"], None),
    (["Compare June 30, 2026 and July 1, 2026."], None),
    (["What policy applied before July 1, 2026?"], None),
    (["What is PT-001's follow-up interval?"], "2026-06-30"),
    ([], "2026-06-30"),
])
async def test_relative_day_does_not_invent_an_anchor_from_month_policy_context_or_encounter(history, as_of):
    provider = ClarifyingDouble()
    result = await resolve_text_intent(provider, "What about the next day?", history, None, as_of)
    assert not result.question and result.clarification and not provider.calls


@pytest.mark.parametrize("resolved,observed,allowed", [
    ("Which policy applied July 1, 2026?", "Which policy applied 2026-07-01?", True),
    ("Which policy applied 2026-07-01?", "Which policy applied July 1, 2026?", True),
    ("Which policy applied July 30, 2026?", "Compare June 30, 2026 and July 1, 2026.", False),
    ("Which policy applied 2026-06-01?", "Which policy applied in June 2026?", False),
    ("What is the 14-day interval for PT-004?", "What is the interval for PT-004?", False),
    ("What is PT-004's status?", "What is PT-001's status?", False),
])
def test_equivalent_date_format_is_safe_but_date_recombination_and_new_facts_are_not(resolved, observed, allowed):
    assert (resolved_detail_conflict(resolved, observed) is None) == allowed


async def test_descriptive_disambiguation_retrieves_identity_without_inventing_patient_id():
    provider = ClarifyingDouble()
    result = await resolve_text_intent(provider, "The one who is 29 and still controlling breakouts.",
                                       ["When should the acne-scar patient follow up?"], None, None)
    assert not result.clarification and not patient_ids(result.question) and not provider.calls
    assert "29" in result.question and "controlling breakouts" in result.question
    assert "follow up" in result.question
    packet = RetrievalService(ingest(ROOT / "config/corpus.json")).evidence(result.question, mode="lexical", top_k=3)
    assert "PT-004" in packet.retrieval["raw_top_k_ids"]
    assert len(packet.scopes) == 1


async def test_demonstrative_patient_uses_latest_correction_without_demanding_unknown_answer_detail():
    provider = ClarifyingDouble(clarification="Which target area should be checked in PT-007's record?")
    question = "For this patient, does the record confirm that they never had a procedure in the target area?"
    result = await resolve_text_intent(provider, question, [
        "For PT-008, what should be kept simple?",
        "For PT-007, what assessment material has already been collected?",
    ], "PT-007", None)
    assert not result.clarification and patient_ids(result.question) == ["PT-007"]
    assert "never had a procedure" in result.question and "target area" in result.question
    assert not provider.calls


@pytest.mark.parametrize("old,correction,expected", [
    ("What is the fictional new-patient aesthetic consultation price?", "Actually, I meant the skincare consultation.",
     "What is the fictional skincare consultation price?"),
    ("What is the listed premium annual plan limit?", "I meant the basic monthly plan.",
     "What is the listed basic monthly plan limit?"),
    ("What is the documented old clinic procedure?", "I meant the new clinic procedure.",
     "What is the documented new clinic procedure?"),
    ("What is the fictional aesthetic consultation price?", "I meant the new-patient skincare consultation.",
     "What is the fictional new-patient skincare consultation price?"),
])
async def test_entity_correction_replaces_complete_old_variant_without_hybrid_modifiers(old, correction, expected):
    provider = ClarifyingDouble()
    result = await resolve_text_intent(provider, correction, [old], None, None)
    assert result.question == expected and not result.clarification and not provider.calls


async def test_corrected_nonpatient_topic_remains_single_target_for_followup():
    corrected = await resolve_text_intent(ClarifyingDouble(), "Actually, I meant the skincare consultation.",
                         ["What is the fictional new-patient aesthetic consultation price?"], None, None)
    provider = ClarifyingDouble("What deposit is required for the fictional skincare consultation?", None)
    result = await resolve_text_intent(provider, "What deposit is required for that one?", [
        "What is the fictional new-patient aesthetic consultation price?", corrected.question,
    ], None, None)
    assert not result.clarification and "new-patient" not in result.question
    assert provider.calls[0]["previous_questions"][-1] == corrected.question


async def test_unrelated_topic_still_drops_previous_patient_and_modality():
    provider = ClarifyingDouble("What is PT-007's body-contouring office schedule?", None)
    result = await resolve_text_intent(provider, "What are the office hours?", [
        "For PT-007, what assessment material has already been collected?",
    ], "PT-007", None)
    assert result.question == "What are the office hours?" and not provider.calls


@pytest.mark.parametrize("prefix", ["Separate issue now:", "New topic:", "Unrelated question:"])
async def test_explicit_topic_switch_with_a_complete_new_issue_bypasses_old_context(prefix):
    provider = ClarifyingDouble(clarification="Which AI platform or organization do you mean?")
    question = prefix + " the AI platform has an outage after hours. Who is contacted first?"
    assert self_contained(question)
    result = await resolve_text_intent(provider, question, ["What is PT-007's follow-up?"], "PT-007", None)
    assert result.question == question and not result.clarification and not provider.calls


@pytest.mark.parametrize("question", ["New topic: what about them?", "Separate issue now:", "Unrelated question: same thing."])
def test_topic_marker_does_not_make_a_vague_fragment_self_contained(question):
    assert not self_contained(question)


@pytest.mark.parametrize("history,current,expected_source,rejected_text", [
    (["What is the fictional new-patient aesthetic consultation price?",
      "What is the fictional skincare consultation price?"],
     "What deposit is required for that one?", "BIZ-501", "new-patient"),
    (["A post-procedure caller has severe pain and unexpected visual symptoms. Where should this go?",
      "Separate issue now: the AI platform has an outage after hours. Who is contacted first?"],
     "If that person does not acknowledge within the escalation window, who is next?", "OPS-307", "visual"),
])
async def test_false_clarification_can_carry_latest_lookup_topic_without_prior_answer(
    history, current, expected_source, rejected_text,
):
    provider = ClarifyingDouble(clarification="Which of the earlier topics do you mean?")
    result = await resolve_text_intent(provider, current, history, None, None)
    assert not result.clarification and current in result.question
    assert history[-1].rstrip(" ?.!") in result.question
    assert rejected_text not in result.question and not patient_ids(result.question)
    assert not resolved_detail_conflict(result.question, "\n".join([*history, current]))
    packet = RetrievalService(ingest(ROOT / "config/corpus.json")).evidence(result.question, mode="lexical", top_k=3)
    assert len(packet.scopes) == 1
    assert expected_source in packet.retrieval["raw_top_k_ids"]


async def test_topic_carry_is_general_and_does_not_fill_missing_role_or_numeric_value():
    current = "What limit applies to that plan?"
    result = await resolve_text_intent(ClarifyingDouble(), current, [
        "What is the listed premium annual plan limit?",
        "What is the listed basic monthly plan limit?",
    ], None, None)
    assert not result.clarification and "basic monthly" in result.question and "premium" not in result.question
    assert current in result.question and not any(char.isdigit() for char in result.question)


@pytest.mark.parametrize("previous,current", [
    ("Compare the standard and premium plans.", "What limit applies to that one?"),
    ("Is the standard plan or premium plan listed?", "What limit applies to that one?"),
    ("What is the standard plan limit?", "What limit applies to that earlier one?"),
    ("What is the standard plan limit?", "What about that one?"),
    ("What is PT-007's status?", "What about that person?"),
    ("Which patient had acne?", "What interval applies to that one?"),
    ("What is the standard plan limit?", "What limit applies to that one on June 1, 2026?"),
])
async def test_topic_carry_does_not_resolve_ambiguous_entities_or_dates(previous, current):
    result = await resolve_text_intent(ClarifyingDouble(), current, [previous], None, None)
    assert result.clarification and not result.question


@pytest.mark.parametrize("invented", [
    "What deposit is required for PT-004?",
    "What deposit of 25 percent is required for the skincare consultation?",
    "What deposit is required on October 1, 2026 for the skincare consultation?",
])
async def test_topic_reference_does_not_accept_provider_invented_identity_numbers_or_dates(invented):
    result = await resolve_text_intent(ClarifyingDouble(invented, None), "What deposit is required for that one?", [
        "What is the fictional skincare consultation price?",
    ], None, None)
    assert result.clarification and not result.question


def original_record(identifier):
    return next(json.loads(line)["text"] for line in (ROOT / "EMER_AI_TakeHome_Knowledge_Corpus.jsonl").read_text().splitlines()
                if json.loads(line)["doc_id"] == identifier)


@pytest.mark.parametrize("identifier,question", [
    ("PT-005", "Are outside records definitely available?"),
    ("PT-005", "Then write one sentence for an internal handoff saying whether an injectable plan is finalized."),
    ("PT-007", "Is the provider assessment still pending?"),
    ("PT-007", "Are photography and measurements completed?"),
])
def test_implicit_record_topic_is_supported_by_multiple_words_in_actual_recent_source(identifier, question):
    assert record_topic_followup(question, original_record(identifier))


@pytest.mark.parametrize("identifier,question", [
    ("PT-005", "What is the policy for obtaining outside records?"),
    ("PT-005", "Give a general overview of injectable treatment plans."),
    ("PT-005", "New topic: are outside treatment records available?"),
    ("PT-005", "Compare outside records for two patients."),
    ("PT-005", "Which patient has outside treatment records?"),
    ("PT-005", "Are Alex's outside records available?"),
    ("PT-005", "Does Alex have outside records?"),
    ("PT-005", "Does alex have outside records?"),
    ("PT-005", "Are outside records available for Alex?"),
    ("PT-005", "For Alex, are outside records available?"),
    ("PT-005", "Are outside records available for the patient named alex?"),
    ("PT-005", "Actually, for PT-007: are outside records available?"),
    ("PT-005", "Are the outside records for PT-099 available?"),
    ("PT-005", "Is that record current?"),
    ("PT-005", "Is age 47 correct as of September 1, 2026?"),
    ("PT-007", "Are outside records definitely available?"),
    ("PT-006", "What cost information is listed in the general price reference?"),
])
def test_record_topic_overlap_does_not_override_general_topics_identity_or_boilerplate(identifier, question):
    assert not record_topic_followup(question, original_record(identifier))


@pytest.mark.parametrize("question,expected", [
    ("Then what are the consultation prices?", True),
    ("Next, explain the general RF overview.", True),
    ("What about that policy?", True),
    ("What about their costs?", False),
    ("What are this patient's costs?", False),
    ("What costs are recorded for PT-006?", False),
])
def test_general_lookup_guard_preserves_explicit_or_deictic_patient_requests(question, expected):
    assert general_lookup(question) is expected


@pytest.mark.parametrize("prefix", ["Then", "Next,"])
def test_sequential_marker_keeps_a_handoff_request_dependent(prefix):
    assert not self_contained(prefix + " write a sentence saying whether an injectable plan is finalized.")


async def test_source_confirmed_patient_context_reaches_implicit_record_intent_resolution():
    question = "Are outside records definitely available?"
    assert record_topic_followup(question, original_record("PT-005"))
    provider = ClarifyingDouble("For PT-005, are outside records definitely available?", None)
    result = await resolve_text_intent(provider, question, [
        "What does PT-005's record say about prior procedures and the injectable plan?",
    ], "PT-005", None)
    assert not result.clarification and patient_ids(result.question) == ["PT-005"]
    assert provider.calls[0]["selected_context"]["patient_id"] == "PT-005"
    assert "definitely available" in result.question


async def test_record_topic_overlap_does_not_insert_an_identity_without_source_confirmed_context():
    question = "Are outside records definitely available?"
    provider = ClarifyingDouble("For PT-005, are outside records definitely available?", None)
    result = await resolve_text_intent(provider, question, ["What is the procedure overview?"], None, None)
    assert result.question == question and not patient_ids(result.question) and not provider.calls


@pytest.mark.parametrize("reference", ["case one million", "case seven point five", "case 7.5"])
def test_scope_helpers_do_not_raise_before_admission_validates_malformed_identity(reference):
    assert not record_topic_followup(f"Are outside records available for {reference}?", original_record("PT-005"))
    assert not general_lookup(f"What are the consultation prices for {reference}?")


@pytest.mark.parametrize("question", [
    "Can I use September 1 as the start date to calculate the revised interval?",
    "Does October 2 establish the event date for the corrected interval?",
    "Which event anchors the updated interval?",
    "Is the documented timing measured from the start date?",
])
def test_temporal_anaphora_requires_previous_lookup_without_implying_policy_topic(question):
    assert not self_contained(question)
    assert not general_lookup(question)


async def test_corrected_patient_interval_keeps_latest_record_without_manufacturing_date():
    history = [
        "What interval is noted for PT-001's follow-up?",
        "Correction: I need PT-008's tolerance reassessment interval instead.",
    ]
    question = "Can I use September 1 as the start date to calculate the revised interval?"
    provider = ClarifyingDouble()
    result = await resolve_text_intent(provider, question, history, "PT-008", "2026-09-10")
    assert result.clarification is None and not provider.calls
    assert patient_ids(result.question) == ["PT-008"]
    assert "tolerance reassessment interval" in result.question and question in result.question
    assert "PT-001" not in result.question and "2026" not in result.question
    assert resolved_detail_conflict(result.question, "\n".join([*history, question])) is None
    packet = RetrievalService(ingest(ROOT / "config/corpus.json")).evidence(
        result.question, mode="lexical", top_k=3,
    )
    assert {identifier for scope in packet.scopes for identifier in scope.patient_ids} == {"PT-008"}
    assert all("PT-001" not in scope.allowed_source_ids for scope in packet.scopes)
    assert any(source.doc_id == "PT-008" for source in packet.sources)


@pytest.mark.parametrize("patient,previous,question", [
    ("PT-004", "What follow-up interval is documented for PT-004?",
     "Can October 12 anchor the revised interval?"),
    ("PT-001", "What is PT-001's documented timing?",
     "Does the start date establish when to count from?"),
])
async def test_interval_topic_carry_uses_only_user_context_and_preserves_unverified_anchor(patient, previous, question):
    provider = ClarifyingDouble()
    result = await resolve_text_intent(provider, question, [previous], patient, "2028-01-01")
    assert not result.clarification and not provider.calls
    assert patient_ids(result.question) == [patient] and question in result.question
    assert "2028" not in result.question
    assert resolved_detail_conflict(result.question, previous + "\n" + question) is None


@pytest.mark.parametrize("question", [
    "New topic: what cancellation notice applies on September 1?",
    "What is the cancellation policy's revised interval?",
    "What are the general follow-up guidelines?",
])
async def test_explicit_general_topic_does_not_carry_previous_patient_interval(question):
    provider = ClarifyingDouble()
    result = await resolve_text_intent(provider, question, [
        "What tolerance reassessment interval is documented for PT-008?",
    ], None, None)
    assert not patient_ids(result.question)


@pytest.mark.parametrize("history,selected", [
    (["Compare the follow-up intervals for PT-001 and PT-008."], None),
    (["What is the consultation cost?"], "PT-008"),
    (["What is the interval for the patient named Robin?"], None),
    (["What is PT-001's follow-up interval?", "What is the new cancellation policy?"], "PT-001"),
])
async def test_interval_anaphora_does_not_guess_identity_or_revive_old_topic(history, selected):
    provider = ClarifyingDouble()
    result = await resolve_text_intent(provider, "Can September 1 anchor the revised interval?", history, selected, None)
    assert result.clarification and not result.question


async def test_explicit_other_patient_in_interval_question_cannot_be_replaced_by_history():
    provider = ClarifyingDouble("What is PT-008's interval?", None)
    result = await resolve_text_intent(provider, "For PT-004, can September 1 anchor the revised interval?", [
        "What interval is documented for PT-008?",
    ], "PT-008", None)
    assert result.clarification and not result.question


@pytest.mark.parametrize("old,correction,expected", [
    ("What amount is listed for an aesthetic consultation for a new patient?",
     "Switch that entry to a skincare consult instead.",
     "What amount is listed for the skincare consult?"),
    ("What limit is published for a premium annual membership for an enterprise team?",
     "Replace that item with a basic monthly package instead.",
     "What limit is published for the basic monthly package?"),
    ("What duration is listed for a fictional training premium workshop for an advanced group?",
     "Change this one to a beginner session.",
     "What duration is listed for the fictional training beginner session?"),
    ("What is the listed premium annual plan limit?",
     "Switch that item to a basic monthly plan instead.",
     "What is the listed basic monthly plan limit?"),
])
async def test_explicit_item_correction_replaces_the_whole_object_not_old_subtype(old, correction, expected):
    provider = ClarifyingDouble()
    result = await resolve_text_intent(provider, correction, [old], None, None)
    assert result.question == expected and result.clarification is None
    assert not provider.calls


@pytest.mark.parametrize("question", [
    "How much money must be put down to reserve that?",
    "What deposit is required for that one?",
    "What paperwork is required to request it?",
])
async def test_followup_folds_raw_user_correction_before_carrying_single_lookup_topic(question):
    provider = ClarifyingDouble()
    history = [
        "What amount is listed for an aesthetic consultation for a new patient?",
        "Switch that entry to a skincare consult instead.",
    ]
    result = await resolve_text_intent(provider, question, history, None, None)
    assert result.clarification is None and not provider.calls
    assert "skincare consult" in result.question
    assert result.question.endswith(question)
    assert "new patient" not in result.question and "aesthetic" not in result.question
    assert "unverified user question" in result.question
    assert history[-1] == "Switch that entry to a skincare consult instead."
    packet = RetrievalService(ingest(ROOT / "config/corpus.json")).evidence(result.question, mode="lexical", top_k=3)
    assert "BIZ-501" in packet.retrieval["raw_top_k_ids"]
    assert all(not scope.patient_ids for scope in packet.scopes)


@pytest.mark.parametrize("old,correction", [
    ("Compare the annual plan and monthly membership.", "Switch that item to a basic plan instead."),
    ("What is the limit for a plan or a membership?", "Replace that item with a basic package."),
    ("What is the follow-up for PT-007?", "Change that entry to a skincare consult instead."),
    ("What amount is listed for a consultation on September 1?", "Switch that entry to a skincare consult instead."),
])
async def test_item_correction_does_not_deterministically_replace_ambiguous_or_identity_date_lookup(old, correction):
    provider = ClarifyingDouble()
    result = await resolve_text_intent(provider, correction, [old], None, None)
    assert result.clarification and provider.calls


async def test_topic_switch_after_raw_item_correction_ignores_old_lookup():
    provider = ClarifyingDouble()
    question = "New topic: what are the office hours?"
    result = await resolve_text_intent(provider, question, [
        "What amount is listed for an aesthetic consultation for a new patient?",
        "Switch that entry to a skincare consult instead.",
    ], None, None)
    assert result.question == question and not provider.calls


@pytest.mark.parametrize("question", [
    "Use PT-007 rather than PT-006: summarize provider assessment status and relevant procedure history.",
    "Use PT-007 instead of PT-006: summarize the assessment and procedure history.",
    "Rather than PT-006, use PT-007: summarize the assessment and procedure history.",
    "PT-007, not PT-006: summarize the assessment and procedure history.",
    "Not PT-006, PT-007: summarize the assessment and procedure history.",
    "Summarize PT-007 and do not use PT-006.",
    "Use patient seven rather than patient six: summarize the assessment and procedure history.",
    "Use case seven instead of case six: summarize the assessment and procedure history.",
    "PT-006, sorry, PT-007: summarize the assessment and procedure history.",
])
def test_rejected_patient_is_removed_before_direct_retrieval_and_source_reference_permission(question):
    from emer.domain.scope import requested_patient_ids

    assert requested_patient_ids(question) == ["PT-007"]
    packet = RetrievalService(ingest(ROOT / "config/corpus.json")).evidence(question, mode="lexical", top_k=3)
    assert {s.doc_id for s in packet.sources if s.category == "synthetic_patient"} == {"PT-007"}
    assert all(scope.patient_ids == ["PT-007"] for scope in packet.scopes)
    assert all("PT-006" not in scope.allowed_source_ids + scope.source_reference_ids for scope in packet.scopes)


@pytest.mark.parametrize("question,expected", [
    ("Compare PT-006 and PT-007's assessment status.", {"PT-006", "PT-007"}),
    ("Show both PT-006 and PT-007, rather than only one record.", {"PT-006", "PT-007"}),
    ("Contrast PT-071 with PT-072 rather than guessing which one I mean.", {"PT-071", "PT-072"}),
    ("PT-071 has no history, PT-072 has a pending assessment.", {"PT-071", "PT-072"}),
    ("Summarize PT-071, not PT-072, and also summarize PT-072.", {"PT-071", "PT-072"}),
])
def test_patient_replacement_does_not_collapse_real_comparisons_or_negated_facts(question, expected):
    from emer.domain.scope import requested_patient_ids

    assert set(requested_patient_ids(question)) == expected


@pytest.mark.parametrize("question", [
    "Use PT-999 instead of PT-006: summarize the record.",
    "Rather than patient six, use patient nine hundred ninety nine: summarize the record.",
])
def test_unknown_positive_target_never_falls_back_to_known_rejected_patient(question):
    packet = RetrievalService(ingest(ROOT / "config/corpus.json")).evidence(question, mode="lexical", top_k=3)
    assert all(scope.patient_ids == [] and scope.unknown_patient_ids == ["PT-999"] for scope in packet.scopes)
    assert not any(s.category == "synthetic_patient" for s in packet.sources)


@pytest.mark.parametrize("question", [
    "Use PT-007 rather than patient six point five: summarize the assessment.",
    "Use PT-007 instead of case one million: summarize the assessment.",
])
def test_rejected_malformed_identifier_is_validated_before_scope_exclusion(question):
    from emer.domain.scope import ScopeError, requested_patient_ids

    with pytest.raises(ScopeError):
        requested_patient_ids(question)


def test_explicit_date_pairs_are_preserved_while_resolving_patient_comparison():
    from datetime import date

    from emer.domain.scope import resolve_scopes

    documents = ingest(ROOT / "config/corpus.json").documents
    scopes = resolve_scopes(
        "Compare PT-006 on March 1, 2026 and PT-007 on September 1, 2026.",
        documents, today=date(2026, 9, 12),
    )
    assert [(s.patient_ids, s.as_of) for s in scopes] == [
        (["PT-006"], "2026-03-01"), (["PT-007"], "2026-09-01"),
    ]


async def test_replacement_query_keeps_raw_user_text_but_filters_generation_packet():
    question = "Use PT-007 rather than PT-006: summarize the assessment and relevant history."
    provider = ClarifyingDouble()
    history = []
    intent = await resolve_text_intent(provider, question, history, None, None)
    assert intent.question == question and not provider.calls and history == []
    packet = RetrievalService(ingest(ROOT / "config/corpus.json")).evidence(intent.question, mode="lexical", top_k=3)
    assert {s.doc_id for s in packet.sources if s.category == "synthetic_patient"} == {"PT-007"}


def test_rejected_patient_cannot_be_cited_even_if_model_echoes_its_id():
    from emer.contracts.answer import ProviderDraft
    from emer.services.answering import AnswerValidationError, validate_draft

    bundle = ingest(ROOT / "config/corpus.json")
    packet = RetrievalService(bundle).evidence(
        "Use PT-007 rather than PT-006: summarize the assessment and relevant history.",
        mode="lexical", top_k=3,
    )
    rejected = next(s for s in bundle.documents if s.doc_id == "PT-006")
    draft = ProviderDraft.model_validate({
        "statements": [{
            "part_id": packet.scopes[0].part_id,
            "text": "PT-006 has no prior RF microneedling.",
            "citations": [{"doc_id": rejected.doc_id, "quote": rejected.text}],
        }], "gaps": [], "next_steps": [],
    })
    with pytest.raises(AnswerValidationError, match="crossed patient scope"):
        validate_draft(packet, draft)
