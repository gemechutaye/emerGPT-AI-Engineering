"""Carry a cited discovery entity forward as navigation context, never as evidence."""

from emer.contracts.answer import EvidencePacket, PublishedAnswer
from emer.domain.scope import PATIENT


def discovered_patient(answer: PublishedAnswer, evidence: EvidencePacket) -> str | None:
    if answer.status not in {"answered", "partial"} or answer.index_id != evidence.index_id:
        return None
    scopes = {scope.part_id: scope for scope in answer.scopes}
    sources = {source.doc_id: source for source in evidence.sources}
    mentioned = {
        f"PT-{int(match.group(1)):03d}"
        for item in [*answer.statements, *answer.next_steps, *answer.gaps]
        for match in PATIENT.finditer(item.text)
    }
    confirmed: set[str] = set()
    for statement in answer.statements:
        identifiers = {f"PT-{int(match.group(1)):03d}" for match in PATIENT.finditer(statement.text)}
        scope = scopes.get(statement.part_id)
        if not scope or not scope.patient_discovery or scope.all_patients:
            continue
        for citation in statement.citations:
            source = sources.get(citation.doc_id)
            if (
                citation.doc_id in identifiers
                and citation.doc_id in scope.allowed_source_ids
                and source is not None
                and source.category == "synthetic_patient"
                and citation.index_id == evidence.index_id
                and citation.source_sha256 == source.sha256
                and 0 <= citation.start < citation.end <= len(source.text)
                and source.text[citation.start:citation.end] == citation.quote
            ):
                confirmed.add(citation.doc_id)
    return next(iter(confirmed)) if len(mentioned) == 1 and confirmed == mentioned else None
