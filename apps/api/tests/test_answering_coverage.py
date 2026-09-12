"""Publication mechanics with invented test sources and declared semantic verdicts.

These provider doubles test repair, filtering and status boundaries, not live-model accuracy.
"""

from copy import deepcopy
from hashlib import sha256

import pytest
from emer.contracts.answer import EvidencePacket, ProviderUsage, QuestionScope, SourceDocument
from emer.providers.openrouter import ProviderError, StructuredResult
from emer.services.answering import ANSWER_PROMPT_ID, CHECKER_PROMPT_ID, PROMPTS, AnsweringService


@pytest.fixture
def packet():
    text = (
        "Calibration is complete. Packaging inventory is unfinished. "
        "Safety clearance is required before release. Post-release observation duration varies. "
        "No shipping quote or delivery date is recorded."
    )
    source = SourceDocument(
        doc_id="TEST-RELEASE",
        title="Invented release fixture",
        category="operations",
        version="1",
        effective_date="2026-01-01",
        authority="Test fixture only",
        text=text,
        sha256=sha256(text.encode()).hexdigest(),
    )
    scope = QuestionScope(
        part_id="part-1",
        question="Fixture request",
        patient_ids=[],
        unknown_patient_ids=[],
        as_of="2026-01-01",
        date_origin="question",
        all_patients=False,
        warnings=[],
        allowed_source_ids=[source.doc_id],
    )
    return EvidencePacket(
        index_id="invented-fixture-index",
        index_checksum="test-index-checksum",
        corpus_checksum="test-corpus-checksum",
        sources=[source],
        scopes=[scope],
        retrieval={"mode": "full"},
    )


def item(packet, text):
    source = packet.sources[0]
    return {
        "part_id": "part-1",
        "text": text,
        "citations": [{"doc_id": source.doc_id, "quote": source.text}],
    }


def draft(statements, *, gaps=(), next_steps=()):
    return {"statements": statements, "gaps": list(gaps), "next_steps": list(next_steps)}


def check(value, status, reason, rejected=None):
    rejected = rejected or {}
    return {
        "verdicts": [
            {
                "kind": kind,
                "index": index,
                "supported": (kind, index) not in rejected,
                "reason": rejected.get((kind, index), "Supported by the invented fixture source."),
            }
            for kind, field in [("statement", "statements"), ("gap", "gaps"), ("next_step", "next_steps")]
            for index, _ in enumerate(value[field])
        ],
        "coverage": [{"part_id": "part-1", "status": status, "reason": reason}],
    }


class ProviderDouble:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.calls = []
        self.last_review = None

    async def structured(self, system, payload, schema, operation, **kwargs):
        self.calls.append((operation, system, deepcopy(payload)))
        response = next(self.replies)
        if isinstance(response, Exception):
            raise response
        if schema.__name__ == "ProviderRepair" and "statement_replacements" not in response:
            # Older fixture narratives specify the desired whole draft. Translate only the
            # explicitly rejected units plus appended facts into the actual patch contract.
            previous = payload["repair"]["draft"]
            assert self.last_review is not None
            rejected = {(v["kind"], v["index"]) for v in self.last_review["verdicts"] if not v["supported"]}
            patch = {"additions": {}}
            for kind, field in [("statement", "statements"), ("gap", "gaps"), ("next_step", "next_steps")]:
                for index, value in enumerate(previous[field]):
                    if (kind, index) not in rejected:
                        assert response[field][index] == value, (
                            "Legacy fixture attempted to rewrite accepted content"
                        )
                patch[kind + "_replacements"] = [
                    {
                        "index": index,
                        "replacement": response[field][index] if index < len(response[field]) else None,
                    }
                    for rejected_kind, index in sorted(rejected)
                    if rejected_kind == kind
                ]
                patch["additions"][field] = response[field][len(previous[field]) :]
            response = patch
        if operation == "support_check":
            # Canned reviews describe the combined answer; the live audit now receives
            # only changed/new items, while unchanged verified units retain their verdict.
            verified = {(v["kind"], v["index"]) for v in payload.get("previously_verified_items", [])}
            if verified:
                response = deepcopy(response)
                response["verdicts"] = [
                    v for v in response["verdicts"] if (v["kind"], v["index"]) not in verified
                ]
            response = deepcopy(response)
            for verdict in response["verdicts"]:
                if verdict["supported"] and verdict["kind"] != "gap":
                    reviewed = next(
                        (
                            item
                            for item in payload["items"]
                            if (item["kind"], item["index"]) == (verdict["kind"], verdict["index"])
                        ),
                        None,
                    )
                    if reviewed is not None:
                        verdict.setdefault("citation_replacements", deepcopy(reviewed["citations"]))
            self.last_review = deepcopy(response)
        return StructuredResult(
            value=schema.model_validate(response),
            usage=ProviderUsage(operation=operation, model="declared-test-double", latency_ms=1),
        )


async def test_supported_but_incomplete_requested_content_gets_one_repair(packet):
    initial = draft([item(packet, "Calibration is complete.")])
    complete = draft([*initial["statements"], item(packet, "Post-release observation duration varies.")])
    provider = ProviderDouble(
        [
            initial,
            check(
                initial, "partial", "The requested observation expectation is omitted despite being recorded."
            ),
            complete,
            check(
                complete,
                "answered",
                "Both the requested current state and observation expectation are present.",
            ),
        ]
    )
    answer = await AnsweringService(provider).answer(
        packet, "What is the calibration state and observation expectation?"
    )
    assert answer.status == "answered" and len(answer.statements) == 2
    assert answer.statements[0].text == initial["statements"][0]["text"]
    assert not answer.gaps and not answer.next_steps
    assert [call[0] for call in provider.calls] == ["generation", "support_check", "repair", "support_check"]
    feedback = provider.calls[2][2]["repair"]["problems"]
    assert any("observation expectation is omitted" in problem for problem in feedback)
    assert provider.calls[0][1] == (PROMPTS / f"{ANSWER_PROMPT_ID}.txt").read_text()
    assert provider.calls[1][1] == (PROMPTS / f"{CHECKER_PROMPT_ID}.txt").read_text()
    assert answer.diagnostics["repair_count"] == 1
    assert answer.diagnostics["withheld_count"] == 0
    assert len(answer.usage) == 4


@pytest.mark.parametrize(
    "malformed,reason",
    [
        (
            "Release is documented as preceding only after safety clearance is obtained.",
            "The sentence attaches opposing temporal directions to release and omits consideration.",
        ),
        (
            "Release occurs only after safety clearance is obtained.",
            "A prerequisite for considering release does not establish that release occurs.",
        ),
        (
            "Safety clearance is obtained only after release is considered.",
            "The answer reverses the source relation: clearance precedes consideration of release.",
        ),
    ],
)
async def test_rejected_conditional_or_temporal_clause_receives_one_scoped_repair(packet, malformed, reason):
    """Replay declared semantic failures; actual checker quality requires provider retesting."""
    text = "Calibration is complete. Release should be considered only after safety clearance is obtained."
    source = packet.sources[0].model_copy(update={"text": text, "sha256": sha256(text.encode()).hexdigest()})
    packet = packet.model_copy(update={"sources": [source]})
    approved = item(packet, "Calibration is complete.")
    initial = draft([approved, item(packet, malformed)])
    corrected = "Release should be considered only after safety clearance is obtained."
    repaired = draft([approved, item(packet, corrected)])
    provider = ProviderDouble(
        [
            initial,
            check(
                initial,
                "partial",
                "The requested condition is not faithfully stated.",
                rejected={("statement", 1): reason},
            ),
            repaired,
            check(
                repaired,
                "answered",
                "The saved state and source-qualified consideration condition are present.",
            ),
        ]
    )
    answer = await AnsweringService(provider).answer(
        packet, "What is complete and what must come before release?"
    )
    assert answer.status == "answered" and [statement.text for statement in answer.statements] == [
        approved["text"],
        corrected,
    ]
    assert [call[0] for call in provider.calls] == ["generation", "support_check", "repair", "support_check"]
    feedback = provider.calls[2][2]["repair"]
    assert feedback["rejected_indices"] == {"statements": [1], "gaps": [], "next_steps": []}
    assert any(reason in problem for problem in feedback["problems"])
    assert malformed not in " ".join(statement.text for statement in answer.statements)
    assert answer.diagnostics["repair_count"] == 1
    assert answer.diagnostics["prompt_id"] == ANSWER_PROMPT_ID
    assert answer.diagnostics["checker_prompt_id"] == CHECKER_PROMPT_ID


@pytest.mark.parametrize("kind", ["statement", "gap", "next_step"])
@pytest.mark.parametrize("supported", [True, False])
async def test_nonexistent_verdict_cannot_change_real_audited_content(packet, kind, supported):
    value = draft([item(packet, "Calibration is complete.")])
    review = check(value, "answered", "Requested state is present.")
    phantom = {"kind": kind, "index": 73, "supported": supported, "reason": "No such item."}
    review["verdicts"].append(phantom)
    provider = ProviderDouble([value, review])
    answer = await AnsweringService(provider).answer(packet, "What is the calibration state?")
    assert answer.status == "answered" and len(answer.statements) == 1
    assert answer.statements[0].text == "Calibration is complete."
    assert len(provider.calls) == 2 and answer.diagnostics["repair_count"] == 0
    assert len(answer.diagnostics["support_verdicts"]) == 1
    ignored = answer.diagnostics["ignored_nonexistent_support_verdicts"]
    assert len(ignored) == 1 and ignored[0]["kind"] == kind and ignored[0]["index"] == 73


@pytest.mark.parametrize("malformation", ["missing", "conflicting_duplicate", "wrong_kind"])
async def test_real_verdict_inventory_remains_exact_after_phantom_filter(packet, malformation):
    value = draft([item(packet, "Calibration is complete.")])
    review = check(value, "answered", "Requested state is present.")
    if malformation == "missing":
        review["verdicts"] = []
    elif malformation == "conflicting_duplicate":
        other = deepcopy(review["verdicts"][0])
        other["supported"] = False
        review["verdicts"].append(other)
    else:
        review["verdicts"][0]["kind"] = "next_step"
    review["verdicts"].append(
        {"kind": "statement", "index": 73, "supported": True, "reason": "No such item."}
    )
    with pytest.raises(ProviderError, match="every response item exactly once"):
        await AnsweringService(ProviderDouble([value, review])).answer(packet, "Fixture request")


async def test_identical_duplicate_verdict_is_one_audit_not_an_extra_vote(packet):
    value = draft([item(packet, "Calibration is complete.")])
    review = check(value, "answered", "Requested state is present.")
    review["verdicts"].append(deepcopy(review["verdicts"][0]))
    answer = await AnsweringService(ProviderDouble([value, review])).answer(packet, "Fixture request")
    assert answer.status == "answered"
    assert len(answer.diagnostics["support_verdicts"]) == 1
    assert len(answer.diagnostics["ignored_duplicate_support_verdicts"]) == 1


@pytest.mark.parametrize("invalid", ["wrong_source", "fake_quote", "wrong_patient", "wrong_part"])
async def test_invalid_typed_repair_preserves_locked_content_without_expanding_permissions(packet, invalid):
    initial = draft([item(packet, "Calibration is complete."), item(packet, "Release is authorized.")])
    replacement = item(packet, "Safety clearance is required before release.")
    if invalid == "wrong_source":
        replacement["citations"][0]["doc_id"] = "TEST-NOT-PERMITTED"
    elif invalid == "fake_quote":
        replacement["citations"][0]["quote"] = "This sentence is not in the original record."
    elif invalid == "wrong_patient":
        replacement["text"] = "PT-777 has clearance."
    else:
        replacement["part_id"] = "part-that-does-not-exist"
    patch = {
        "statement_replacements": [{"index": 1, "replacement": replacement}],
        "gap_replacements": [],
        "next_step_replacements": [],
        "additions": draft([]),
    }
    surviving = draft([initial["statements"][0]])
    provider = ProviderDouble(
        [
            initial,
            check(initial, "partial", "Clearance needs correction.", {("statement", 1): "Not authorized."}),
            patch,
            check(surviving, "answered", "The remaining state is supported."),
        ]
    )
    answer = await AnsweringService(provider).answer(packet, "Fixture request")
    assert answer.status == "partial" and len(answer.statements) == 1
    assert answer.statements[0].text == initial["statements"][0]["text"]
    assert len(provider.calls) == 4 and not answer.gaps
    assert answer.diagnostics["withheld_count"] == 1
    assert len(answer.diagnostics["withheld_invalid_repair_units"]) == 1
    assert answer.diagnostics["reused_immutable_support_verdicts"] == 1
    assert not provider.calls[-1][2]["items"]


async def test_incomplete_repair_stays_partial_without_inventing_a_gap_or_more_calls(packet):
    value = draft([item(packet, "Calibration is complete.")])
    verdict = check(value, "partial", "The requested observation expectation remains missing from prose.")
    provider = ProviderDouble([value, verdict, value, verdict])
    answer = await AnsweringService(provider).answer(
        packet, "What is the calibration state and observation expectation?"
    )
    assert answer.status == "partial" and len(provider.calls) == 4
    assert not answer.gaps and answer.statements[0].text == value["statements"][0]["text"]


async def test_wholly_unavailable_values_are_unsupported_despite_true_related_context(packet):
    value = draft(
        [item(packet, "Safety clearance is required before release.")],
        gaps=[{"part_id": "part-1", "text": "The shipping quote and delivery date are not recorded."}],
    )
    provider = ProviderDouble(
        [
            value,
            check(
                value,
                "unsupported",
                "Neither requested value is available; the clearance rule is related context.",
            ),
        ]
    )
    answer = await AnsweringService(provider).answer(packet, "Give the shipping quote and delivery date.")
    assert answer.status == "unsupported" and answer.statements and answer.gaps
    assert len(provider.calls) == 2 and answer.diagnostics["repair_count"] == 0


async def test_real_requested_gap_keeps_compound_answer_partial_without_retry(packet):
    value = draft(
        [item(packet, "Safety clearance is required before release.")],
        gaps=[{"part_id": "part-1", "text": "The shipping quote is not recorded."}],
    )
    provider = ProviderDouble(
        [
            value,
            check(
                value, "partial", "The requested prerequisite is answered, but the requested quote is absent."
            ),
        ]
    )
    answer = await AnsweringService(provider).answer(
        packet, "Give the release prerequisite and shipping quote."
    )
    assert answer.status == "partial" and answer.statements and answer.gaps
    assert len(provider.calls) == 2 and answer.diagnostics["repair_count"] == 0


async def test_extra_unasked_gap_does_not_make_a_complete_answer_partial(packet):
    value = draft(
        [item(packet, "Safety clearance is required before release.")],
        gaps=[{"part_id": "part-1", "text": "The shipping quote is not recorded."}],
    )
    verdict = check(
        value,
        "answered",
        "The requested prerequisite is fully stated.",
        {("gap", 0): "The user did not ask for a shipping quote; this gap expands the request."},
    )
    provider = ProviderDouble([value, verdict, value, verdict])
    answer = await AnsweringService(provider).answer(packet, "What is required before release?")
    assert answer.status == "answered" and len(answer.statements) == 1 and not answer.gaps
    assert len(provider.calls) == 4 and answer.diagnostics["withheld_count"] == 1


async def test_two_supported_facts_do_not_authorize_an_added_prerequisite(packet):
    value = draft(
        [item(packet, "Safety clearance is required before release; packaging inventory is unfinished.")],
        next_steps=[
            item(packet, "Do not release without safety clearance."),
            item(packet, "Review whether packaging inventory is complete before release."),
        ],
    )
    verdict = check(
        value,
        "answered",
        "The actual release prerequisite is stated.",
        {
            (
                "next_step",
                1,
            ): "The source requires clearance before release, not a packaging-completion review before release."
        },
    )
    provider = ProviderDouble([value, verdict, value, verdict])
    answer = await AnsweringService(provider).answer(packet, "What is required before release?")
    assert answer.status == "answered" and len(answer.next_steps) == 1
    assert answer.next_steps[0].text == "Do not release without safety clearance."
    assert answer.statements[0].text == value["statements"][0]["text"]
    assert answer.diagnostics["withheld_count"] == 1 and len(provider.calls) == 4
    assert answer.diagnostics["support_verdicts"][-1]["supported"] is False


async def test_failed_coverage_repair_preserves_completed_and_failed_attempt_receipts(packet):
    value = draft([item(packet, "Calibration is complete.")])
    error = ProviderError(
        "provider_rate_limited",
        "Declared failure.",
        usage=ProviderUsage(operation="repair", model="declared-test-double", latency_ms=1),
    )
    provider = ProviderDouble(
        [
            value,
            check(value, "partial", "The requested observation expectation is missing."),
            error,
        ]
    )
    with pytest.raises(ProviderError) as failed:
        await AnsweringService(provider).answer(
            packet, "What is the calibration state and observation expectation?"
        )
    assert failed.value is error
    assert [receipt["operation"] for receipt in error.attempt_usage] == [
        "generation",
        "support_check",
        "repair",
    ]
    assert len(provider.calls) == 3


async def test_missing_supported_fact_is_repaired_even_with_a_real_requested_gap(packet):
    initial = draft(
        [item(packet, "Calibration is complete.")],
        gaps=[{"part_id": "part-1", "text": "The shipping quote is not recorded."}],
    )
    review = check(initial, "partial", "The quote is absent and the recorded inventory status is omitted.")
    review["coverage"][0]["missing_supported_facts"] = ["Packaging inventory is unfinished."]
    repaired = draft(
        [*initial["statements"], item(packet, "Packaging inventory is unfinished.")], gaps=initial["gaps"]
    )
    provider = ProviderDouble(
        [
            initial,
            review,
            repaired,
            check(repaired, "partial", "Both recorded statuses are supplied; quote is absent."),
        ]
    )
    answer = await AnsweringService(provider).answer(
        packet, "Give calibration status, inventory status, and the shipping quote."
    )
    assert answer.status == "partial"
    assert len(answer.statements) == 2 and len(answer.gaps) == 1
    assert answer.diagnostics["repair_count"] == 1
    assert len(provider.calls) == 4


async def test_remaining_supported_omission_cannot_publish_answered(packet):
    value = draft([item(packet, "Calibration is complete.")])
    inconsistent = check(value, "answered", "Calibration answered but inventory status omitted.")
    inconsistent["coverage"][0]["missing_supported_facts"] = ["Packaging inventory is unfinished."]
    provider = ProviderDouble([value, inconsistent, value, inconsistent])
    answer = await AnsweringService(provider).answer(packet, "Give calibration and inventory status.")
    assert answer.status == "partial"
    assert answer.diagnostics["repair_count"] == 1
    assert len(provider.calls) == 4


async def test_source_backed_ambiguity_publishes_clarification(packet):
    value = draft([], gaps=[{"part_id": "part-1", "text": "Which release do you mean?"}])
    provider = ProviderDouble(
        [value, check(value, "clarification", "The requested release identity is ambiguous.")]
    )
    answer = await AnsweringService(provider).answer(packet, "What is the state of that release?")
    assert answer.status == "clarification"
    assert len(provider.calls) == 2


async def test_compound_request_accounting_does_not_erase_answered_portion(packet):
    value = draft(
        [item(packet, "Calibration is complete.")],
        gaps=[{"part_id": "part-1", "text": "The shipping quote is not recorded."}],
    )
    review = check(value, "unsupported", "Status is established; the quote is unavailable.")
    review["coverage"][0].update(
        answered_requests=["Calibration status"], unanswered_requests=["Shipping quote"]
    )
    provider = ProviderDouble([value, review])
    answer = await AnsweringService(provider).answer(packet, "Give calibration status and shipping quote.")
    assert answer.status == "partial"
    assert len(provider.calls) == 2


async def test_missing_only_requested_value_stays_unsupported_despite_related_context(packet):
    value = draft(
        [item(packet, "Calibration is complete.")],
        gaps=[{"part_id": "part-1", "text": "The shipping quote is not recorded."}],
    )
    review = check(value, "partial", "Only related calibration context is available.")
    review["coverage"][0].update(answered_requests=[], unanswered_requests=["Shipping quote"])
    answer = await AnsweringService(ProviderDouble([value, review])).answer(
        packet, "What is the shipping quote?"
    )
    assert answer.status == "unsupported"


def test_model_context_partitions_dates_and_omits_debug_rankings(packet):
    from emer.services.answering import model_context

    second = packet.scopes[0].model_copy(update={"part_id": "part-2", "as_of": "2026-07-01"})
    packet = packet.model_copy(update={"scopes": [packet.scopes[0], second]})
    context = model_context(packet, "Compare the two periods.")
    assert "retrieval" not in context["evidence"]
    assert context["evidence"]["sources"] == [d.model_dump(mode="json") for d in packet.sources]
    assert [p["assigned_date"] for p in context["answer_partition"]["parts"]] == ["2026-01-01", "2026-07-01"]
    assert packet.retrieval == {"mode": "full"}


@pytest.mark.parametrize(
    "absence",
    [
        "The supplied record does not document a communication preference.",
        "The record documents completion, but it does not name a task owner.",
        "The requested flag is not present in the supplied record.",
    ],
)
def test_record_bounded_absence_displays_entire_same_record(packet, absence):
    from emer.contracts.answer import ProviderDraft
    from emer.services.answering import validate_draft

    source = packet.sources[0].model_copy(update={"category": "synthetic_patient"})
    packet = packet.model_copy(update={"sources": [source]})
    value = draft(
        [
            {
                "part_id": "part-1",
                "text": absence,
                "citations": [{"doc_id": source.doc_id, "quote": "Calibration is complete."}],
            }
        ]
    )
    published, _ = validate_draft(packet, ProviderDraft.model_validate(value))
    citation = published[0].citations[0]
    assert (citation.start, citation.end, citation.quote) == (0, len(source.text), source.text)
    assert citation.source_sha256 == source.sha256
    assert citation.index_id == packet.index_id


def test_record_citation_expansion_does_not_replace_invalid_quotes_or_clinical_negation(packet):
    from emer.contracts.answer import ProviderDraft
    from emer.services.answering import AnswerValidationError, validate_draft

    source = packet.sources[0].model_copy(update={"category": "synthetic_patient"})
    packet = packet.model_copy(update={"sources": [source]})
    value = draft(
        [
            {
                "part_id": "part-1",
                "text": "Calibration is complete; packaging is unfinished.",
                "citations": [{"doc_id": source.doc_id, "quote": "Calibration is complete."}],
            }
        ]
    )
    provider_draft = ProviderDraft.model_validate(value)
    original = provider_draft.model_dump()
    published, _ = validate_draft(packet, provider_draft)
    assert provider_draft.model_dump() == original
    assert published[0].text == value["statements"][0]["text"]
    citation = published[0].citations[0]
    assert (citation.start, citation.end, citation.quote) == (0, len(source.text), source.text)
    assert "Calibration is complete." in citation.quote
    assert citation.source_sha256 == source.sha256 and citation.index_id == packet.index_id
    value["statements"][0].update(text="The record does not document a task owner.")
    value["statements"][0]["citations"][0]["quote"] = "Invented missing quotation."
    with pytest.raises(AnswerValidationError):
        validate_draft(packet, ProviderDraft.model_validate(value))


def patch(*, statements=(), gaps=(), next_steps=(), additions=None):
    return {
        "statement_replacements": list(statements),
        "gap_replacements": list(gaps),
        "next_step_replacements": list(next_steps),
        "additions": additions if additions is not None else draft([]),
    }


async def test_targeted_repair_preserves_approved_source_qualified_prerequisite(packet):
    qualified = "Release should be considered only after safety clearance."
    text = qualified + " Packaging inventory is unfinished."
    source = packet.sources[0].model_copy(update={"text": text, "sha256": sha256(text.encode()).hexdigest()})
    packet = packet.model_copy(update={"sources": [source]})
    approved = item(packet, qualified)
    rejected = item(packet, "Packaging inventory is complete.")
    correction = item(packet, "Packaging inventory is unfinished.")
    initial = draft([approved, rejected])
    repaired = draft([approved, correction])
    provider = ProviderDouble(
        [
            initial,
            check(
                initial,
                "partial",
                "Inventory wording is wrong.",
                {("statement", 1): "Inventory is unfinished."},
            ),
            patch(statements=[{"index": 1, "replacement": correction}]),
            check(
                repaired, "answered", "The source-qualified prerequisite and inventory state are now correct."
            ),
        ]
    )
    answer = await AnsweringService(provider).answer(
        packet, "What is the release prerequisite and inventory state?"
    )
    assert answer.status == "answered"
    assert answer.statements[0].text == qualified
    assert "must" not in answer.statements[0].text
    assert provider.calls[2][2]["repair"]["rejected_indices"] == {
        "statements": [1],
        "gaps": [],
        "next_steps": [],
    }
    assert provider.calls[3][2]["previously_verified_items"][0]["text"] == qualified
    assert answer.statements[1].text == correction["text"]


@pytest.mark.parametrize("field", ["statements", "gaps", "next_steps"])
def test_repair_cannot_replace_approved_units_in_any_output_kind(packet, field):
    from emer.contracts.answer import ProviderDraft, ProviderRepair
    from emer.services.answering import apply_repair

    base = draft(
        [item(packet, "Calibration is complete.")],
        gaps=[{"part_id": "part-1", "text": "Shipping quote is absent."}],
        next_steps=[item(packet, "Obtain safety clearance before release.")],
    )
    change = {"index": 0, "replacement": None}
    reply = patch(**{field: [change]})
    ignored = []
    result = apply_repair(
        ProviderDraft.model_validate(base),
        ProviderRepair.model_validate(reply),
        {"statements": set(), "gaps": set(), "next_steps": set()},
        ignored,
    )
    assert result.model_dump() == base
    assert ignored == [{"kind": field, "index": 0}]
    assert all(len(base[kind]) == 1 for kind in ["statements", "gaps", "next_steps"])


@pytest.mark.parametrize("indices", [[99], [0, 0]])
def test_repair_rejects_unknown_or_duplicate_indices(packet, indices):
    from emer.contracts.answer import ProviderDraft, ProviderRepair
    from emer.services.answering import AnswerValidationError, apply_repair

    base = ProviderDraft.model_validate(draft([item(packet, "Packaging is complete.")]))
    reply = ProviderRepair.model_validate(
        patch(statements=[{"index": i, "replacement": None} for i in indices])
    )
    with pytest.raises(AnswerValidationError, match="duplicate or unknown"):
        apply_repair(base, reply, {"statements": {0}, "gaps": set(), "next_steps": set()})


@pytest.mark.parametrize("explicit_null", [False, True])
def test_repair_removes_rejected_units_and_adds_omitted_facts_without_rewriting_approved(
    packet, explicit_null
):
    from emer.contracts.answer import ProviderDraft, ProviderRepair
    from emer.services.answering import apply_repair

    approved = item(packet, "Calibration is complete.")
    bad = item(packet, "Packaging inventory is complete.")
    missing = item(packet, "Post-release observation duration varies.")
    gap = {"part_id": "part-1", "text": "An unasked shipping quote is absent."}
    base = ProviderDraft.model_validate(draft([approved, bad], gaps=[gap], next_steps=[bad]))
    reply = patch(
        statements=[{"index": 1, "replacement": None}] if explicit_null else [],
        gaps=[{"index": 0, "replacement": None}] if explicit_null else [],
        next_steps=[{"index": 0, "replacement": None}] if explicit_null else [],
        additions=draft([missing]),
    )
    repaired = apply_repair(
        base, ProviderRepair.model_validate(reply), {"statements": {1}, "gaps": {0}, "next_steps": {0}}
    )
    assert repaired.model_dump() == draft([approved, missing])
    assert base.model_dump() == draft([approved, bad], gaps=[gap], next_steps=[bad])


async def test_coverage_omission_is_added_through_patch_with_no_replacement_authority(packet):
    initial = draft([item(packet, "Calibration is complete.")])
    added = item(packet, "Packaging inventory is unfinished.")
    review = check(initial, "partial", "The requested inventory status is omitted.")
    review["coverage"][0]["missing_supported_facts"] = [added["text"]]
    complete = draft([*initial["statements"], added])
    provider = ProviderDouble(
        [
            initial,
            review,
            patch(additions=draft([added])),
            check(complete, "answered", "Both requested statuses are present."),
        ]
    )
    answer = await AnsweringService(provider).answer(packet, "Give calibration and inventory status.")
    assert answer.status == "answered" and [s.text for s in answer.statements] == [
        s["text"] for s in complete["statements"]
    ]
    assert provider.calls[2][2]["repair"]["rejected_indices"] == {
        "statements": [],
        "gaps": [],
        "next_steps": [],
    }
