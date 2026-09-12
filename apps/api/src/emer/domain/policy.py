"""Policy applicability comes from reviewed source-backed intervals, never recency alone."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from emer.contracts.answer import PolicyWindow, QuestionScope
from emer.domain.source_metadata import resolve_catalog


def apply_source_metadata(scope: QuestionScope, catalog: dict[str, Any]) -> QuestionScope:
    """Attach generic applicability without treating source inspection as operational precedence.

    This resolves the complete catalog. The retrieval service intersects permissions
    with selected evidence before publication; it must not silently replace that
    intersection with all catalog sources.
    """
    resolved = resolve_catalog(
        catalog,
        scope.as_of,
        patient_ids=scope.patient_ids,
        source_reference_ids=scope.source_reference_ids,
    )
    return scope.model_copy(
        update={
            "applicable_source_ids": resolved["applicable_source_ids"],
            "inapplicable_source_ids": resolved["inapplicable_source_ids"],
            "conflicting_source_ids": resolved["conflicting_source_ids"],
            "source_windows": resolved["source_windows"],
            "allowed_source_ids": resolved["allowed_source_ids"],
            "warnings": [*scope.warnings, *resolved["warnings"]],
        }
    )


def apply_policies(scope: QuestionScope, annotations: list[dict[str, Any]]) -> QuestionScope:
    families: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for policy in annotations:
        families[policy["family"]].append(policy)
    applicable, inactive, superseded, conflicts, windows = [], [], [], [], []
    for versions in families.values():
        eligible = [
            p
            for p in versions
            if p["valid_from"] <= scope.as_of
            and (p["valid_through"] is None or scope.as_of <= p["valid_through"])
        ]
        explicitly_superseded = {previous for p in eligible for previous in p["supersedes"]}
        remaining = [p for p in eligible if p["doc_id"] not in explicitly_superseded]
        active = [p["doc_id"] for p in remaining]
        applicable.extend(active)
        inactive.extend(p["doc_id"] for p in versions if p["doc_id"] not in active)
        superseded.extend(p["doc_id"] for p in versions if p["doc_id"] in explicitly_superseded)
        if len(active) > 1:
            conflicts.extend(active)
        for policy in versions:
            relation = (
                "conflict"
                if len(active) > 1 and policy["doc_id"] in active
                else "applicable"
                if policy["doc_id"] in active
                else "not_yet_effective"
                if policy["valid_from"] > scope.as_of
                else "superseded"
                if policy["doc_id"] in explicitly_superseded
                else "expired"
            )
            windows.append(
                PolicyWindow(
                    doc_id=policy["doc_id"],
                    family=policy["family"],
                    valid_from=policy["valid_from"],
                    valid_through=policy["valid_through"],
                    relation=relation,
                )
            )
    referenced_policies = set(scope.source_reference_ids) & {p["doc_id"] for p in annotations}
    warnings = list(scope.warnings)
    if referenced_policies:
        warnings.append(
            "The explicitly requested source revisions may be described or compared as documents. "
            "This does not make their terms applicable to the lookup date; distinguish historical "
            "wording from current instructions."
        )
    return scope.model_copy(
        update={
            "applicable_policy_ids": applicable,
            "superseded_policy_ids": superseded,
            "inapplicable_policy_ids": inactive,
            "policy_windows": windows,
            "conflicting_policy_ids": conflicts,
            "warnings": warnings,
        }
    )
