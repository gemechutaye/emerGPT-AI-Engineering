"""Structured generation → deterministic citation checks → batched support review → publication."""

from __future__ import annotations

import json
import re
import time
from hashlib import sha256
from itertools import pairwise
from pathlib import Path
from typing import Any

from emer.contracts.answer import (
    AnswerGap,
    Citation,
    EvidencePacket,
    ProviderDraft,
    ProviderRepair,
    PublishedAnswer,
    PublishedStatement,
    SupportCheck,
)
from emer.domain.citation_context import citation_context_span
from emer.domain.record_assertions import record_assertion_problems
from emer.domain.scope import patient_ids
from emer.providers.openrouter import OpenRouterClient, ProviderError
from emer.services.source_coverage import source_audit_units, source_coverage_problems

PROMPTS = Path(__file__).resolve().parents[1] / "prompts"
ANSWER_PROMPT_ID = "answer-v16"
CHECKER_PROMPT_ID = "check-v20"
_EQUIVALENT = str.maketrans({"’": "'", "‘": "'", "“": '"', "”": '"', "–": "-", "—": "-", "\u00a0": " "})
_RECORD_ABSENCE = re.compile(
    r"\b(?:no|not|never|unknown|uncertain|undocumented|unspecified|unrecorded)\b"
    r"|\b(?:doesn't|isn't|hasn't|haven't|can't|cannot)\b",
    re.IGNORECASE,
)


def _normalized(text: str) -> tuple[str, list[int]]:
    """Case-fold, unify quote/dash variants and collapse whitespace; map each kept character back."""
    out: list[str] = []
    index_map: list[int] = []
    previous_space = False
    for position, char in enumerate(text.translate(_EQUIVALENT)):
        if char.isspace():
            if previous_space:
                continue
            char, previous_space = " ", True
        else:
            previous_space = False
        out.append(char.lower())
        index_map.append(position)
    return "".join(out), index_map


def locate_quote(text: str, quote: str) -> tuple[int, int] | None:
    """Find a model-copied quote in the original text; the published span is always the original bytes.

    Exact match first. Otherwise tolerate the copying differences models actually make (collapsed
    double spaces, straight vs. curly quotes, dash variants, letter case). Nothing fuzzier: a quote
    that is not a continuous original passage is still rejected.
    """
    if not quote.strip():
        return None
    start = text.find(quote)
    if start >= 0:
        return start, start + len(quote)
    normalized_text, index_map = _normalized(text)
    normalized_quote, _ = _normalized(quote.strip())
    if not normalized_quote:
        return None
    position = normalized_text.find(normalized_quote)
    if position < 0:
        return None
    return index_map[position], index_map[position + len(normalized_quote) - 1] + 1


def canonicalize_citations(packet: EvidencePacket, draft: ProviderDraft) -> int:
    """Resolve copied excerpts to original spans; split only independently exact sentence/paragraph blocks.

    A model sometimes joins genuine sentences or paragraphs while omitting intervening text.
    Never publish that join as one original quote. If every explicitly separated block
    matches this same source, preserve it as separate citations; otherwise validation
    still rejects the reference. No fuzzy word matching or generated replacement text.
    """
    sources = {source.doc_id: source for source in packet.sources}
    split_count = 0
    for statement in [*draft.statements, *draft.next_steps]:
        resolved = []
        item_splits = 0
        for ref in statement.citations:
            source = sources.get(ref.doc_id)
            span = locate_quote(source.text, ref.quote) if source else None
            if span:
                resolved.append(ref.model_copy(update={"quote": source.text[span[0] : span[1]]}))
                continue
            candidates = [
                re.split(r"\n\s*\n", ref.quote.strip()),
                re.split(r"(?<=[.!?])\s+(?=[A-Z])", ref.quote.strip()),
            ]
            for blocks in candidates:
                spans = [locate_quote(source.text, block) for block in blocks] if source else []
                if (
                    2 <= len(blocks) <= 8
                    and all(len(block.strip()) >= 8 for block in blocks)
                    and spans
                    and all(span is not None for span in spans)
                    and all(left[1] <= right[0] for left, right in pairwise(spans))
                ):
                    resolved.extend(
                        ref.model_copy(update={"quote": source.text[start:end]}) for start, end in spans
                    )
                    item_splits += 1
                    break
            else:
                resolved.append(ref)
        if len(resolved) <= 8:
            statement.citations = resolved
            split_count += item_splits
    return split_count


def model_context(packet: EvidencePacket, question: str) -> dict[str, Any]:
    """Send source evidence and explicit task boundaries, not search debug ranks.

    Debug rankings remain on the persisted packet. They neither establish facts nor
    assign a patient. Date/patient parts partition one answer, not repeated requests.
    """
    evidence = packet.model_dump(mode="json", exclude={"retrieval"})
    if packet.retrieval.get("unit") == "source_chunk":
        # Full immutable originals exist only for provenance validation/persistence.
        # Models receive selected exact passages, never a second full-document copy.
        evidence["sources"] = [source.model_dump(mode="json", exclude={"text"}) for source in packet.sources]
        return {"question": question, "evidence": evidence,
                "coverage_boundary": "Passages are the inspected evidence. Empty/partial retrieval does not prove corpus-wide absence."}
    return {
        "question": question,
        "evidence": evidence,
        "answer_partition": {
            "rule": "Together these parts answer one request. Each part answers only its assigned "
            "date and patient scope. Information assigned to another part is not a knowledge "
            "gap in this part. Judge comparison completeness across the combined answer.",
            "parts": [
                {
                    "part_id": scope.part_id,
                    "assigned_date": scope.as_of,
                    "question_slice": scope.question,
                    "patient_ids": scope.patient_ids,
                    "patient_discovery": scope.patient_discovery,
                }
                for scope in packet.scopes
            ],
        },
    }


def support_payload(
    packet: EvidencePacket,
    question: str,
    draft: ProviderDraft,
    verified: dict[tuple[str, int], Any] | None = None,
) -> dict[str, Any]:
    """Bind each audit item to its real per-kind index before provider serialization."""
    return {
        **model_context(packet, question),
        "source_audit_units": source_audit_units(packet, draft),
        "items": [
            {"kind": kind, "index": index, **item.model_dump(mode="json")}
            for kind, items in (
                ("statement", draft.statements),
                ("gap", draft.gaps),
                ("next_step", draft.next_steps),
            )
            for index, item in enumerate(items)
            if (kind, index) not in (verified or {})
        ],
        "previously_verified_items": [
            {"kind": kind, "index": index, **item.model_dump(mode="json")}
            for kind, items in (
                ("statement", draft.statements),
                ("gap", draft.gaps),
                ("next_step", draft.next_steps),
            )
            for index, item in enumerate(items)
            if (kind, index) in (verified or {})
        ],
    }


def support_identity(kind: str, item: Any) -> tuple[str, str]:
    return kind, json.dumps(item.model_dump(mode="json"), sort_keys=True, ensure_ascii=False)


def inventory_coverage_problems(packet, draft, review) -> list[str]:
    facts = {fact["fact_id"]: (part["part_id"], fact["text"])
             for part in packet.assessment.get("parts", []) for fact in part["facts"]}
    if not facts:
        return []
    if {item.fact_id for item in review.fact_coverage} != set(facts) or len(review.fact_coverage) != len(facts):
        raise AnswerValidationError("The verifier must account for every inventoried fact exactly once.")
    approved = {(item.kind, item.index) for item in review.verdicts if item.supported}
    problems = []
    for item in review.fact_coverage:
        part_id, fact = facts[item.fact_id]
        linked = [("statement", index, draft.statements) for index in item.statement_indices]
        linked += [("next_step", index, draft.next_steps) for index in item.next_step_indices]
        linked += [("gap", index, draft.gaps) for index in item.gap_indices]
        if item.status == "covered":
            if not linked or any(index < 0 or index >= len(items) or items[index].part_id != part_id
                                 or (kind, index) not in approved for kind, index, items in linked):
                raise AnswerValidationError("Fact coverage must point to approved prose in its own question part.")
        elif item.status == "omitted":
            problems.append(f"coverage {part_id}: Include inventoried fact {item.fact_id}: {fact}")
        elif not item.reason.strip():
            raise AnswerValidationError("Rejecting an inventoried fact requires a source-grounded reason.")
    return problems


class AnswerValidationError(ProviderError):
    def __init__(self, message: str):
        super().__init__("answer_validation_failed", message)
        self.problems: list[str] = [message]


def validate_draft(
    packet: EvidencePacket, draft: ProviderDraft
) -> tuple[list[PublishedStatement], list[PublishedStatement]]:
    sources = {source.doc_id: source for source in packet.sources}
    scopes = {scope.part_id: scope for scope in packet.scopes}
    if not (draft.statements or draft.gaps):
        raise AnswerValidationError(
            "The provider returned neither supported information nor a knowledge gap."
        )
    errors = []
    for index, gap in enumerate(draft.gaps):
        scope = scopes.get(gap.part_id)
        if scope is None:
            errors.append(f"gaps[{index}]: The response references an unknown question part.")
            continue
        if set(patient_ids(gap.text)) - set(scope.patient_ids + scope.unknown_patient_ids):
            errors.append(f"gaps[{index}]: The response crossed patient scope.")

    def publish(statement: Any) -> PublishedStatement:
        scope = scopes.get(statement.part_id)
        if scope is None:
            raise AnswerValidationError("The response references an unknown question part.")
        mentioned = set(patient_ids(statement.text))
        if mentioned - set(scope.patient_ids):
            raise AnswerValidationError("The response crossed patient scope.")
        citations = []
        cited_patients = set()
        for ref in statement.citations:
            if ref.doc_id not in sources or ref.doc_id not in scope.allowed_source_ids:
                raise AnswerValidationError(
                    f"Citation {ref.doc_id} is outside this question's patient or policy scope."
                )
            source = sources[ref.doc_id]
            span = locate_quote(source.text, ref.quote)
            if packet.retrieval.get("unit") == "source_chunk":
                span = next((
                    (passage.start + local[0], passage.start + local[1])
                    for passage in packet.passages
                    if passage.doc_id == source.doc_id and statement.part_id in passage.scope_ids
                    and (local := locate_quote(passage.text, ref.quote)) is not None
                ), None)
            if span is None:
                raise AnswerValidationError(f"Citation {ref.doc_id} quote is not an exact original passage.")
            start, end = span
            if packet.retrieval.get("unit") == "source_chunk" and not any(
                passage.doc_id == source.doc_id and statement.part_id in passage.scope_ids
                and passage.start <= start < end <= passage.end
                for passage in packet.passages
            ):
                raise AnswerValidationError(
                    f"Citation {ref.doc_id} is outside the evidence passages retrieved for this question part."
                )
            # Keep the exact cited fragment in bounded paragraph context, so
            # qualifications and nearby antecedents remain visible in the reader.
            # This changes presentation spans only; scope and support checks still
            # apply to the model's claim and its original same-source references.
            start, end = citation_context_span(source.text, start, end)
            if source.category == "synthetic_patient":
                cited_patients.add(source.doc_id)
                # A fragment cannot show that a field is absent from an entire record.
                # Expand only an already validated, same-source short record citation.
                # This improves traceability; the semantic checker still decides support.
                if len(source.text) <= 1600 and _RECORD_ABSENCE.search(statement.text):
                    start, end = 0, len(source.text)
            citations.append(
                Citation(
                    doc_id=source.doc_id,
                    title=source.title,
                    version=source.version,
                    effective_date=source.effective_date,
                    authority=source.authority,
                    source_sha256=source.sha256,
                    index_id=packet.index_id,
                    quote=source.text[start:end],
                    start=start,
                    end=end,
                )
            )
        if mentioned - cited_patients:
            raise AnswerValidationError(
                "A patient-specific statement must cite that patient's source record. "
                "Missing citations for: " + ", ".join(sorted(mentioned - cited_patients)) + ". "
                "Add an exact supporting passage from each named patient's record, or remove the unsupported patient-specific clause."
            )
        clarity_problems = record_assertion_problems(statement, packet.sources)
        if clarity_problems:
            raise AnswerValidationError(" ".join(clarity_problems))
        unique = {(c.doc_id, c.index_id, c.start, c.end): c for c in citations}
        return PublishedStatement(
            part_id=statement.part_id, text=statement.text, citations=list(unique.values())
        )

    published = []
    for kind, items in (("statements", draft.statements), ("next_steps", draft.next_steps)):
        accepted = []
        for index, item in enumerate(items):
            try:
                accepted.append(publish(item))
            except AnswerValidationError as exc:
                errors.append(f"{kind}[{index}]: {exc}")
        published.append(accepted)
    if errors:
        failure = AnswerValidationError("\n".join(errors))
        failure.problems = errors
        raise failure
    return published[0], published[1]


def repair_verified_citations(
    packet: EvidencePacket, draft: ProviderDraft, verdicts: list[Any], *, require_quotes: bool = False
) -> int:
    """A verifier may correct an excerpt, never a claim or the cited source identity.

    Apply only to an approved factual item and re-run all deterministic scope, source,
    identity and exact-span checks. Invalid verifier excerpts become repair feedback;
    they cannot turn a false claim into a supported one or expand document authority.
    """
    corrected = 0
    for verdict in verdicts:
        if not verdict.citation_replacements:
            if require_quotes and verdict.supported and verdict.kind != "gap":
                verdict.supported = False
                verdict.reason = (
                    "The verifier supplied no exact citation evidence for this factual item. "
                    "Audit every clause and supply its complete same-source citation list."
                )
            continue
        if not verdict.supported or verdict.kind == "gap":
            verdict.supported = False
            verdict.reason = "Citation replacement is valid only for a source-supported factual item."
            continue
        field = "statements" if verdict.kind == "statement" else "next_steps"
        original = getattr(draft, field)[verdict.index]
        existing_sources = {ref.doc_id for ref in original.citations}
        if {ref.doc_id for ref in verdict.citation_replacements} - existing_sources:
            verdict.supported = False
            verdict.reason = "Citation replacement cannot introduce a different source document."
            continue
        candidate = ProviderDraft(statements=[original.model_copy(deep=True)], gaps=[], next_steps=[])
        candidate.statements[0].citations = verdict.citation_replacements
        canonicalize_citations(packet, candidate)
        try:
            validate_draft(packet, candidate)
        except AnswerValidationError as exc:
            verdict.supported = False
            verdict.reason = "Verifier citation replacement is invalid: " + str(exc)
            continue
        replacement = candidate.statements[0].citations
        corrected += int(original.citations != replacement)
        original.citations = replacement
    return corrected


def discard_invalid_repair_units(packet: EvidencePacket, draft: ProviderDraft) -> list[dict[str, Any]]:
    """Keep independently valid repair units; never broaden source or patient permissions.

    Used only after a typed repair has preserved the locked, previously approved units.
    Remaining units still need the normal complete support audit. A failed replacement
    is an explicit publication limitation, not evidence that the knowledge is absent.
    """
    rejected = []
    for field in ("statements", "gaps", "next_steps"):
        kept = []
        for index, item in enumerate(getattr(draft, field)):
            candidate = ProviderDraft(
                statements=[] if field == "gaps" else [item],
                gaps=[item] if field == "gaps" else [],
                next_steps=[],
            )
            try:
                validate_draft(packet, candidate)
            except AnswerValidationError as exc:
                rejected.append({"kind": field, "index": index, "reason": str(exc)})
            else:
                kept.append(item)
        setattr(draft, field, kept)
    return rejected


def apply_repair(
    base: ProviderDraft,
    patch: ProviderRepair,
    rejected: dict[str, set[int]],
    ignored: list[dict[str, Any]] | None = None,
) -> ProviderDraft:
    """A repair can replace rejected units and add missing facts; accepted units are immutable."""
    final = {}
    for field, patch_field in (
        ("statements", "statement_replacements"),
        ("gaps", "gap_replacements"),
        ("next_steps", "next_step_replacements"),
    ):
        changes = getattr(patch, patch_field)
        indices = [change.index for change in changes]
        if len(set(indices)) != len(indices) or set(indices) - set(range(len(getattr(base, field)))):
            raise AnswerValidationError("Repair supplied a duplicate or unknown response index.")
        # Locked units retain the original value regardless of a model's attempted edit.
        # Ignoring an unauthorized edit is safe; it cannot erase approved information.
        if ignored is not None:
            ignored.extend(
                {"kind": field, "index": index} for index in indices if index not in rejected[field]
            )
        replacements = {
            change.index: change.replacement for change in changes if change.index in rejected[field]
        }
        kept = [
            replacements.get(index) if index in rejected[field] else item
            for index, item in enumerate(getattr(base, field))
        ]
        # Omitting an explicitly rejected item safely removes it; no guessed replacement.
        final[field] = [item for item in kept if item is not None] + getattr(patch.additions, field)
    try:
        return ProviderDraft.model_validate(final)
    except ValueError as exc:
        raise AnswerValidationError("The repaired response exceeds its output contract.") from exc


class AnsweringService:
    def __init__(
        self, provider: OpenRouterClient, checker: OpenRouterClient | None = None, check_support: bool = True
    ):
        self.provider = provider
        self.checker = checker or provider
        self.check_support = check_support

    async def answer(self, packet: EvidencePacket, question: str) -> PublishedAnswer:
        independent = packet.retrieval.get("unit") == "source_chunk"
        if independent and self.check_support and (
            self.checker is self.provider or not getattr(self.checker, "model", None)
            or self.checker.model == getattr(self.provider, "model", None)
        ):
            raise ProviderError("independent_verifier_required", "A distinct configured verifier is required.")
        started = time.perf_counter()
        prompt = (PROMPTS / f"{ANSWER_PROMPT_ID}.txt").read_text()
        checker_prompt = (PROMPTS / f"{CHECKER_PROMPT_ID}.txt").read_text()
        payload: dict[str, Any] = model_context(packet, question)
        usage = []
        problems: list[str] = []
        verdicts = []
        coverage = []
        repair_count = 0
        ignored_repair_edits: list[dict[str, Any]] = []
        ignored_nonexistent_verdicts: list[dict[str, Any]] = []
        ignored_duplicate_verdicts: list[dict[str, Any]] = []
        invalid_repair_units: list[dict[str, Any]] = []
        split_citation_count = 0
        verifier_citation_repair_count = 0
        rate_limit_retries = 0
        empty_draft_fallback = False
        repair_base = None
        repair_targets = None
        verified_units: dict[tuple[str, str], Any] = {}
        reused_support_count = 0
        try:
            for attempt in range(2):
                response = await self.provider.structured(
                    prompt,
                    payload,
                    ProviderRepair if repair_base is not None else ProviderDraft,
                    operation="generation" if attempt == 0 else "repair",
                )
                usage.append(response.usage)
                rate_limit_retries += response.rate_limit_retries
                draft = (
                    apply_repair(
                        repair_base,
                        ProviderRepair.model_validate(response.value),
                        repair_targets,
                        ignored_repair_edits,
                    )
                    if repair_base is not None
                    else ProviderDraft.model_validate(response.value)
                )
                if (
                    attempt == 1
                    and not independent
                    and repair_base is None
                    and not (draft.statements or draft.gaps or draft.next_steps)
                ):
                    # The provider succeeded twice and had nothing grounded to say (e.g. gibberish or an
                    # unrelated request). That is an honest "not in these records", not an outage.
                    empty_draft_fallback = True
                    statements, next_steps, verdicts, coverage = [], [], [], []
                    draft.gaps = [
                        AnswerGap(
                            part_id=scope.part_id,
                            text="No statement or knowledge gap could be grounded in the supplied records for this request.",
                        )
                        for scope in packet.scopes
                    ]
                    problems = []
                    break
                split_citation_count += canonicalize_citations(packet, draft)
                try:
                    statements, next_steps = validate_draft(packet, draft)
                except AnswerValidationError as exc:
                    if attempt == 1 and repair_base is not None:
                        invalid_repair_units.extend(discard_invalid_repair_units(packet, draft))
                        statements, next_steps = validate_draft(packet, draft)
                    elif attempt == 1:
                        raise
                    else:
                        problems = [str(exc)]
                        payload["repair"] = {
                            "draft": draft.model_dump(),
                            "problems": problems,
                            "instruction": "Correct the structured draft once. Use only exact permitted citations. Preserve supported parts.",
                        }
                        repair_count = 1
                        continue
                problems = []
                if self.check_support:
                    verified = {
                        (kind, index): verified_units[support_identity(kind, item)].model_copy(
                            update={"index": index}
                        )
                        for kind, items in (
                            ("statement", draft.statements),
                            ("gap", draft.gaps),
                            ("next_step", draft.next_steps),
                        )
                        for index, item in enumerate(items)
                        if support_identity(kind, item) in verified_units
                    }
                    reused_support_count += len(verified)
                    check = await self.checker.structured(
                        checker_prompt,
                        support_payload(packet, question, draft, verified),
                        SupportCheck,
                        operation="support_check",
                        max_tokens=6000 if independent else 4000,
                    )
                    usage.append(check.usage)
                    rate_limit_retries += check.rate_limit_retries
                    review = SupportCheck.model_validate(check.value)
                    expected = {
                        (kind, i)
                        for kind, items in (
                            ("statement", draft.statements),
                            ("gap", draft.gaps),
                            ("next_step", draft.next_steps),
                        )
                        for i in range(len(items))
                    }
                    # A verdict about a nonexistent item cannot approve, reject, or
                    # alter real content. Retain it as protocol diagnostics, then
                    # require exactly one verdict for every actual requested item.
                    # Conflicting duplicates, missing items and re-audits of locked
                    # items still fail. Byte-equivalent repeated verdicts add no vote.
                    ignored_nonexistent_verdicts.extend(
                        v.model_dump() for v in review.verdicts if (v.kind, v.index) not in expected
                    )
                    review.verdicts = [
                        v for v in review.verdicts if (v.kind, v.index) in expected
                    ]
                    unique_verdicts = {}
                    for verdict in review.verdicts:
                        identity = json.dumps(verdict.model_dump(), sort_keys=True)
                        if identity in unique_verdicts:
                            ignored_duplicate_verdicts.append(verdict.model_dump())
                        else:
                            unique_verdicts[identity] = verdict
                    review.verdicts = list(unique_verdicts.values())
                    verdicts, coverage = [*verified.values(), *review.verdicts], review.coverage
                    # The checker lists actual requested portions before choosing a label.
                    # Derive compound coverage from that explicit accounting, so an absent
                    # value cannot erase an independently answered education/status request.
                    for item in coverage:
                        if item.status != "clarification" and (
                            item.answered_requests or item.unanswered_requests
                        ):
                            item.status = (
                                "partial"
                                if item.answered_requests
                                and (item.unanswered_requests or item.missing_supported_facts)
                                else "answered"
                                if item.answered_requests
                                else "unsupported"
                            )
                    received_new = {(v.kind, v.index) for v in review.verdicts}
                    received = {(v.kind, v.index) for v in verdicts}
                    if (
                        received_new != expected - set(verified)
                        or len(received_new) != len(review.verdicts)
                        or received != expected
                        or len(received) != len(verdicts)
                    ):
                        raise AnswerValidationError(
                            "The support checker did not review every response item exactly once."
                        )
                    verifier_citation_repair_count += repair_verified_citations(
                        packet, draft, review.verdicts, require_quotes=True
                    )
                    statements, next_steps = validate_draft(packet, draft)
                    expected_parts = {scope.part_id for scope in packet.scopes}
                    if {item.part_id for item in coverage} != expected_parts or len(coverage) != len(
                        expected_parts
                    ):
                        raise AnswerValidationError(
                            "The support checker did not assess every question part's answerability exactly once."
                        )
                    problems = [f"{v.kind} {v.index}: {v.reason}" for v in verdicts if not v.supported]
                    supported_gap_parts = {
                        draft.gaps[v.index].part_id for v in verdicts if v.kind == "gap" and v.supported
                    }
                    # A supported but incomplete response also gets the existing single repair.
                    # Accepted source gaps already explain genuine missing knowledge; they are
                    # not an instruction to invent an answer or retry an unanswerable question.
                    coverage_problems = [
                        f"coverage {item.part_id}: {item.reason} "
                        + (
                            "Missing supported facts: " + "; ".join(item.missing_supported_facts) + ". "
                            if item.missing_supported_facts
                            else ""
                        )
                        + "Address the missing requested information from permitted sources, or explain "
                        "its precise absence in a gap. Do not add unasked requirements."
                        for item in coverage
                        if item.missing_supported_facts
                        or (item.status == "partial" and item.part_id not in supported_gap_parts)
                    ]
                    inventory_problems = inventory_coverage_problems(packet, draft, review)
                    coverage_problems.extend(inventory_problems)
                    coverage_problems.extend(source_coverage_problems(packet, draft, review))
                    if (problems or coverage_problems) and attempt == 0:
                        if independent:
                            # Every item is independently checked again. Legacy locked-item
                            # patches prevent repairing a qualification inside an accepted
                            # sentence and make models reconstruct a needless edit protocol.
                            verified_units = {}
                            payload["repair"] = {
                                "draft": draft.model_dump(),
                                "problems": [*problems, *coverage_problems],
                                "instruction": "Return one complete corrected ProviderDraft. All items may be revised "
                                "and every item will be independently rechecked. Preserve supported information, "
                                "repair the listed qualifications and omissions using permitted sources, and do not "
                                "add new requests or invent missing values.",
                            }
                            repair_count = 1
                            continue
                        repair_base = draft.model_copy(deep=True)
                        verified_units = {} if independent else {
                            support_identity(kind, item): next(
                                v for v in verdicts if v.kind == kind and v.index == index
                            )
                            for kind, items in (
                                ("statement", draft.statements),
                                ("gap", draft.gaps),
                                ("next_step", draft.next_steps),
                            )
                            for index, item in enumerate(items)
                            if kind != "gap"
                            and any(v.kind == kind and v.index == index and v.supported for v in verdicts)
                        }
                        repair_targets = {
                            field: {v.index for v in verdicts if v.kind == kind and not v.supported}
                            for kind, field in (
                                ("statement", "statements"),
                                ("gap", "gaps"),
                                ("next_step", "next_steps"),
                            )
                        }
                        payload["repair"] = {
                            "draft": draft.model_dump(),
                            "problems": [*problems, *coverage_problems],
                            "rejected_indices": {
                                field: sorted(indices) for field, indices in repair_targets.items()
                            },
                            "instruction": "Return ProviderRepair only. Accepted original items are locked and "
                            "will remain unchanged. Replace only explicitly rejected indices (zero-based within "
                            "their original arrays), or omit/null them to remove them. Put missing source-supported "
                            "requested facts in additions. Do not repeat or rewrite accepted items in additions. "
                            "Preserve exact source modal wording and all supported record details. Re-read the "
                            "original request; do not add unasked requirements or absent values.",
                        }
                        repair_count = 1
                        continue
                    if independent and coverage_problems:
                        failure = AnswerValidationError("Supported requested facts remain missing after the bounded repair.")
                        failure.problems = coverage_problems
                        raise failure
                    if problems:
                        # Preserve accepted parts without presenting rejected content or pretending an outage is missing knowledge.
                        approved = {(v.kind, v.index) for v in verdicts if v.supported}
                        statements = [
                            item for i, item in enumerate(statements) if ("statement", i) in approved
                        ]
                        next_steps = [
                            item for i, item in enumerate(next_steps) if ("next_step", i) in approved
                        ]
                        draft.gaps = [item for i, item in enumerate(draft.gaps) if ("gap", i) in approved]
                        if not statements and not draft.gaps:
                            failure = AnswerValidationError(
                                "The generated content could not be supported; no answer was published."
                            )
                            failure.problems = problems
                            raise failure
                break
        except ProviderError as exc:
            if exc.usage:
                usage.append(exc.usage)
            # The run owner persists actual completed calls even when the final answer cannot publish.
            exc.attempt_usage = [item.model_dump(mode="json") for item in usage]
            raise
        if empty_draft_fallback:
            status = "unsupported"
        elif self.check_support:
            positive_parts = {item.part_id for item in statements + next_steps}
            gap_parts = {item.part_id for item in draft.gaps}
            effective_coverage = [
                "clarification"
                if item.status == "clarification"
                else "unsupported"
                if item.part_id not in positive_parts
                else "partial"
                if item.missing_supported_facts or (item.status == "answered" and item.part_id in gap_parts)
                else item.status
                for item in coverage
            ]
            status = (
                "clarification"
                if any(value == "clarification" for value in effective_coverage)
                and all(value in {"clarification", "unsupported"} for value in effective_coverage)
                else "answered"
                if all(value == "answered" for value in effective_coverage)
                else "unsupported"
                if all(value == "unsupported" for value in effective_coverage)
                else "partial"
            )
        else:
            status = "partial" if statements and draft.gaps else "answered" if statements else "unsupported"
        if invalid_repair_units and status == "answered":
            status = "partial"
        if independent:
            missing_parts = [part for part in packet.assessment.get("parts", []) if part["missing_requests"]]
            if missing_parts and status == "answered":
                # Reporting that a requested value is unavailable does not supply that value.
                status = "partial" if statements or next_steps else "unsupported"
            unresolved = [scope for scope in packet.scopes if scope.conflicting_source_ids
                          and not set(scope.conflicting_source_ids).issubset(scope.source_reference_ids)]
            for scope in unresolved:
                draft.gaps.append(AnswerGap(part_id=scope.part_id, text=
                    "The source metadata leaves competing applicable revisions ("
                    + ", ".join(scope.conflicting_source_ids)
                    + "). Their precedence is unresolved; a single governing rule cannot be established."))
            if unresolved:
                status = "partial" if statements or next_steps else "clarification"
        return PublishedAnswer(
            status=status,
            statements=statements,
            gaps=draft.gaps,
            next_steps=next_steps,
            scopes=packet.scopes,
            index_id=packet.index_id,
            index_checksum=packet.index_checksum,
            usage=usage,
            diagnostics={
                "retrieval": packet.retrieval,
                "citation_validation": "passed",
                "citation_context_policy": "adjacent-paragraphs-v1; max_chars=1600; exact original quote retained",
                "record_assertion_policy": "source-aware-record-clarity-v1; bounded ambiguity checks, not entailment",
                "support_check": "not_run"
                if empty_draft_fallback or not self.check_support
                else "automated_pass"
                if not problems
                else "automated_partial",
                "empty_draft_fallback": empty_draft_fallback,
                "withheld_count": len(problems) + len(invalid_repair_units),
                "repair_count": repair_count,
                "ignored_locked_repair_edits": ignored_repair_edits,
                "ignored_nonexistent_support_verdicts": ignored_nonexistent_verdicts,
                "ignored_duplicate_support_verdicts": ignored_duplicate_verdicts,
                "withheld_invalid_repair_units": invalid_repair_units,
                "split_exact_citation_count": split_citation_count,
                "reused_immutable_support_verdicts": reused_support_count,
                "verifier_citation_repair_count": verifier_citation_repair_count,
                "rate_limit_retries": rate_limit_retries,
                "support_verdicts": [v.model_dump() for v in verdicts],
                "answer_coverage": [item.model_dump() for item in coverage],
                "fact_coverage": [item.model_dump() for item in review.fact_coverage]
                if self.check_support and not empty_draft_fallback else [],
                "source_review": [item.model_dump() for item in review.source_review]
                if self.check_support and not empty_draft_fallback else [],
                "prompt_id": ANSWER_PROMPT_ID,
                "checker_prompt_id": CHECKER_PROMPT_ID,
                "generator_model": getattr(self.provider, "model", None),
                "verifier_model": getattr(self.checker, "model", None),
                "independent_verification": independent and self.check_support,
                "prompt_sha256": sha256(prompt.encode()).hexdigest(),
                "checker_prompt_sha256": sha256(checker_prompt.encode()).hexdigest()
                if self.check_support
                else None,
                "total_latency_ms": round((time.perf_counter() - started) * 1000, 3),
            },
        )
