"""Structured conversational reference boundaries; doubles test contracts, not model quality."""

from copy import deepcopy
from hashlib import sha256
from types import SimpleNamespace

import pytest
from emer.contracts.answer import (
    AnswerGap,
    Citation,
    EvidencePacket,
    PublishedAnswer,
    PublishedStatement,
    QuestionScope,
    SourceDocument,
)
from emer.domain.chunking import count_tokens
from emer.domain.dialogue_state import ReferenceBinding, ReferenceResolution, build_dialogue_state
from emer.services.text_intent import resolve_text_intent


def saved_turn(run_id="run-1", *, question="Which procedure is described in the guidance?", text=None):
    source = SourceDocument(
        doc_id="GUIDE-77", title="Aster laser guidance", category="procedure", version="3",
        effective_date="2026-01-01", authority="Practice guidance",
        text=text or "Aster laser uses a short light pulse. The procedure lasts 30 minutes.", sha256="",
    )
    source.sha256 = sha256(source.text.encode()).hexdigest()
    scope = QuestionScope(part_id="part-1", question=question, patient_ids=[], unknown_patient_ids=[],
                          as_of="2026-09-01", date_origin="today", all_patients=False, warnings=[],
                          allowed_source_ids=[source.doc_id])
    packet = EvidencePacket(index_id="frozen-index", index_checksum="frozen-checksum", corpus_checksum="corpus",
                            sources=[source], scopes=[scope], retrieval={})
    citation = Citation(doc_id=source.doc_id, title=source.title, version=source.version,
                        effective_date=source.effective_date, authority=source.authority,
                        source_sha256=source.sha256, index_id=packet.index_id,
                        quote=source.text, start=0, end=len(source.text))
    answer = PublishedAnswer(status="answered", statements=[PublishedStatement(
        part_id=scope.part_id, text="The described procedure is Aster laser.", citations=[citation],
    )], gaps=[], next_steps=[], scopes=[scope], index_id=packet.index_id,
        index_checksum=packet.index_checksum, usage=[], diagnostics={})
    return SimpleNamespace(
        id=run_id, conversation_id="conversation-1", context_version=1, status="completed",
        question=question, context={"resolved_question": question},
        answer=answer.model_dump(mode="json"), evidence=packet.model_dump(mode="json"),
    )


class Resolver:
    def __init__(self, *, mention="that procedure", target="Aster laser", reference_id="run-1:statement:0",
                 status="resolved", base=None, topic_anchor=None):
        self.calls = []
        self.result = ReferenceResolution(
            status=status, base_reference_id=base, topic_anchor=topic_anchor,
            bindings=[ReferenceBinding(mention=mention, target=target, reference_id=reference_id)],
        )

    async def structured(self, instructions, payload, schema, **options):
        self.calls.append((instructions, payload, schema, options))
        assert schema is ReferenceResolution
        return SimpleNamespace(value=self.result)


async def test_assistant_only_referent_uses_exact_cited_original_and_preserves_new_task():
    state = build_dialogue_state([saved_turn()])
    resolver = Resolver()
    question = "If that procedure is appropriate, how long does it take and what remains uncertain?"
    result = await resolve_text_intent(resolver, question, [], None, None, dialogue_state=state)
    assert result.question == question.replace("that procedure", "Aster laser")
    assert result.reference_resolution == resolver.result
    payload = resolver.calls[0][1]
    reference = payload["dialogue_state"]["turns"][0]["assistant_references"][0]
    assert reference["citations"][0]["index_id"] == "frozen-index"
    assert reference["citations"][0]["source_sha256"] == state.turns[0].assistant_references[0].citations[0].source_sha256
    assert "sources" not in payload  # Navigation references do not become an EvidencePacket.


@pytest.mark.parametrize("mutation", ["quote", "hash", "bytes", "version", "index", "checksum", "scope", "uncited"])
def test_invalid_saved_provenance_cannot_become_reference_memory(mutation):
    row = saved_turn()
    citation = row.answer["statements"][0]["citations"][0]
    if mutation == "quote":
        citation["quote"] = "A valid-looking but fabricated source quotation."
    elif mutation == "hash":
        citation["source_sha256"] = "different"
    elif mutation == "bytes":
        row.evidence["sources"][0]["text"] += " altered"
    elif mutation == "version":
        citation["version"] = "wrong"
    elif mutation == "index":
        citation["index_id"] = "wrong"
    elif mutation == "checksum":
        row.answer["index_checksum"] = "wrong"
    elif mutation == "scope":
        row.evidence["scopes"][0]["allowed_source_ids"] = []
    else:
        row.answer["statements"][0]["citations"] = []
    state = build_dialogue_state([row])
    assert not state.turns[0].assistant_references
    assert list(state.references()) == ["run-1:user"]


async def test_assistant_paraphrase_cannot_supply_a_target_absent_from_its_source():
    row = saved_turn()
    row.answer["statements"][0]["text"] = "The described procedure is invented rejuvenation."
    state = build_dialogue_state([row])
    result = await resolve_text_intent(Resolver(target="invented rejuvenation"), "How long does that procedure take?",
                                       [], None, None, dialogue_state=state)
    assert result.clarification and not result.question


def test_unresolved_requests_are_separate_and_never_referenceable_facts():
    row = saved_turn()
    row.answer["gaps"] = [AnswerGap(part_id="part-1", text="The price was not found in that lookup.").model_dump()]
    state = build_dialogue_state([row])
    assert state.turns[0].unresolved_requests == ["The price was not found in that lookup."]
    assert all("price" not in reference.text for reference in state.references().values())


@pytest.mark.parametrize("current", ["New topic: What are the office hours?",
                                    "Leave that patient aside. What are the office hours?",
                                    "What are the office hours?"])
async def test_topic_reset_or_complete_new_question_never_calls_reference_model(current):
    state = build_dialogue_state([saved_turn()])
    resolver = Resolver(target="unrelated old subject")
    result = await resolve_text_intent(resolver, current, [], "PT-004", "2026-03-01", dialogue_state=state)
    assert result.question == "What are the office hours?" and not resolver.calls


async def test_reset_followed_by_orphan_reference_clarifies_without_old_memory():
    resolver = Resolver()
    result = await resolve_text_intent(resolver, "New topic: What about them?", [], "PT-004", None,
                                       dialogue_state=build_dialogue_state([saved_turn()]))
    assert result.clarification and not resolver.calls


async def test_explicit_correction_inherits_only_previous_user_task_and_replaces_entity():
    row = saved_turn(question="What is PT-004's documented status and what remains uncertain?")
    resolver = Resolver(mention="PT-004", target="PT-009", reference_id="$current", base="run-1:user")
    result = await resolve_text_intent(resolver, "No, PT009", [], "PT-004", None,
                                       dialogue_state=build_dialogue_state([row]))
    assert result.question == "What is PT-009's documented status and what remains uncertain?"
    assert result.clarification is None


async def test_correction_cannot_use_assistant_answer_as_task_template():
    result = await resolve_text_intent(
        Resolver(mention="Aster laser", target="another procedure", reference_id="$current", base="run-1:statement:0"),
        "I meant another procedure", [], None, None, dialogue_state=build_dialogue_state([saved_turn()]),
    )
    assert result.clarification


async def test_ambiguous_reference_clarifies_without_model_generated_explanation():
    resolver = Resolver(status="ambiguous")
    result = await resolve_text_intent(resolver, "What about that procedure?", [], None, None,
                                       dialogue_state=build_dialogue_state([saved_turn()]))
    assert result.clarification and "Aster" not in result.clarification
    assert not result.question


async def test_archived_topic_cannot_silently_become_active_after_reset():
    second = saved_turn("run-2", question="New topic: What are the office hours?")
    second.answer["statements"] = []
    state = build_dialogue_state([saved_turn(), second])
    assert [turn.topic_epoch for turn in state.turns] == [0, 1]
    result = await resolve_text_intent(Resolver(), "How long does that procedure take?", [], None, None,
                                       dialogue_state=state)
    assert result.clarification


async def test_explicit_named_return_can_resolve_an_archived_assistant_topic():
    second = saved_turn("run-2", question="New topic: What are the office hours?")
    second.answer["statements"] = []
    state = build_dialogue_state([saved_turn(), second])
    question = "Returning to Aster laser, how long does that procedure take?"
    result = await resolve_text_intent(Resolver(topic_anchor="Aster laser"), question, [], None, None,
                                       dialogue_state=state)
    assert result.question == question.replace("that procedure", "Aster laser")
    assert result.clarification is None


@pytest.mark.parametrize("field,value", [("conversation_id", "other-conversation"), ("context_version", 2)])
def test_snapshot_rejects_mixed_conversation_or_context_version(field, value):
    other = saved_turn("run-2")
    setattr(other, field, value)
    with pytest.raises(ValueError, match="cannot mix"):
        build_dialogue_state([saved_turn(), other])


def test_snapshot_omits_incomplete_clarification_and_mutable_input_aliases():
    completed = saved_turn()
    clarification = saved_turn("clarifying")
    clarification.answer["status"] = "clarification"
    running = saved_turn("running")
    running.status = "running"
    state = build_dialogue_state([completed, clarification, running])
    before = deepcopy(state.model_dump())
    completed.answer["statements"][0]["text"] = "modified externally"
    assert state.model_dump() == before
    assert [turn.run_id for turn in state.turns] == ["run-1"]


def test_snapshot_respects_serialized_token_and_turn_budgets_with_whole_units():
    rows = [saved_turn(f"run-{index}", text="Precise source passage. " * 100) for index in range(12)]
    state = build_dialogue_state(rows, max_turns=4, max_tokens=1000)
    assert count_tokens(state.model_dump_json()) <= 1000
    assert state.truncated and 1 <= len(state.turns) <= 4
    assert state.turns[-1].run_id == "run-11"
    for turn in state.turns:
        for reference in turn.assistant_references:
            assert reference.text == rows[-1].answer["statements"][0]["text"]
            assert reference.citations[0].quote == rows[-1].evidence["sources"][0]["text"]


async def test_cited_numeric_referent_is_allowed_but_invented_numeric_target_is_not():
    state = build_dialogue_state([saved_turn(text="The revision takes effect on 2026-10-01.")])
    question = "What is the cancellation policy on that date?"
    accepted = await resolve_text_intent(Resolver(mention="that date", target="2026-10-01"), question,
                                         [], None, None, dialogue_state=state)
    assert accepted.question == question.replace("that date", "2026-10-01")
    rejected = await resolve_text_intent(Resolver(mention="that date", target="2026-11-30"), question,
                                         [], None, None, dialogue_state=state)
    assert rejected.clarification and not rejected.question


async def test_replacement_must_have_one_exact_nonoverlapping_mention():
    state = build_dialogue_state([saved_turn()])
    question = "Does that procedure differ from that procedure?"
    result = await resolve_text_intent(Resolver(), question, [], None, None, dialogue_state=state)
    assert result.clarification
    resolver = Resolver()
    resolver.result.bindings.append(ReferenceBinding(mention="procedure", target="Aster laser",
                                                       reference_id="run-1:statement:0"))
    result = await resolve_text_intent(resolver, "What does that procedure involve?", [], None, None,
                                       dialogue_state=state)
    assert result.clarification


async def test_correction_cannot_resurrect_an_older_task_template():
    state = build_dialogue_state([
        saved_turn(question="What is PT-004's status?"),
        saved_turn("run-2", question="What is PT-004's procedure pricing?"),
    ])
    result = await resolve_text_intent(
        Resolver(mention="PT-004", target="PT-009", reference_id="$current", base="run-1:user"),
        "No, PT009", [], None, None, dialogue_state=state,
    )
    assert result.clarification and not result.question


async def test_current_explicit_numeric_detail_cannot_be_replaced_by_a_memory_number():
    state = build_dialogue_state([saved_turn()])
    result = await resolve_text_intent(
        Resolver(mention="500", target="30"), "Does that procedure last 500 minutes?", [], None, None,
        dialogue_state=state,
    )
    assert result.clarification and not result.question


@pytest.mark.parametrize('question', ['Have they decided to proceed?', 'Did she request pricing?', 'Can he proceed yet?'])
async def test_immediate_single_patient_subject_does_not_depend_on_model(question):
    state = build_dialogue_state([saved_turn(question='What has PT-417 completed?')])
    resolver = Resolver(status='ambiguous')
    result = await resolve_text_intent(resolver, question, [], None, None, dialogue_state=state)
    assert result.clarification is None
    assert 'PT-417' in result.question
    assert result.reference_resolution.bindings[0].reference_id == 'run-1:user'
    assert not resolver.calls


@pytest.mark.parametrize('previous', [
    'Compare PT-417 and PT-418.',
    'What did the provider tell PT-417?',
    'Did PT-417 bring their mother?',
    'What is the cancellation policy?',
])
async def test_patient_subject_shortcut_does_not_guess_competing_or_missing_identity(previous):
    state = build_dialogue_state([saved_turn(question=previous)])
    resolver = Resolver(status='ambiguous')
    result = await resolve_text_intent(resolver, 'Have they decided to proceed?', [], None, None, dialogue_state=state)
    assert result.clarification
    assert resolver.calls
