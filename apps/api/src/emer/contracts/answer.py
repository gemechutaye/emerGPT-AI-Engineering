"""Provider drafts are never API answers; citations are resolved by the server."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SourceDocument(Contract):
    doc_id: str
    title: str
    category: str
    version: str
    effective_date: str
    authority: str
    text: str
    sha256: str


class SourceChunk(Contract):
    chunk_id: str
    doc_id: str
    source_sha256: str
    ordinal: int = Field(ge=0)
    start: int = Field(ge=0)
    end: int = Field(gt=0)
    text: str
    section_path: list[str] = Field(default_factory=list)
    token_count: int = Field(gt=0)


class EvidencePassage(SourceChunk):
    scope_ids: list[str] = Field(default_factory=list)


class PolicyWindow(Contract):
    doc_id: str
    family: str
    valid_from: str
    valid_through: str | None
    relation: Literal["applicable", "not_yet_effective", "expired", "superseded", "conflict"]


class QuestionScope(Contract):
    part_id: str
    question: str
    patient_ids: list[str]
    unknown_patient_ids: list[str]
    as_of: str
    date_origin: Literal["question", "context", "today"]
    all_patients: bool
    patient_discovery: bool = False
    warnings: list[str]
    applicable_policy_ids: list[str] = Field(default_factory=list)
    superseded_policy_ids: list[str] = Field(default_factory=list)
    conflicting_policy_ids: list[str] = Field(default_factory=list)
    allowed_source_ids: list[str] = Field(default_factory=list)
    inapplicable_policy_ids: list[str] = Field(default_factory=list)
    policy_windows: list[PolicyWindow] = Field(default_factory=list)
    source_reference_ids: list[str] = Field(default_factory=list)
    requested_information: list[str] = Field(default_factory=list)
    applicable_source_ids: list[str] = Field(default_factory=list)
    inapplicable_source_ids: list[str] = Field(default_factory=list)
    conflicting_source_ids: list[str] = Field(default_factory=list)
    source_windows: list[dict[str, Any]] = Field(default_factory=list)


class EvidencePacket(Contract):
    index_id: str
    index_checksum: str
    corpus_checksum: str
    sources: list[SourceDocument]
    scopes: list[QuestionScope]
    retrieval: dict[str, Any]
    passages: list[EvidencePassage] = Field(default_factory=list)
    assessment: dict[str, Any] = Field(default_factory=dict)


class DraftCitation(Contract):
    doc_id: str
    quote: str = Field(min_length=1, max_length=1600)


class DraftStatement(Contract):
    part_id: str
    text: str = Field(min_length=1, max_length=1800)
    citations: list[DraftCitation] = Field(min_length=1, max_length=8)


class AnswerGap(Contract):
    part_id: str
    text: str = Field(min_length=1, max_length=1200)


class ProviderDraft(Contract):
    statements: list[DraftStatement] = Field(max_length=32)
    gaps: list[AnswerGap] = Field(max_length=16)
    next_steps: list[DraftStatement] = Field(max_length=12)


class StatementReplacement(Contract):
    index: int = Field(ge=0)
    replacement: DraftStatement | None


class GapReplacement(Contract):
    index: int = Field(ge=0)
    replacement: AnswerGap | None


class ProviderRepair(Contract):
    statement_replacements: list[StatementReplacement] = Field(default_factory=list, max_length=32)
    gap_replacements: list[GapReplacement] = Field(default_factory=list, max_length=16)
    next_step_replacements: list[StatementReplacement] = Field(default_factory=list, max_length=12)
    additions: ProviderDraft


class Citation(Contract):
    doc_id: str
    title: str
    version: str
    effective_date: str
    authority: str
    source_sha256: str
    index_id: str
    quote: str
    start: int
    end: int


class PublishedStatement(Contract):
    part_id: str
    text: str
    citations: list[Citation]


class ProviderUsage(Contract):
    operation: str
    model: str
    provider: str | None = None
    request_id: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    cost: float | None = None
    latency_ms: float


class PublishedAnswer(Contract):
    status: Literal["answered", "partial", "unsupported", "clarification"]
    statements: list[PublishedStatement]
    gaps: list[AnswerGap]
    next_steps: list[PublishedStatement]
    scopes: list[QuestionScope]
    index_id: str
    index_checksum: str
    usage: list[ProviderUsage]
    diagnostics: dict[str, Any]


class SupportVerdict(Contract):
    kind: Literal["statement", "gap", "next_step"]
    index: int = Field(ge=0)
    supported: bool
    reason: str
    citation_replacements: list[DraftCitation] = Field(default_factory=list, max_length=8)


class AnswerCoverage(Contract):
    part_id: str
    status: Literal["answered", "partial", "unsupported", "clarification"]
    reason: str
    missing_supported_facts: list[str] = Field(default_factory=list, max_length=16)
    answered_requests: list[str] = Field(default_factory=list, max_length=16)
    unanswered_requests: list[str] = Field(default_factory=list, max_length=16)


class FactCoverage(Contract):
    fact_id: str
    status: Literal["covered", "omitted", "not_supported", "not_requested"]
    statement_indices: list[int] = Field(default_factory=list, max_length=32)
    next_step_indices: list[int] = Field(default_factory=list, max_length=12)
    gap_indices: list[int] = Field(default_factory=list, max_length=16)
    reason: str


class SourceUnitReview(Contract):
    unit_id: str
    disposition: Literal["represented", "required_omission", "not_required"]
    reason: str


class SupportCheck(Contract):
    verdicts: list[SupportVerdict]
    coverage: list[AnswerCoverage]
    fact_coverage: list[FactCoverage] = Field(default_factory=list, max_length=128)
    source_review: list[SourceUnitReview] = Field(default_factory=list, max_length=160)
