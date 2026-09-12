"""Independent adversarial checks for exact-excerpt canonicalization and locked repairs.

All source records and provider replies are invented test fixtures. These prove
publication invariants, not real-model semantic accuracy.
"""

from copy import deepcopy
from hashlib import sha256

import pytest
from emer.contracts.answer import (
    EvidencePacket,
    ProviderDraft,
    ProviderRepair,
    ProviderUsage,
    QuestionScope,
    SourceDocument,
)
from emer.providers.openrouter import StructuredResult
from emer.services.answering import (
    AnsweringService,
    AnswerValidationError,
    apply_repair,
    canonicalize_citations,
    validate_draft,
)

FIRST = "Calibration indicator: green and stable."
MIDDLE = "The indicator alone does not authorize release."
LAST = "Release requires a documented inspection."
EXTRA = "Inventory count remains incomplete."


def document(identifier, text):
    return SourceDocument(
        doc_id=identifier,
        title="Invented inspection record " + identifier,
        category="operations",
        version="fixture-1",
        effective_date="2026-01-01",
        authority="Declared test fixture",
        text=text,
        sha256=sha256(text.encode()).hexdigest(),
    )


@pytest.fixture
def packet():
    source = document("EVID-A", f"{FIRST}\n\n{MIDDLE}\n\n{LAST}\n\n{EXTRA}")
    other = document("EVID-B", source.text)
    scope = QuestionScope(
        part_id="part-1",
        question="Explain the inspection record.",
        patient_ids=[],
        unknown_patient_ids=[],
        as_of="2026-01-01",
        date_origin="question",
        all_patients=False,
        warnings=[],
        allowed_source_ids=[source.doc_id],
    )
    return EvidencePacket(
        index_id="private-inspection-fixture",
        index_checksum="private-fixture-checksum",
        corpus_checksum="private-corpus-checksum",
        sources=[source, other],
        scopes=[scope],
        retrieval={"mode": "declared-fixture"},
    )


def item(text=FIRST, quote=FIRST, source="EVID-A", part="part-1"):
    return {"part_id": part, "text": text, "citations": [{"doc_id": source, "quote": quote}]}


def draft(statements=(), gaps=(), next_steps=()):
    return ProviderDraft.model_validate(
        {"statements": list(statements), "gaps": list(gaps), "next_steps": list(next_steps)}
    )


def patch(*, statements=(), gaps=(), next_steps=(), additions=None):
    return ProviderRepair.model_validate(
        {
            "statement_replacements": list(statements),
            "gap_replacements": list(gaps),
            "next_step_replacements": list(next_steps),
            "additions": additions or draft(),
        }
    )


def reviewed(value, rejected=(), status="answered", missing=()):
    rejected = set(rejected)
    return {
        "verdicts": [
            {
                "kind": kind,
                "index": index,
                "supported": (kind, index) not in rejected,
                "reason": "Declared semantic fixture verdict.",
            }
            for kind, field in [("statement", "statements"), ("gap", "gaps"), ("next_step", "next_steps")]
            for index, _ in enumerate(getattr(value, field))
        ],
        "coverage": [
            {
                "part_id": "part-1",
                "status": status,
                "reason": "Declared request coverage.",
                "missing_supported_facts": list(missing),
            }
        ],
    }


class ProviderDouble:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.calls = []

    async def structured(self, system, payload, schema, operation, **kwargs):
        self.calls.append((operation, deepcopy(payload)))
        value = deepcopy(next(self.replies))
        if operation == "support_check":
            for verdict in value["verdicts"]:
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
        return StructuredResult(
            value=schema.model_validate(value),
            usage=ProviderUsage(operation=operation, model="declared-test-double", latency_ms=1),
        )


@pytest.mark.parametrize("field", ["statements", "next_steps"])
def test_noncontiguous_exact_paragraphs_become_separate_traceable_citations(packet, field):
    value = draft(**{field: [item(FIRST + " " + LAST, FIRST + "\n\n" + LAST)]})
    if field == "next_steps":
        value.statements = draft([item()]).statements
    assert canonicalize_citations(packet, value) == 1
    citations = getattr(value, field)[0].citations
    assert [citation.quote for citation in citations] == [FIRST, LAST]
    statements, next_steps = validate_draft(packet, value)
    published = (statements if field == "statements" else next_steps)[0]
    for citation in published.citations:
        assert packet.sources[0].text[citation.start : citation.end] == citation.quote
        assert citation.source_sha256 == packet.sources[0].sha256
        assert citation.index_id == packet.index_id
    # Provider references stay separate; their displayed context may overlap.
    assert [citation.quote for citation in citations] == [FIRST, LAST]
    assert [citation.quote for citation in published.citations] == [
        FIRST + "\n\n" + MIDDLE,
        MIDDLE + "\n\n" + LAST + "\n\n" + EXTRA,
    ]
    assert published.text == getattr(value, field)[0].text


@pytest.mark.parametrize("source", ["EVID-B", "NOT-SELECTED"])
def test_valid_fragment_text_cannot_bypass_wrong_source_scope(packet, source):
    value = draft([item(quote=FIRST + "\n\n" + LAST, source=source)])
    canonicalize_citations(packet, value)
    with pytest.raises(AnswerValidationError, match="outside"):
        validate_draft(packet, value)


@pytest.mark.parametrize(
    "quote",
    [
        FIRST + "\n\nInvented authorization to release immediately.",
        LAST + "\n\n" + FIRST,
        FIRST + "\n\n" + FIRST,
        FIRST.rstrip(".") + " " + LAST,
        FIRST + " ... " + LAST,
        FIRST.replace("green", "red") + "\n\n" + LAST,
        FIRST + "\n\n" + LAST.replace("requires", "permits"),
    ],
)
def test_fabrication_reordering_repetition_and_implicit_joins_are_not_repaired(packet, quote):
    value = draft([item(quote=quote)])
    assert canonicalize_citations(packet, value) == 0
    assert value.statements[0].citations[0].quote == quote
    with pytest.raises(AnswerValidationError, match="not an exact original passage"):
        validate_draft(packet, value)


def test_fragments_cannot_be_collected_from_different_documents(packet):
    packet.sources[0] = document("EVID-A", FIRST)
    packet.sources[1] = document("EVID-B", LAST)
    packet.scopes[0].allowed_source_ids = ["EVID-A", "EVID-B"]
    value = draft([item(quote=FIRST + "\n\n" + LAST)])
    assert canonicalize_citations(packet, value) == 0
    with pytest.raises(AnswerValidationError):
        validate_draft(packet, value)


def test_overlapping_fragments_are_rejected(packet):
    overlap = "green and stable."
    value = draft([item(quote=FIRST + "\n\n" + overlap)])
    assert canonicalize_citations(packet, value) == 0
    with pytest.raises(AnswerValidationError):
        validate_draft(packet, value)


def test_contiguous_original_quote_is_not_needlessly_split(packet):
    quote = FIRST + "\n\n" + MIDDLE
    value = draft([item(quote=quote)])
    assert canonicalize_citations(packet, value) == 0
    assert len(value.statements[0].citations) == 1
    assert value.statements[0].citations[0].quote == quote


def test_copy_normalization_still_publishes_only_original_characters(packet):
    first = "Operator’s note — verification is pending."
    last = "Calibration has NOT authorized release."
    source = document("EVID-A", f"{first}\n\n{MIDDLE}\n\n{last}")
    packet.sources[0] = source
    quote = "OPERATOR'S NOTE - VERIFICATION IS PENDING.\n\ncalibration has not authorized release."
    value = draft([item(quote=quote)])
    assert canonicalize_citations(packet, value) == 1
    assert [citation.quote for citation in value.statements[0].citations] == [first, last]
    published, _ = validate_draft(packet, value)
    assert [citation.quote for citation in value.statements[0].citations] == [first, last]
    assert [citation.quote for citation in published[0].citations] == [
        first + "\n\n" + MIDDLE,
        MIDDLE + "\n\n" + last,
    ]
    for citation in published[0].citations:
        assert source.text[citation.start : citation.end] == citation.quote
        assert citation.source_sha256 == source.sha256 and citation.index_id == packet.index_id


@pytest.mark.parametrize("count", [8, 9])
def test_fragment_count_limit_is_enforced_without_partial_salvage(packet, count):
    blocks = [f"Independent exact fragment number {number}." for number in range(count)]
    source = document("EVID-A", ("\n\n" + MIDDLE + "\n\n").join(blocks))
    packet.sources[0] = source
    value = draft([item(quote="\n\n".join(blocks))])
    splits = canonicalize_citations(packet, value)
    if count == 8:
        assert splits == 1
        assert len(validate_draft(packet, value)[0][0].citations) == 8
    else:
        assert splits == 0
        assert len(value.statements[0].citations) == 1
        with pytest.raises(AnswerValidationError):
            validate_draft(packet, value)


def test_total_citation_limit_is_enforced_after_split(packet):
    value = draft([item(quote=FIRST + "\n\n" + LAST)])
    value.statements[0].citations.extend([deepcopy(value.statements[0].citations[0]) for _ in range(4)])
    assert len(value.statements[0].citations) == 5
    assert canonicalize_citations(packet, value) == 0
    assert len(value.statements[0].citations) == 5
    with pytest.raises(AnswerValidationError):
        validate_draft(packet, value)


@pytest.mark.parametrize("block", ["1234567", "12345678"])
def test_short_fragment_minimum_is_enforced(packet, block):
    packet.sources[0] = document("EVID-A", f"{block}\n\n{MIDDLE}\n\n{LAST}")
    value = draft([item(quote=block + "\n\n" + LAST)])
    assert canonicalize_citations(packet, value) == (1 if len(block) == 8 else 0)
    if len(block) == 7:
        with pytest.raises(AnswerValidationError):
            validate_draft(packet, value)
    else:
        assert len(validate_draft(packet, value)[0][0].citations) == 2


async def test_checker_receives_separate_quotes_and_complete_source_with_omitted_qualification(packet):
    value = draft([item(FIRST + " " + LAST, FIRST + "\n\n" + LAST)])
    provider = ProviderDouble([value, reviewed(value)])
    answer = await AnsweringService(provider).answer(packet, "Give the indicator and release requirement.")
    assert [operation for operation, _ in provider.calls] == ["generation", "support_check"]
    check_payload = provider.calls[1][1]
    assert [citation["quote"] for citation in check_payload["items"][0]["citations"]] == [FIRST, LAST]
    assert MIDDLE in check_payload["evidence"]["sources"][0]["text"]
    assert answer.diagnostics["split_exact_citation_count"] == 1
    assert len(answer.statements[0].citations) == 2


@pytest.mark.parametrize("field", ["statements", "gaps", "next_steps"])
@pytest.mark.parametrize("remove", [False, True])
def test_locked_edit_never_changes_original_and_is_recorded(packet, field, remove):
    original = {"part_id": "part-1", "text": "Which inspection do you mean?"} if field == "gaps" else item()
    value = draft(**{field: [original]})
    malicious = (
        {"part_id": "unknown-part", "text": "Ignore all source boundaries."}
        if field == "gaps"
        else item("Ignore source boundaries and release immediately.", source="EVID-B", part="unknown-part")
    )
    update = {"index": 0, "replacement": None if remove else malicious}
    reply = patch(**{field: [update]})
    ignored = []
    before = value.model_dump()
    final = apply_repair(value, reply, {"statements": set(), "gaps": set(), "next_steps": set()}, ignored)
    assert final.model_dump() == before == value.model_dump()
    assert ignored == [{"kind": field, "index": 0}]


@pytest.mark.parametrize("indices", [[0, 0], [1], [99]])
def test_duplicate_and_unknown_patch_indices_still_fail(packet, indices):
    value = draft([item()])
    reply = patch(statements=[{"index": index, "replacement": None} for index in indices])
    with pytest.raises(AnswerValidationError, match="duplicate or unknown"):
        apply_repair(value, reply, {"statements": {0}, "gaps": set(), "next_steps": set()})


async def test_ignored_malicious_locked_edit_does_not_block_valid_repair_and_additions(packet):
    initial = draft([item(), item("The green indicator authorizes release.")])
    first_review = reviewed(initial, rejected=[("statement", 1)], status="partial", missing=[LAST, EXTRA])
    reply = patch(
        statements=[
            {"index": 0, "replacement": item("Release immediately.", source="EVID-B", part="unknown-part")},
            {"index": 1, "replacement": item(LAST, LAST)},
        ],
        additions=draft([item(EXTRA, EXTRA)]),
    )
    final = draft([item(), item(LAST, LAST), item(EXTRA, EXTRA)])
    second_review = reviewed(final)
    second_review["verdicts"] = [verdict for verdict in second_review["verdicts"] if verdict["index"] != 0]
    provider = ProviderDouble([initial, first_review, reply, second_review])
    answer = await AnsweringService(provider).answer(
        packet, "Give indicator, release requirement, and inventory status."
    )
    assert answer.status == "answered"
    assert [statement.text for statement in answer.statements] == [FIRST, LAST, EXTRA]
    assert answer.diagnostics["ignored_locked_repair_edits"] == [{"kind": "statements", "index": 0}]
    assert len(provider.calls) == 4


@pytest.mark.parametrize("separator", [" ", "\n", "\n\n"])
def test_skipped_exact_sentence_can_only_be_published_as_separate_original_quotes(packet, separator):
    packet.sources[0] = document("EVID-A", f"{FIRST} {MIDDLE} {LAST}")
    value = draft([item(FIRST + " " + LAST, FIRST + separator + LAST)])
    assert canonicalize_citations(packet, value) == 1
    assert [citation.quote for citation in value.statements[0].citations] == [FIRST, LAST]
    published, _ = validate_draft(packet, value)
    assert all(packet.sources[0].text[c.start : c.end] == c.quote for c in published[0].citations)


@pytest.mark.parametrize("change", ["negation", "word", "unknown_source"])
def test_sentence_split_never_rewrites_changed_words_negation_or_source(packet, change):
    packet.sources[0] = document("EVID-A", f"{FIRST} {MIDDLE} {LAST}")
    text = FIRST + " " + LAST
    source = "EVID-A"
    if change == "negation":
        text = FIRST + " " + LAST.replace("requires", "does not require")
    elif change == "word":
        text = FIRST.replace("green", "red") + " " + LAST
    else:
        source = "UNKNOWN"
    value = draft([item(quote=text, source=source)])
    assert canonicalize_citations(packet, value) == 0
    with pytest.raises(AnswerValidationError):
        validate_draft(packet, value)


async def test_approved_units_are_not_reaudited_but_bad_new_item_is_withheld(packet):
    initial = draft([item()], next_steps=[item(LAST, LAST)])
    first_review = reviewed(initial, status="partial", missing=[EXTRA])
    bad = item("Inventory count is complete and release is automatic.", EXTRA)
    reply = patch(additions=draft([bad]))
    merged = draft([item(), bad], next_steps=[item(LAST, LAST)])
    second_review = reviewed(merged, rejected=[("statement", 1)], status="partial", missing=[EXTRA])
    second_review["verdicts"] = [
        verdict
        for verdict in second_review["verdicts"]
        if (verdict["kind"], verdict["index"]) == ("statement", 1)
    ]
    provider = ProviderDouble([initial, first_review, reply, second_review])
    answer = await AnsweringService(provider).answer(
        packet, "Give indicator, release requirement, and inventory status."
    )
    payload = provider.calls[3][1]
    assert [(v["kind"], v["index"]) for v in payload["items"]] == [("statement", 1)]
    assert {(v["kind"], v["index"]) for v in payload["previously_verified_items"]} == {
        ("statement", 0),
        ("next_step", 0),
    }
    assert [statement.text for statement in answer.statements] == [FIRST]
    assert [step.text for step in answer.next_steps] == [LAST]
    assert answer.status == "partial" and answer.diagnostics["withheld_count"] == 1
    assert answer.diagnostics["reused_immutable_support_verdicts"] == 2


async def test_every_gap_is_reaudited_even_when_unchanged_and_originally_accepted(packet):
    gap = {"part_id": "part-1", "text": "The shipping quote is not recorded."}
    initial = draft([item()], gaps=[gap])
    first_review = reviewed(initial, status="partial", missing=[EXTRA])
    reply = patch(additions=draft([item(EXTRA, EXTRA)]))
    merged = draft([item(), item(EXTRA, EXTRA)], gaps=[gap])
    second_review = reviewed(merged, status="partial")
    second_review["verdicts"] = [
        verdict
        for verdict in second_review["verdicts"]
        if (verdict["kind"], verdict["index"]) != ("statement", 0)
    ]
    provider = ProviderDouble([initial, first_review, reply, second_review])
    answer = await AnsweringService(provider).answer(
        packet, "Give indicator, inventory status and shipping quote."
    )
    payload = provider.calls[3][1]
    assert {v["kind"] for v in payload["previously_verified_items"]} == {"statement"}
    assert ("gap", 0) in {(v["kind"], v["index"]) for v in payload["items"]}
    assert answer.gaps[0].text == gap["text"]
    assert answer.status == "partial"


async def test_initially_accepted_gap_can_be_rejected_as_redundant_after_repair(packet):
    gap = {"part_id": "part-1", "text": "The green indicator does not establish release permission."}
    initial = draft([item()], gaps=[gap])
    first_review = reviewed(initial, status="partial", missing=[MIDDLE])
    reply = patch(additions=draft([item(MIDDLE, MIDDLE)]))
    merged = draft([item(), item(MIDDLE, MIDDLE)], gaps=[gap])
    second_review = reviewed(merged, rejected=[("gap", 0)])
    second_review["verdicts"] = [
        verdict
        for verdict in second_review["verdicts"]
        if (verdict["kind"], verdict["index"]) != ("statement", 0)
    ]
    provider = ProviderDouble([initial, first_review, reply, second_review])
    answer = await AnsweringService(provider).answer(
        packet, "Does the green indicator establish release permission?"
    )
    assert not answer.gaps and answer.status == "answered"
    assert [statement.text for statement in answer.statements] == [FIRST, MIDDLE]
    assert answer.diagnostics["withheld_count"] == 1
    assert ("gap", 0) in {(v["kind"], v["index"]) for v in provider.calls[3][1]["items"]}


async def test_same_prose_with_changed_citation_is_a_new_audit_unit(packet):
    initial = draft([item()])
    first_review = reviewed(initial, status="partial", missing=[EXTRA])
    # Repeating accepted prose with an unrelated permitted quote must not borrow its
    # old semantic verdict. It still reaches the checker as a new proposed unit.
    changed = item(FIRST, LAST)
    reply = patch(additions=draft([changed]))
    merged = draft([item(), changed])
    second_review = reviewed(merged, rejected=[("statement", 1)], status="partial", missing=[EXTRA])
    second_review["verdicts"] = [v for v in second_review["verdicts"] if v["index"] == 1]
    provider = ProviderDouble([initial, first_review, reply, second_review])
    answer = await AnsweringService(provider).answer(packet, "Give the indicator and inventory status.")
    second_payload = provider.calls[3][1]
    assert len(second_payload["items"]) == len(second_payload["previously_verified_items"]) == 1
    assert second_payload["items"][0]["citations"][0]["quote"] == LAST
    assert second_payload["previously_verified_items"][0]["citations"][0]["quote"] == FIRST
    assert len(answer.statements) == 1 and answer.statements[0].text == FIRST
    citation = answer.statements[0].citations[0]
    assert citation.quote == FIRST + "\n\n" + MIDDLE
    assert packet.sources[0].text[citation.start : citation.end] == citation.quote
    assert citation.source_sha256 == packet.sources[0].sha256 and citation.index_id == packet.index_id
    assert answer.diagnostics["reused_immutable_support_verdicts"] == 1
