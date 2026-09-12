"""Bounded reference memory. Saved answers identify referents; they are never RAG evidence."""

from __future__ import annotations

import re
from hashlib import sha256
from typing import Any, Literal

from emer.contracts.answer import EvidencePacket, PublishedAnswer
from emer.domain.chunking import count_tokens
from emer.domain.scope import ScopeError, dismissed_context_question, patient_ids
from pydantic import BaseModel, ConfigDict, Field, ValidationError

TOPIC_RESET = re.compile(
    r"^\s*(?:separate\s+(?:issue|question)|new\s+topic|unrelated\s+question)(?:\s+now)?\s*[:,.!—-]\s*(.+)$",
    re.IGNORECASE | re.DOTALL,
)


def starts_new_topic(question: str) -> bool:
    try:
        return bool(TOPIC_RESET.fullmatch(question) or dismissed_context_question(question) is not None)
    except ScopeError:
        return False


class StateModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ReferenceCitation(StateModel):
    doc_id: str
    title: str
    version: str
    index_id: str
    source_sha256: str
    start: int
    end: int
    quote: str


class DialogueReference(StateModel):
    reference_id: str
    run_id: str
    kind: Literal["user_intent", "supported_assistant"]
    text: str
    patient_ids: list[str] = Field(default_factory=list)
    citations: list[ReferenceCitation] = Field(default_factory=list)


class DialogueTurn(StateModel):
    topic_epoch: int = 0
    run_id: str
    user_question: str
    user_intent: DialogueReference
    assistant_references: list[DialogueReference] = Field(default_factory=list)
    # A gap describes a prior unanswered request, never an established absence or fact.
    unresolved_requests: list[str] = Field(default_factory=list)


class DialogueState(StateModel):
    schema_version: Literal[1] = 1
    conversation_id: str | None = None
    context_version: int | None = None
    turns: list[DialogueTurn] = Field(default_factory=list)
    active_topic_epoch: int = 0
    truncated: bool = False

    def references(self) -> dict[str, DialogueReference]:
        return {
            reference.reference_id: reference
            for turn in self.turns
            for reference in [turn.user_intent, *turn.assistant_references]
        }


class ReferenceBinding(StateModel):
    mention: str = Field(min_length=1, max_length=1000)
    target: str = Field(min_length=1, max_length=200)
    reference_id: str
    mode: Literal["replace", "qualify"] = "replace"


class ReferenceResolution(StateModel):
    status: Literal["resolved", "independent", "ambiguous"]
    # Null means preserve the latest question. An older user intent is permitted only for a correction.
    base_reference_id: str | None
    bindings: list[ReferenceBinding] = Field(max_length=12)
    topic_anchor: str | None = Field(default=None, min_length=3, max_length=200)


class CurrentQuestionReferences(ReferenceResolution):
    """Ordinary follow-ups can bind references but cannot replace the current task."""

    base_reference_id: None


def _get(row: Any, field: str, default: Any = None) -> Any:
    return row.get(field, default) if isinstance(row, dict) else getattr(row, field, default)


def _verified_assistant_references(row: Any) -> list[DialogueReference]:
    """Recheck frozen provenance instead of trusting saved prose or a caller's 'supported' flag."""
    try:
        answer = PublishedAnswer.model_validate(_get(row, "answer"))
        evidence = EvidencePacket.model_validate(_get(row, "evidence"))
    except (ValidationError, TypeError):
        return []
    if (
        answer.status not in {"answered", "partial"}
        or answer.index_id != evidence.index_id
        or answer.index_checksum != evidence.index_checksum
    ):
        return []
    sources = {source.doc_id: source for source in evidence.sources}
    scopes = {scope.part_id: scope for scope in evidence.scopes}
    references = []
    for number, statement in enumerate(answer.statements):
        scope = scopes.get(statement.part_id)
        if not scope or not statement.citations:
            continue
        citations = []
        for citation in statement.citations:
            source = sources.get(citation.doc_id)
            if (
                source is None
                or citation.doc_id not in scope.allowed_source_ids
                or citation.index_id != evidence.index_id
                or citation.source_sha256 != source.sha256
                or sha256(source.text.encode()).hexdigest() != source.sha256
                or citation.version != source.version
                or citation.title != source.title
                or citation.effective_date != source.effective_date
                or citation.authority != source.authority
                or not 0 <= citation.start < citation.end <= len(source.text)
                or source.text[citation.start:citation.end] != citation.quote
            ):
                break
            citations.append(ReferenceCitation(**citation.model_dump(exclude={"authority", "effective_date"})))
        else:
            identifiers = patient_ids(statement.text)
            # Patient identities in an assistant statement need the original patient citation.
            if set(identifiers) - {citation.doc_id for citation in citations}:
                continue
            run_id = str(_get(row, "id"))
            references.append(DialogueReference(
                reference_id=f"{run_id}:statement:{number}", run_id=run_id,
                kind="supported_assistant", text=statement.text,
                patient_ids=identifiers, citations=citations,
            ))
    return references


def build_dialogue_state(
    rows: list[Any], *, conversation_id: str | None = None, context_version: int | None = None,
    max_turns: int = 6, max_tokens: int = 3500,
) -> DialogueState:
    """Build from chronological completed runs within a serialized cl100k_base token budget.

    The count includes JSON overhead. Oversized assistant units are omitted,
    never clipped into misleading statements. DB callers must filter conversation and context version;
    this boundary rejects mixed input rather than accidentally blending memory from different owners.
    """
    if max_turns < 1 or max_tokens < 128:
        raise ValueError("Dialogue state needs a positive turn limit and at least 128 tokens")
    conversations = {str(_get(row, "conversation_id")) for row in rows if _get(row, "conversation_id") is not None}
    versions = {_get(row, "context_version") for row in rows if _get(row, "context_version") is not None}
    if conversation_id is not None:
        conversations.add(conversation_id)
    if context_version is not None:
        versions.add(context_version)
    if len(conversations) > 1 or len(versions) > 1:
        raise ValueError("Dialogue state cannot mix conversations or context versions")
    state = DialogueState(
        conversation_id=next(iter(conversations), None), context_version=next(iter(versions), None),
    )
    eligible = [row for row in rows if _get(row, "status") == "completed"
                and (_get(row, "answer") or {}).get("status") != "clarification"]
    epochs = []
    epoch = 0
    for row in eligible:
        epoch += int(starts_new_topic(str(_get(row, "question", ""))))
        epochs.append(epoch)
    state.active_topic_epoch = epoch
    state.truncated = len(eligible) > max_turns
    for row, epoch in reversed(list(zip(eligible, epochs))[-max_turns:]):
        run_id = str(_get(row, "id"))
        context = _get(row, "context") or {}
        original = str(_get(row, "question", "")).strip()
        intent = str(context.get("resolved_question") or original).strip()
        if not original or not intent:
            continue
        try:
            referenced_patients = patient_ids(intent)
        except ScopeError:
            # Historical unresolved input remains auditable on its run; it cannot
            # become a usable reference or prevent admission of a new question.
            state.truncated = True
            continue
        turn = DialogueTurn(
            run_id=run_id, topic_epoch=epoch, user_question=original,
            user_intent=DialogueReference(reference_id=f"{run_id}:user", run_id=run_id,
                                         kind="user_intent", text=intent, patient_ids=referenced_patients),
        )
        state.turns.insert(0, turn)
        if count_tokens(state.model_dump_json()) > max_tokens:
            state.turns.pop(0)
            state.truncated = True
            break
        for reference in _verified_assistant_references(row):
            turn.assistant_references.append(reference)
            if count_tokens(state.model_dump_json()) > max_tokens:
                turn.assistant_references.pop()
                state.truncated = True
        for gap in (_get(row, "answer") or {}).get("gaps", []):
            text = gap.get("text", "")
            if not text:
                continue
            turn.unresolved_requests.append(text)
            if count_tokens(state.model_dump_json()) > max_tokens:
                turn.unresolved_requests.pop()
                state.truncated = True
    return state


def append_user_intent(state: DialogueState, question: str, reference_id: str, max_tokens=3500) -> DialogueState:
    """Represent an admitted but unfinished spoken request without treating its answer as fact."""
    state = state.model_copy(deep=True)
    if not question.strip() or (state.turns and state.turns[-1].user_intent.text == question.strip()):
        return state
    try:
        referenced_patients = patient_ids(question)
    except ScopeError:
        return state
    state.active_topic_epoch += int(starts_new_topic(question))
    state.turns.append(DialogueTurn(
        run_id=reference_id, topic_epoch=state.active_topic_epoch, user_question=question.strip(),
        user_intent=DialogueReference(reference_id=reference_id + ":user", run_id=reference_id,
                                     kind="user_intent", text=question.strip(), patient_ids=referenced_patients),
    ))
    while state.turns and (len(state.turns) > 6 or count_tokens(state.model_dump_json()) > max_tokens):
        state.turns.pop(0)
        state.truncated = True
    return state
