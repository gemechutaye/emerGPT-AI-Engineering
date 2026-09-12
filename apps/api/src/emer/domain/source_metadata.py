"""Reviewed source applicability and precedence, independent of search and providers.

A logical ID explicitly groups competing revisions; a family groups related
documents for retrieval expansion. Neither is guessed from titles. Source record dates are retained
for provenance and become applicability dates only through an explicit annotation.
"""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import date
from typing import Any, Literal

from emer.contracts.answer import SourceDocument
from pydantic import BaseModel, ConfigDict, Field, StrictInt, ValidationError


class SourceMetadataError(ValueError):
    pass


class _MetadataModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class MetadataProvenance(_MetadataModel):
    field: Literal[
        "logical_id",
        "family",
        "date_semantics",
        "effective_from",
        "effective_until",
        "supersedes",
        "authority_domain",
        "applicability_scope",
    ]
    quote: str = Field(min_length=1)
    source_field: Literal["text", "title", "authority", "effective_date", "version"] = "text"


class ApplicabilityScope(_MetadataModel):
    kind: Literal["general", "patient"] = "general"
    patient_ids: list[str] = Field(default_factory=list)


class SourceMetadata(_MetadataModel):
    doc_id: str = Field(min_length=1)
    logical_id: str = Field(min_length=1)
    family: str = Field(min_length=1)
    version: str = Field(min_length=1)
    record_date: str
    date_semantics: Literal["effective", "snapshot", "publication"]
    effective_from: str | None = None
    effective_until: str | None = None
    supersedes: list[str] = Field(default_factory=list)
    authority: str
    authority_domain: str = "unranked"
    authority_rank: StrictInt | None = None
    applicability_scope: ApplicabilityScope = Field(default_factory=ApplicabilityScope)
    provenance: list[MetadataProvenance] = Field(default_factory=list)


def _calendar(value: str, label: str) -> date:
    try:
        parsed = date.fromisoformat(value)
        if parsed.isoformat() != value:
            raise ValueError
        return parsed
    except (TypeError, ValueError) as exc:
        raise SourceMetadataError(f"Invalid ISO date for {label}") from exc


def _date_is_quoted(value: str, quotes: list[str]) -> bool:
    parsed = _calendar(value, "applicability")
    variants = (
        value,
        f"{parsed.strftime('%B')} {parsed.day}, {parsed.year}",
        f"{parsed.day} {parsed.strftime('%B')} {parsed.year}",
    )
    return any(
        re.search(r"(?<!\w)" + re.escape(variant) + r"(?!\w)", quote, re.IGNORECASE)
        for quote in quotes
        for variant in variants
    )


def validate_catalog(
    documents: list[SourceDocument],
    source_metadata: list[dict[str, Any]],
    authority: dict[str, dict[str, int | None]] | None = None,
) -> dict[str, Any]:
    """Validate manifest annotations and return a fully serializable immutable-index catalog.

    Authority is domain -> original source authority label -> integer rank, where
    larger ranks take precedence. Missing/None ranks establish no ordering. Omitted
    sources receive singleton families and no applicability interval.
    """
    by_id = {doc.doc_id: doc for doc in documents}
    if len(by_id) != len(documents) or not by_id:
        raise SourceMetadataError("Source catalog requires nonempty unique document identifiers")
    authority = {} if authority is None else authority
    if not isinstance(authority, dict) or any(
        not isinstance(domain, str)
        or not domain
        or not isinstance(labels, dict)
        or any(
            not isinstance(label, str) or not label or (rank is not None and type(rank) is not int)
            for label, rank in labels.items()
        )
        for domain, labels in authority.items()
    ):
        raise SourceMetadataError(
            "Authority configuration must map domains and labels to integer ranks or null"
        )
    if not isinstance(source_metadata, list):
        raise SourceMetadataError("source_metadata must be a list of source annotations")
    annotations: dict[str, dict] = {}
    for annotation in source_metadata:
        if (
            not isinstance(annotation, dict)
            or not isinstance(annotation.get("doc_id"), str)
            or annotation["doc_id"] not in by_id
        ):
            raise SourceMetadataError("Metadata refers to an unknown source")
        identifier = annotation["doc_id"]
        if identifier in annotations:
            raise SourceMetadataError("Duplicate source metadata annotation")
        annotations[identifier] = annotation
    catalog: dict[str, SourceMetadata] = {}
    for identifier, doc in sorted(by_id.items()):
        annotation = annotations.get(identifier, {})
        if {"record_date", "authority", "authority_rank"} & annotation.keys():
            raise SourceMetadataError(
                "Source dates/authority and authority ranks are not source-row overrides"
            )
        defaults = {
            "doc_id": identifier,
            "logical_id": identifier,
            "family": identifier,
            "version": doc.version,
            "date_semantics": "snapshot" if doc.category == "synthetic_patient" else "publication",
            "applicability_scope": {
                "kind": "patient" if doc.category == "synthetic_patient" else "general",
                "patient_ids": [identifier] if doc.category == "synthetic_patient" else [],
            },
        }
        domain = annotation.get("authority_domain", "unranked")
        if not isinstance(domain, str) or not domain or (domain != "unranked" and domain not in authority):
            raise SourceMetadataError(
                "Source authority domain is absent from manifest authority configuration"
            )
        try:
            item = SourceMetadata.model_validate(
                {
                    **defaults,
                    **annotation,
                    "record_date": doc.effective_date,
                    "authority": doc.authority,
                    "authority_rank": authority.get(domain, {}).get(doc.authority),
                }
            )
        except ValidationError as exc:
            raise SourceMetadataError(f"Invalid source metadata for {identifier}") from exc
        _calendar(item.record_date, "source record date")
        if item.version != doc.version:
            raise SourceMetadataError("Annotated version differs from its original source")
        scope = item.applicability_scope
        if len(set(scope.patient_ids)) != len(scope.patient_ids) or (
            scope.kind == "general" and scope.patient_ids or scope.kind == "patient" and not scope.patient_ids
        ):
            raise SourceMetadataError("Invalid general/patient applicability scope")
        if any(
            patient not in by_id or by_id[patient].category != "synthetic_patient"
            for patient in scope.patient_ids
        ):
            raise SourceMetadataError("Applicability refers to an unknown patient record")
        if doc.category == "synthetic_patient" and (
            scope.kind != "patient" or scope.patient_ids != [identifier]
        ):
            raise SourceMetadataError("A patient record cannot widen or replace its subject scope")
        quotes: dict[str, list[str]] = defaultdict(list)
        for evidence in item.provenance:
            if evidence.quote not in getattr(doc, evidence.source_field):
                raise SourceMetadataError("Metadata provenance is not an exact original source passage")
            quotes[evidence.field].append(evidence.quote)
        if item.date_semantics != "effective" and (item.effective_from or item.effective_until):
            raise SourceMetadataError("Snapshot/publication dates cannot become applicability intervals")
        if item.date_semantics == "effective" and not item.effective_from:
            raise SourceMetadataError("Effective metadata requires an explicit effective_from date")
        for field in ("effective_from", "effective_until"):
            value = getattr(item, field)
            if value and not _date_is_quoted(value, quotes[field]):
                raise SourceMetadataError(f"{field} has no matching date in its source provenance")
        if item.effective_until and item.effective_until < item.effective_from:
            raise SourceMetadataError("Applicability ends before it begins")
        if len(set(item.supersedes)) != len(item.supersedes):
            raise SourceMetadataError("Duplicate supersession edge")
        for previous in item.supersedes:
            if not any(
                re.search(r"(?<![\w-])" + re.escape(previous) + r"(?![\w-])", quote)
                for quote in quotes["supersedes"]
            ):
                raise SourceMetadataError("Supersession edge has no exact source identifier provenance")
        catalog[identifier] = item
    for item in catalog.values():
        for previous in item.supersedes:
            target = catalog.get(previous)
            if target is None:
                raise SourceMetadataError("Supersession target is not in the source catalog")
            if (
                item.logical_id != target.logical_id
                or item.family != target.family
                or item.authority_domain != target.authority_domain
            ):
                raise SourceMetadataError(
                    "Supersession cannot cross a logical record, family or authority domain"
                )
            if item.applicability_scope != target.applicability_scope:
                raise SourceMetadataError("Supersession cannot cross applicability subjects")
            if not item.effective_from or not target.effective_from:
                raise SourceMetadataError(
                    "Supersession requires explicit applicability dates on both revisions"
                )
            if item.effective_from < target.effective_from:
                raise SourceMetadataError("A revision cannot supersede a later-effective source")
            if (
                item.authority_rank is not None
                and target.authority_rank is not None
                and item.authority_rank < target.authority_rank
            ):
                raise SourceMetadataError("A lower authority cannot supersede a higher authority")
    visiting, complete = set(), set()

    def visit(identifier: str) -> None:
        if identifier in visiting:
            raise SourceMetadataError("Source supersession graph contains a cycle")
        if identifier in complete:
            return
        visiting.add(identifier)
        for previous in catalog[identifier].supersedes:
            visit(previous)
        visiting.remove(identifier)
        complete.add(identifier)

    for identifier in catalog:
        visit(identifier)
    return {
        "schema_version": 1,
        "sources": [item.model_dump(mode="json") for item in catalog.values()],
        "authority": authority,
    }


def resolve_catalog(
    catalog: dict[str, Any],
    as_of: str,
    *,
    patient_ids: list[str] | None = None,
    source_reference_ids: list[str] | tuple[str, ...] = (),
) -> dict[str, Any]:
    """Resolve a validated catalog at one date; document inspection never changes applicability."""
    _calendar(as_of, "lookup")
    sources = {row["doc_id"]: SourceMetadata.model_validate(row) for row in catalog["sources"]}
    requested_patients = set(patient_ids or [])
    relations: dict[str, str] = {}
    candidates: dict[tuple, list[SourceMetadata]] = defaultdict(list)
    for item in sources.values():
        scope = item.applicability_scope
        if scope.kind == "patient" and not set(scope.patient_ids).issubset(requested_patients):
            relations[item.doc_id] = "out_of_scope"
        else:
            candidates[
                (item.logical_id, item.authority_domain, scope.kind, tuple(sorted(scope.patient_ids)))
            ].append(item)

    def ancestors(identifier: str, prior: set[str]) -> None:
        for previous in sources[identifier].supersedes:
            if previous not in prior:
                prior.add(previous)
                ancestors(previous, prior)

    for family in candidates.values():
        # A replacement remains a replacement after its own expiry; old versions
        # never revive merely because their replacement expires. Future revisions
        # cannot supersede anything before their own effective date.
        prior: set[str] = set()

        for item in family:
            if item.effective_from and item.effective_from <= as_of:
                ancestors(item.doc_id, prior)
        remaining = []
        for item in family:
            if item.effective_from and item.effective_from > as_of:
                relations[item.doc_id] = "not_yet_effective"
            elif item.doc_id in prior:
                relations[item.doc_id] = "superseded"
            elif item.effective_until and item.effective_until < as_of:
                relations[item.doc_id] = "expired"
            else:
                remaining.append(item)
        if not remaining:
            continue
        ranks_comparable = len({item.authority_domain for item in remaining}) == 1 and all(
            item.authority_rank is not None for item in remaining
        )
        highest = max(item.authority_rank for item in remaining) if ranks_comparable else None
        winners = []
        for item in remaining:
            if ranks_comparable and item.authority_rank < highest:
                relations[item.doc_id] = "lower_authority"
            else:
                winners.append(item)
        for item in winners:
            relations[item.doc_id] = "conflict" if len(winners) > 1 else "applicable"
    by_relation = {
        relation: sorted(identifier for identifier, value in relations.items() if value == relation)
        for relation in set(relations.values())
    }
    applicable = by_relation.get("applicable", []) + by_relation.get("conflict", [])
    inspection = sorted(
        identifier
        for identifier in set(source_reference_ids) & sources.keys()
        if relations[identifier] != "out_of_scope"
    )
    windows = [
        {
            "doc_id": item.doc_id,
            "logical_id": item.logical_id,
            "family": item.family,
            "authority_domain": item.authority_domain,
            "authority_rank": item.authority_rank,
            "effective_from": item.effective_from,
            "effective_until": item.effective_until,
            "record_date": item.record_date,
            "date_semantics": item.date_semantics,
            "relation": relations[item.doc_id],
        }
        for item in sources.values()
    ]
    warnings = []
    if by_relation.get("conflict"):
        warnings.append(
            "Competing source revisions have no unique applicable precedence; preserve the conflict."
        )
    if inspection:
        warnings.append(
            "Explicitly requested source revisions may be inspected as documents; inspection does not "
            "make expired, future, superseded or lower-authority instructions applicable."
        )
    return {
        "applicable_source_ids": sorted(applicable),
        "inapplicable_source_ids": sorted(set(sources) - set(applicable)),
        "superseded_source_ids": by_relation.get("superseded", []),
        "lower_authority_source_ids": by_relation.get("lower_authority", []),
        "conflicting_source_ids": by_relation.get("conflict", []),
        "inspection_source_ids": inspection,
        "allowed_source_ids": sorted(set(applicable) | set(inspection)),
        "source_windows": windows,
        "warnings": warnings,
    }


def policy_annotations(catalog: dict[str, Any]) -> list[dict[str, Any]]:
    """Project explicitly effective sources for legacy family expansion/diagnostic consumers.

    Generic precedence consumers must use resolve_catalog: the legacy policy schema
    cannot represent lower-authority or undated-source relations.
    """
    return [
        {
            "doc_id": row["doc_id"],
            "family": row["family"],
            "valid_from": row["effective_from"],
            "valid_through": row["effective_until"],
            "supersedes": row["supersedes"],
            "provenance_quote": " ".join(
                evidence["quote"]
                for evidence in row["provenance"]
                if evidence["field"] in {"effective_from", "effective_until"}
            ),
            "supersession_quote": " ".join(
                evidence["quote"] for evidence in row["provenance"] if evidence["field"] == "supersedes"
            ),
        }
        for row in catalog["sources"]
        if row["date_semantics"] == "effective"
    ]
