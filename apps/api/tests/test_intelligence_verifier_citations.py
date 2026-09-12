"""Citation-only verifier mechanics using invented sources and declared verdicts.

These tests establish source/scope/immutability boundaries, not live semantic accuracy.
"""

from copy import deepcopy
from hashlib import sha256

import pytest
from emer.contracts.answer import (
    EvidencePacket,
    ProviderDraft,
    ProviderUsage,
    QuestionScope,
    SourceDocument,
    SupportVerdict,
)
from emer.providers.openrouter import StructuredResult
from emer.services.answering import (
    AnsweringService,
    AnswerValidationError,
    repair_verified_citations,
    validate_draft,
)

REVIEW = "The review is complete."
APPROVAL = "Staff approval is required before export."
OWNER = "The export owner is the records team."
PENDING = "The export remains pending."
CLAIM = "Staff approval is required before export, and the records team owns the export."


def source(identifier, text, category="operations"):
    return SourceDocument(
        doc_id=identifier,
        title="Invented " + identifier,
        category=category,
        version="1",
        effective_date="2026-02-03",
        authority="Test fixture only",
        text=text,
        sha256=sha256(text.encode()).hexdigest(),
    )


@pytest.fixture
def packet():
    sources = [
        source("TEST-EXPORT", f"{REVIEW} {APPROVAL} {OWNER} {PENDING}"),
        source("TEST-OTHER", "A different record has different approval conditions."),
    ]
    return EvidencePacket(
        index_id="test-pinned-index",
        index_checksum="test-index-hash",
        corpus_checksum="test-corpus-hash",
        sources=sources,
        scopes=[
            QuestionScope(
                part_id="part-1",
                question="What approval and owner apply?",
                patient_ids=[],
                unknown_patient_ids=[],
                as_of="2026-02-03",
                date_origin="question",
                all_patients=False,
                warnings=[],
                allowed_source_ids=[s.doc_id for s in sources],
            )
        ],
        retrieval={"mode": "lexical"},
    )


def citation(quote, identifier="TEST-EXPORT"):
    return {"doc_id": identifier, "quote": quote}


def statement(text=CLAIM, quote=REVIEW):
    return {"part_id": "part-1", "text": text, "citations": [citation(quote)]}


def draft(items=None, *, gaps=None, next_steps=None):
    return ProviderDraft.model_validate(
        {
            "statements": items if items is not None else [statement()],
            "gaps": gaps or [],
            "next_steps": next_steps or [],
        }
    )


def verdict(*, kind="statement", index=0, supported=True, replacements=None):
    return SupportVerdict(
        kind=kind,
        index=index,
        supported=supported,
        reason="Declared fixture semantic result.",
        citation_replacements=replacements or [],
    )


@pytest.mark.parametrize("kind", ["statement", "next_step"])
def test_exact_same_source_multiple_passages_preserve_claim_and_provenance(packet, kind):
    initial = draft() if kind == "statement" else draft([statement(REVIEW)], next_steps=[statement()])
    field = "statements" if kind == "statement" else "next_steps"
    before = getattr(initial, field)[0].text
    check = verdict(kind=kind, replacements=[citation(APPROVAL), citation(OWNER)])
    assert repair_verified_citations(packet, initial, [check]) == 1
    assert check.supported
    assert getattr(initial, field)[0].text == before
    assert [ref.quote for ref in getattr(initial, field)[0].citations] == [APPROVAL, OWNER]
    published = validate_draft(packet, initial)[0 if kind == "statement" else 1][0]
    # Two verified fragments from one paragraph become one identical display window.
    assert published.text == before and len(published.citations) == 1
    assert published.citations[0].quote == packet.sources[0].text
    assert APPROVAL in published.citations[0].quote and OWNER in published.citations[0].quote
    assert [ref.quote for ref in getattr(initial, field)[0].citations] == [APPROVAL, OWNER]
    for ref in published.citations:
        original = packet.sources[0]
        assert ref.doc_id == original.doc_id and ref.index_id == packet.index_id
        assert ref.source_sha256 == sha256(original.text.encode()).hexdigest()
        assert original.text[ref.start : ref.end] == ref.quote


@pytest.mark.parametrize(
    "replacement,reason",
    [
        (citation("A different record has different approval conditions.", "TEST-OTHER"), "different source"),
        (citation("Automatic export is already authorized."), "not an exact original passage"),
    ],
)
def test_new_document_or_hallucinated_excerpt_rejects_patch_without_mutation(packet, replacement, reason):
    initial = draft()
    before = initial.model_dump()
    check = verdict(replacements=[replacement])
    assert repair_verified_citations(packet, initial, [check]) == 0
    assert not check.supported and reason in check.reason
    assert initial.model_dump() == before


def test_original_source_permission_is_rechecked_for_replacement(packet):
    packet.scopes[0].allowed_source_ids = ["TEST-OTHER"]
    initial = draft()
    before = initial.model_dump()
    check = verdict(replacements=[citation(APPROVAL), citation(OWNER)])
    assert repair_verified_citations(packet, initial, [check]) == 0
    assert not check.supported and "outside" in check.reason
    assert initial.model_dump() == before


def test_quote_cannot_drop_the_named_patients_source_in_favor_of_another_allowed_record(packet):
    first = source("PT-071", "PT-071 assessment is pending.", "synthetic_patient")
    second = source("PT-072", "PT-072 assessment is complete.", "synthetic_patient")
    packet.sources = [first, second]
    packet.scopes[0].patient_ids = [first.doc_id, second.doc_id]
    packet.scopes[0].allowed_source_ids = [first.doc_id, second.doc_id]
    initial = draft(
        [
            {
                "part_id": "part-1",
                "text": "PT-071 assessment is pending.",
                "citations": [citation(first.text, first.doc_id), citation(second.text, second.doc_id)],
            }
        ]
    )
    before = initial.model_dump()
    check = verdict(replacements=[citation(second.text, second.doc_id)])
    assert repair_verified_citations(packet, initial, [check]) == 0
    assert not check.supported and "Missing citations for: PT-071" in check.reason
    assert initial.model_dump() == before


def test_valid_quote_cannot_override_a_false_semantic_verdict(packet):
    initial = draft([statement("The export is complete.", PENDING)])
    before = initial.model_dump()
    check = verdict(supported=False, replacements=[citation(PENDING)])
    assert repair_verified_citations(packet, initial, [check]) == 0
    assert not check.supported and initial.model_dump() == before


def test_gap_cannot_receive_a_quote_patch(packet):
    initial = draft(gaps=[{"part_id": "part-1", "text": "The export date is not supplied."}])
    before = initial.model_dump()
    check = verdict(kind="gap", replacements=[citation(PENDING)])
    assert repair_verified_citations(packet, initial, [check]) == 0
    assert not check.supported and initial.model_dump() == before


class ProviderDouble:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.calls = []

    async def structured(self, system, payload, schema, operation, **kwargs):
        self.calls.append((operation, deepcopy(payload)))
        return StructuredResult(
            value=schema.model_validate(next(self.replies)),
            usage=ProviderUsage(operation=operation, model="declared-fixture", latency_ms=1),
        )


def review(verdicts, status="answered", missing=None):
    return {
        "verdicts": [v.model_dump() for v in verdicts],
        "coverage": [
            {
                "part_id": "part-1",
                "status": status,
                "reason": "Declared fixture coverage.",
                "missing_supported_facts": missing or [],
            }
        ],
    }


async def test_complete_quote_correction_publishes_without_an_additional_generation(packet):
    initial = draft()
    provider = ProviderDouble(
        [
            initial.model_dump(),
            review([verdict(replacements=[citation(APPROVAL), citation(OWNER)])]),
        ]
    )
    answer = await AnsweringService(provider).answer(packet, "What approval and owner apply?")
    assert answer.status == "answered" and answer.statements[0].text == CLAIM
    assert len(answer.statements[0].citations) == 1
    citation_window = answer.statements[0].citations[0]
    assert citation_window.quote == packet.sources[0].text
    assert packet.sources[0].text[citation_window.start : citation_window.end] == citation_window.quote
    assert citation_window.source_sha256 == packet.sources[0].sha256
    assert citation_window.index_id == packet.index_id
    assert [operation for operation, _ in provider.calls] == ["generation", "support_check"]
    assert answer.diagnostics["repair_count"] == 0
    assert answer.diagnostics["verifier_citation_repair_count"] == 1


async def test_corrected_citations_are_part_of_immutable_verdict_cache_identity(packet):
    initial = draft([statement(), statement("The export is complete.", PENDING)])
    provider = ProviderDouble(
        [
            initial.model_dump(),
            review(
                [
                    verdict(replacements=[citation(APPROVAL), citation(OWNER)]),
                    verdict(index=1, supported=False),
                ],
                "partial",
                ["The export remains pending."],
            ),
            {
                "statement_replacements": [{"index": 1, "replacement": statement(PENDING, PENDING)}],
                "gap_replacements": [],
                "next_step_replacements": [],
                "additions": {"statements": [], "gaps": [], "next_steps": []},
            },
            review([verdict(index=1, replacements=[citation(PENDING)])]),
        ]
    )
    answer = await AnsweringService(provider).answer(packet, "What approval, owner and status apply?")
    assert answer.status == "answered"
    assert [s.text for s in answer.statements] == [CLAIM, PENDING]
    assert len(answer.statements[0].citations) == 1
    citation_window = answer.statements[0].citations[0]
    assert citation_window.quote == packet.sources[0].text
    assert packet.sources[0].text[citation_window.start : citation_window.end] == citation_window.quote
    assert citation_window.source_sha256 == packet.sources[0].sha256
    assert citation_window.index_id == packet.index_id
    assert [operation for operation, _ in provider.calls] == [
        "generation",
        "support_check",
        "repair",
        "support_check",
    ]
    final_payload = provider.calls[-1][1]
    assert [(i["kind"], i["index"]) for i in final_payload["items"]] == [("statement", 1)]
    immutable = final_payload["previously_verified_items"]
    assert len(immutable) == 1 and immutable[0]["index"] == 0 and immutable[0]["text"] == CLAIM
    assert [c["quote"] for c in immutable[0]["citations"]] == [APPROVAL, OWNER]
    assert answer.diagnostics["reused_immutable_support_verdicts"] == 1
    assert answer.diagnostics["verifier_citation_repair_count"] == 1


@pytest.mark.parametrize("kind", ["statement", "next_step"])
def test_required_empty_quote_proof_rejects_supported_factual_verdict(packet, kind):
    initial = draft() if kind == "statement" else draft([statement(REVIEW)], next_steps=[statement()])
    before = initial.model_dump()
    check = verdict(kind=kind)
    assert repair_verified_citations(packet, initial, [check], require_quotes=True) == 0
    assert not check.supported and "no exact citation evidence" in check.reason.lower()
    assert initial.model_dump() == before


@pytest.mark.parametrize("supported,kind", [(False, "statement"), (True, "gap"), (False, "gap")])
def test_missing_quote_requirement_does_not_reclassify_rejections_or_knowledge_gaps(packet, supported, kind):
    initial = draft(gaps=[{"part_id": "part-1", "text": "The export date is unavailable."}])
    check = verdict(kind=kind, supported=supported)
    before = check.model_dump()
    assert repair_verified_citations(packet, initial, [check], require_quotes=True) == 0
    assert check.model_dump() == before


def test_optional_helper_mode_preserves_legacy_direct_validation(packet):
    initial = draft()
    check = verdict()
    assert repair_verified_citations(packet, initial, [check]) == 0
    assert check.supported


@pytest.mark.parametrize("kind", ["statement", "next_step"])
def test_unchanged_complete_exact_proof_validates_without_counting_a_repair(packet, kind):
    factual = statement(APPROVAL, APPROVAL)
    initial = draft([factual]) if kind == "statement" else draft([statement(REVIEW)], next_steps=[factual])
    check = verdict(kind=kind, replacements=[citation(APPROVAL)])
    before = initial.model_dump()
    assert repair_verified_citations(packet, initial, [check], require_quotes=True) == 0
    assert check.supported and initial.model_dump() == before


def test_required_complete_quote_list_can_cover_multiple_existing_sources(packet):
    first = statement(
        "Export approval is required, and a different record has different approval conditions.", APPROVAL
    )
    first["citations"].append(citation(packet.sources[1].text, "TEST-OTHER"))
    initial = draft([first])
    check = verdict(replacements=deepcopy(first["citations"]))
    assert repair_verified_citations(packet, initial, [check], require_quotes=True) == 0
    assert check.supported
    published = validate_draft(packet, initial)[0][0]
    assert {c.doc_id for c in published.citations} == {"TEST-EXPORT", "TEST-OTHER"}


async def test_service_missing_first_proof_requires_one_repair_and_new_exact_proof(packet):
    initial = draft([statement(APPROVAL, APPROVAL)])
    provider = ProviderDouble(
        [
            initial.model_dump(),
            review([verdict()]),
            {
                "statement_replacements": [{"index": 0, "replacement": statement(APPROVAL, APPROVAL)}],
                "gap_replacements": [],
                "next_step_replacements": [],
                "additions": {"statements": [], "gaps": [], "next_steps": []},
            },
            review([verdict(replacements=[citation(APPROVAL)])]),
        ]
    )
    answer = await AnsweringService(provider).answer(packet, "What approval is required?")
    assert answer.status == "answered" and answer.statements[0].text == APPROVAL
    assert [op for op, _ in provider.calls] == ["generation", "support_check", "repair", "support_check"]
    assert answer.diagnostics["repair_count"] == 1
    assert not provider.calls[-1][1]["previously_verified_items"]
    assert len(provider.calls[-1][1]["items"]) == 1


async def test_missing_proof_after_bounded_repair_is_withheld_while_approved_unit_stays_cached(packet):
    initial = draft([statement(APPROVAL, APPROVAL), statement(OWNER, OWNER)])
    provider = ProviderDouble(
        [
            initial.model_dump(),
            review([verdict(replacements=[citation(APPROVAL)]), verdict(index=1)]),
            {
                "statement_replacements": [{"index": 1, "replacement": statement(OWNER, OWNER)}],
                "gap_replacements": [],
                "next_step_replacements": [],
                "additions": {"statements": [], "gaps": [], "next_steps": []},
            },
            review([verdict(index=1)], "partial", [OWNER]),
        ]
    )
    answer = await AnsweringService(provider).answer(packet, "What approval and owner apply?")
    assert [s.text for s in answer.statements] == [APPROVAL]
    assert answer.status == "partial" and answer.diagnostics["withheld_count"] == 1
    assert answer.diagnostics["reused_immutable_support_verdicts"] == 1
    assert [(i["kind"], i["index"]) for i in provider.calls[-1][1]["items"]] == [("statement", 1)]
    assert provider.calls[-1][1]["previously_verified_items"][0]["text"] == APPROVAL


async def test_supported_knowledge_gap_needs_no_factual_quote_proof(packet):
    initial = draft([], gaps=[{"part_id": "part-1", "text": "The exact export date is not supplied."}])
    provider = ProviderDouble([initial.model_dump(), review([verdict(kind="gap")], "unsupported")])
    answer = await AnsweringService(provider).answer(packet, "What is the exact export date?")
    assert answer.status == "unsupported" and len(answer.gaps) == 1
    assert [op for op, _ in provider.calls] == ["generation", "support_check"]


async def test_contextual_publication_preserves_prior_recipient_without_changing_verifier_refs_or_scope(
    packet,
):
    recipient = "The licensed review team receives reported concerns."
    original_quote = "Its role is to assess escalations."
    source_text = recipient + "\n\n" + original_quote
    original_source = source("TEST-EXPORT", source_text)
    packet.sources = [original_source, source("TEST-OTHER", source_text)]
    packet.scopes[0].allowed_source_ids = [original_source.doc_id]
    before_packet = packet.model_dump()
    initial = draft([statement("The licensed review team assesses escalations.", original_quote)])
    before_draft = initial.model_dump()
    provider = ProviderDouble(
        [
            initial.model_dump(),
            review([verdict(replacements=[citation(original_quote)])]),
        ]
    )
    answer = await AnsweringService(provider).answer(packet, "Who assesses escalations?")
    assert answer.status == "answered"
    assert [operation for operation, _ in provider.calls] == ["generation", "support_check"]
    checked = provider.calls[1][1]["items"][0]
    assert checked["citations"] == [citation(original_quote)]
    assert checked["text"] == before_draft["statements"][0]["text"]
    assert not provider.calls[1][1]["previously_verified_items"]
    assert initial.model_dump() == before_draft and packet.model_dump() == before_packet
    displayed = answer.statements[0].citations
    assert len(displayed) == 1
    assert (displayed[0].start, displayed[0].end, displayed[0].quote) == (0, len(source_text), source_text)
    assert original_quote in displayed[0].quote and recipient in displayed[0].quote
    assert displayed[0].doc_id == original_source.doc_id
    assert displayed[0].index_id == packet.index_id
    assert displayed[0].source_sha256 == sha256(source_text.encode()).hexdigest()
    assert answer.scopes[0].allowed_source_ids == [original_source.doc_id]
    # Identical text in a retrieved but disallowed source cannot acquire permission
    # through the context-window feature.
    unauthorized = draft([statement(original_quote, original_quote)])
    unauthorized.statements[0].citations[0].doc_id = "TEST-OTHER"
    with pytest.raises(AnswerValidationError, match="outside"):
        validate_draft(packet, unauthorized)
