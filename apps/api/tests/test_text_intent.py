"""Intent boundary tests with provider doubles; no live-model quality claims."""

import asyncio

import pytest
from emer.contracts.answer import ProviderUsage
from emer.providers.openrouter import ProviderError, StructuredResult
from emer.services import text_intent
from emer.services.text_intent import ResolvedTextIntent, resolve_text_intent


class ProviderDouble:
    def __init__(self, question="", clarification=None, error=None):
        self.value = ResolvedTextIntent(question=question, clarification=clarification)
        self.error = error
        self.calls = []

    async def structured(self, instructions, payload, schema, **options):
        self.calls.append((instructions, payload, schema, options))
        if self.error:
            raise self.error
        return StructuredResult(
            value=self.value,
            usage=ProviderUsage(operation="text_intent_resolution", model="test-double", latency_ms=0),
        )


async def test_first_self_contained_question_is_verbatim_without_provider_call():
    provider = ProviderDouble()
    result = await resolve_text_intent(provider, "  What is PT-006's recorded history?  ", [], None, None)
    assert result == ResolvedTextIntent(question="What is PT-006's recorded history?", clarification=None)
    assert not provider.calls


async def test_correction_without_a_previous_task_requires_clarification():
    provider = ProviderDouble()
    result = await resolve_text_intent(provider, "No, PT005", [], None, None)
    assert not result.question and result.clarification
    assert not provider.calls


async def test_brief_correction_replaces_previous_patient_in_latest_task():
    provider = ProviderDouble("What is PT-005's recorded history and what remains uncertain?")
    result = await resolve_text_intent(
        provider, "No, PT005", ["What is PT-006's recorded history and what remains uncertain?"], None, None
    )
    assert result.question == provider.value.question
    assert result.clarification is None
    assert len(provider.calls) == 1
    _, payload, schema, options = provider.calls[0]
    assert payload["active_patient_ids"] == ["PT-005"]
    assert schema is ResolvedTextIntent
    assert options == {"operation": "text_intent_resolution", "max_tokens": 1600}


@pytest.mark.parametrize(
    "resolved",
    [
        "What is PT-006's recorded history?",
        "Compare PT-006 and PT-005's recorded history?",
        "What is their history?",
    ],
)
async def test_correction_cannot_keep_old_patient_mix_both_or_omit_new_identifier(resolved):
    result = await resolve_text_intent(
        ProviderDouble(resolved), "No, PT005", ["What is PT-006's recorded history?"], None, None
    )
    assert result.question == "" and result.clarification


async def test_followup_uses_latest_corrected_patient_not_earlier_patient():
    provider = ProviderDouble("What downtime information is supplied for PT-005?")
    result = await resolve_text_intent(
        provider,
        "What about their downtime?",
        ["Summarize PT-006's consultation.", "No, PT005"],
        None,
        None,
    )
    assert result.question == provider.value.question
    assert provider.calls[0][1]["active_patient_ids"] == ["PT-005"]


async def test_selected_patient_overrides_earlier_patient_for_pronouns():
    provider = ProviderDouble("What downtime information is supplied for PT-005?")
    result = await resolve_text_intent(
        provider, "What about their downtime?", ["Summarize PT-006's consultation."], "PT-005", None
    )
    assert result.clarification is None
    obsolete = await resolve_text_intent(
        ProviderDouble("What downtime information is supplied for PT-006?"),
        "What about their downtime?",
        ["Summarize PT-006's consultation."],
        "PT-005",
        None,
    )
    assert not obsolete.question and obsolete.clarification


async def test_explicit_current_patient_overrides_selected_patient():
    result = await resolve_text_intent(
        ProviderDouble("What is PT-005's recorded history?"),
        "No, PT005",
        ["What is PT-006's recorded history?"],
        "PT-006",
        None,
    )
    assert result.clarification is None


@pytest.mark.parametrize(
    "correction", ["PT006, sorry, PT005", "Use PT005 instead of PT006", "PT005, not PT006"]
)
async def test_explicit_replacement_syntax_does_not_preserve_rejected_identifier(correction):
    result = await resolve_text_intent(
        ProviderDouble("What is PT-005's recorded history?"),
        correction,
        ["What is PT-006's recorded history?"],
        None,
        None,
    )
    assert result.clarification is None


async def test_explicit_comparison_keeps_both_patients_and_historical_months():
    question = "Compare PT005 and PT006 under the March and September 2026 policies."
    result = await resolve_text_intent(ProviderDouble(question), question, ["Summarize PT001."], None, None)
    assert result.question == question and result.clarification is None


async def test_unknown_but_explicit_patient_is_not_substituted_with_a_known_patient():
    result = await resolve_text_intent(
        ProviderDouble("Summarize PT-009's history."), "Actually PT009", ["Summarize PT006."], None, None
    )
    assert result.clarification is None
    fabricated = await resolve_text_intent(
        ProviderDouble("Summarize PT-008's history."), "Actually PT009", ["Summarize PT006."], None, None
    )
    assert not fabricated.question and fabricated.clarification


@pytest.mark.parametrize("resolved", ["Is PT005 overdue by 14 days?", "What is PT005's January follow-up?"])
async def test_resolver_cannot_add_numbers_or_months_from_model_memory(resolved):
    result = await resolve_text_intent(
        ProviderDouble(resolved), "What about their follow-up?", ["Summarize PT005."], None, None
    )
    assert not result.question and result.clarification


async def test_selected_date_and_patient_normalization_are_permitted():
    result = await resolve_text_intent(
        ProviderDouble("What policy applies to PT-005 on 2026-09-11?"),
        "And the policy?",
        ["Summarize PT5."],
        "PT5",
        "2026-09-11",
    )
    assert result.clarification is None


async def test_model_uncertainty_returns_clarification_without_its_unchecked_assertions():
    result = await resolve_text_intent(
        ProviderDouble(
            "PT005 already received treatment.", "Since treatment is complete, do you mean PT005?"
        ),
        "What about that?",
        ["Summarize PT005."],
        None,
        None,
    )
    assert not result.question and result.clarification
    assert "treatment" not in result.clarification


async def test_history_is_bounded_and_contains_only_caller_supplied_questions():
    history = [f"Question {number}: " + "a" * 4000 for number in range(10)]
    provider = ProviderDouble("What is the practice cancellation policy?")
    await resolve_text_intent(provider, "And the practice cancellation policy?", history, None, None)
    payload = provider.calls[0][1]
    assert len(payload["previous_questions"]) <= text_intent.MAX_PREVIOUS_QUESTIONS
    assert sum(map(len, payload["previous_questions"])) <= text_intent.MAX_HISTORY_CHARACTERS
    assert payload["previous_questions"][-1] == history[-1]
    assert "transcript" not in payload and "answers" not in payload and "sources" not in payload


async def test_provider_failure_is_operational_error_not_a_clarification():
    failure = ProviderError("provider_rate_limited", "Provider unavailable.")
    provider = ProviderDouble(error=failure)
    with pytest.raises(ProviderError) as caught:
        await resolve_text_intent(provider, "What about their downtime?", ["Summarize PT005."], None, None)
    assert caught.value is failure
    assert len(provider.calls) == 1


async def test_timeout_is_bounded_without_automatic_retry(monkeypatch):
    class StalledProvider(ProviderDouble):
        async def structured(self, *args, **kwargs):
            self.calls.append((args, kwargs))
            await asyncio.sleep(10)

    monkeypatch.setattr(text_intent, "INTENT_TIMEOUT_SECONDS", 0.01)
    provider = StalledProvider()
    with pytest.raises(ProviderError) as caught:
        await resolve_text_intent(provider, "What about their downtime?", ["Summarize PT005."], None, None)
    assert caught.value.code == "provider_timeout"
    assert len(provider.calls) == 1


@pytest.mark.parametrize("question", ["What about their downtime?", "And their follow-up?", "Same question."])
async def test_orphan_followup_without_context_requires_clarification(question):
    provider = ProviderDouble()
    result = await resolve_text_intent(provider, question, [], None, None)
    assert not result.question and result.clarification
    assert not provider.calls


@pytest.mark.parametrize("history", [[], ["What is the current cancellation policy?"]])
@pytest.mark.parametrize(
    "question",
    [
        "What was the cancellation rule before July 1, 2026?",
        "What rule applied after June 30, 2026?",
        "Compare cancellation policies from March through September 2026.",
    ],
)
async def test_temporal_range_requests_clarify_before_any_provider_rewrite(history, question):
    # A model must not erase 'before' and turn the boundary itself into the lookup date.
    provider = ProviderDouble("What cancellation rule applied on July 1, 2026?")
    result = await resolve_text_intent(provider, question, history, None, "2026-07-01")
    assert not result.question and result.clarification
    assert "date" in result.clarification.lower()
    assert not provider.calls


async def test_rewritten_range_cannot_bypass_the_domain_date_boundary():
    provider = ProviderDouble("What rule applied before July 1, 2026?")
    result = await resolve_text_intent(
        provider,
        "And the cancellation rule?",
        ["What cancellation policy applied on July 1, 2026?"],
        None,
        None,
    )
    assert not result.question and result.clarification
    assert len(provider.calls) == 1


async def test_followup_can_supply_exact_dates_after_range_clarification():
    question = "Compare cancellation on June 30, 2026 and July 1, 2026."
    provider = ProviderDouble(question)
    result = await resolve_text_intent(
        provider, question, ["What was the cancellation rule before July 1, 2026?"], None, None
    )
    assert result.question == question and result.clarification is None


@pytest.mark.parametrize("question", [
    "What are the two listed consultation prices, and are they actual EMER Medical prices?",
    "Which are the permitted communication channels, and can they be used after an opt-out?",
])
async def test_local_nonpatient_antecedent_is_not_an_orphan_patient(question):
    provider = ProviderDouble()
    result = await resolve_text_intent(provider, question, [], None, None)
    assert result.question == question and result.clarification is None
    assert not provider.calls
