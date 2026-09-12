"""Source-independent metadata fixtures exercise precedence, dates and provenance."""

import json
from hashlib import sha256

import pytest
from emer.contracts.answer import SourceDocument
from emer.domain.policy import apply_policies, apply_source_metadata
from emer.domain.scope import resolve_scopes
from emer.domain.source_metadata import (
    SourceMetadataError,
    policy_annotations,
    resolve_catalog,
    validate_catalog,
)


def source(identifier, *, start="2026-01-01", end=None, supersedes=(), authority="Local", patient=False):
    text = f"Effective from {start}."
    if end:
        text += f" Effective until {end}."
    if supersedes:
        text += " This revision supersedes " + ", ".join(supersedes) + "."
    return SourceDocument(
        doc_id=identifier,
        title=f"Original source {identifier}",
        category="synthetic_patient" if patient else "operations",
        version="1.0",
        effective_date=start,
        authority="Synthetic Patient Source" if patient else authority,
        text=text,
        sha256=sha256(text.encode()).hexdigest(),
    )


def annotation(doc, *, logical_id="rule-7", family="operations", end=None, supersedes=(), domain="unranked"):
    provenance = [{"field": "effective_from", "quote": doc.text}]
    if end:
        provenance.append({"field": "effective_until", "quote": doc.text})
    if supersedes:
        provenance.append({"field": "supersedes", "quote": doc.text})
    return {
        "doc_id": doc.doc_id,
        "logical_id": logical_id,
        "family": family,
        "date_semantics": "effective",
        "effective_from": doc.effective_date,
        "effective_until": end,
        "supersedes": list(supersedes),
        "authority_domain": domain,
        "provenance": provenance,
    }


def test_neutral_sources_do_not_acquire_effective_dates_from_record_metadata():
    a, b = source("DOC-X", start="2027-04-02"), source("DOC-Y", start="2026-08-08")
    catalog = validate_catalog([b, a], [])
    resolved = resolve_catalog(catalog, "2020-01-01")
    assert resolved["applicable_source_ids"] == ["DOC-X", "DOC-Y"]
    assert all(row["effective_from"] is None for row in catalog["sources"])
    assert catalog["sources"][0]["record_date"] == "2027-04-02"
    assert json.loads(json.dumps(catalog)) == catalog


def test_family_is_expansion_group_not_automatic_conflict():
    a, b = source("RULE-A"), source("GUIDE-B")
    catalog = validate_catalog(
        [a, b],
        [
            annotation(a, logical_id="one-rule"),
            annotation(b, logical_id="another-rule"),
        ],
    )
    resolved = resolve_catalog(catalog, "2026-03-01")
    assert resolved["conflicting_source_ids"] == []
    assert resolved["applicable_source_ids"] == ["GUIDE-B", "RULE-A"]


def test_date_and_version_never_create_implicit_precedence():
    a = source("OLD", start="2025-01-01")
    b = source("NEW", start="2026-01-01").model_copy(update={"version": "999.0"})
    catalog = validate_catalog([a, b], [annotation(a), annotation(b)])
    resolved = resolve_catalog(catalog, "2026-03-01")
    assert resolved["conflicting_source_ids"] == ["NEW", "OLD"]
    assert set(resolved["allowed_source_ids"]) == {"NEW", "OLD"}


@pytest.mark.parametrize(
    "ranks,expected,conflicts",
    [
        ({"Local": 1, "Approved": 5}, ["B"], []),
        ({"Local": 5, "Approved": 5}, ["A", "B"], ["A", "B"]),
        ({"Local": 5, "Approved": None}, ["A", "B"], ["A", "B"]),
        ({"Local": 5}, ["A", "B"], ["A", "B"]),
    ],
)
def test_authority_rank_is_explicit_and_ties_or_unknown_ranks_remain_conflicts(ranks, expected, conflicts):
    a, b = source("A"), source("B", authority="Approved")
    catalog = validate_catalog(
        [a, b], [annotation(a, domain="practice"), annotation(b, domain="practice")], {"practice": ranks}
    )
    resolved = resolve_catalog(catalog, "2026-03-01")
    assert resolved["applicable_source_ids"] == expected
    assert resolved["conflicting_source_ids"] == conflicts
    assert resolved["lower_authority_source_ids"] == (["A"] if expected == ["B"] else [])


def test_domains_are_independent_and_ranks_do_not_cross_them():
    a, b = source("A"), source("B")
    catalog = validate_catalog(
        [a, b],
        [annotation(a, domain="practice"), annotation(b, domain="vendor")],
        {"practice": {"Local": 1}, "vendor": {"Local": 99}},
    )
    resolved = resolve_catalog(catalog, "2026-03-01")
    assert resolved["applicable_source_ids"] == ["A", "B"]
    assert resolved["lower_authority_source_ids"] == []


def test_explicit_supersession_respects_effective_boundary_and_legacy_projection():
    a = source("A", end="2026-06-30")
    b = source("B", start="2026-07-01", supersedes=["A"])
    catalog = validate_catalog(
        [a, b],
        [
            annotation(a, end="2026-06-30"),
            annotation(b, supersedes=["A"]),
        ],
    )
    assert resolve_catalog(catalog, "2026-06-30")["applicable_source_ids"] == ["A"]
    assert resolve_catalog(catalog, "2026-07-01")["applicable_source_ids"] == ["B"]
    assert resolve_catalog(catalog, "2026-07-01")["superseded_source_ids"] == ["A"]
    policies = policy_annotations(catalog)
    assert policies[1]["supersedes"] == ["A"]
    scope = resolve_scopes("What rule applies?", [a, b], as_of="2026-07-01")[0]
    assert apply_policies(scope, policies).applicable_policy_ids == ["B"]


def test_transitive_replacement_does_not_revive_after_successor_expiry():
    a = source("A")
    b = source("B", start="2026-02-01", supersedes=["A"])
    c = source("C", start="2026-03-01", end="2026-03-31", supersedes=["B"])
    catalog = validate_catalog(
        [a, b, c],
        [
            annotation(a),
            annotation(b, supersedes=["A"]),
            annotation(c, end="2026-03-31", supersedes=["B"]),
        ],
    )
    march = resolve_catalog(catalog, "2026-03-01")
    assert march["applicable_source_ids"] == ["C"]
    assert march["superseded_source_ids"] == ["A", "B"]
    april = resolve_catalog(catalog, "2026-04-01")
    assert april["applicable_source_ids"] == []
    assert april["superseded_source_ids"] == ["A", "B"]


def test_inspection_preserves_operational_inapplicability():
    a = source("OLD", end="2026-01-31")
    b = source("FUTURE", start="2027-01-01")
    catalog = validate_catalog([a, b], [annotation(a, end="2026-01-31"), annotation(b)])
    resolved = resolve_catalog(catalog, "2026-08-01", source_reference_ids=["OLD", "FUTURE"])
    assert resolved["applicable_source_ids"] == []
    assert resolved["inspection_source_ids"] == ["FUTURE", "OLD"]
    assert {row["relation"] for row in resolved["source_windows"]} == {"expired", "not_yet_effective"}
    assert "does not" in resolved["warnings"][0]


def test_patient_snapshot_remains_queryable_for_historical_lookup_without_becoming_encounter_date():
    patient = source("PT-451", start="2026-09-01", patient=True)
    other = source("PT-452", patient=True)
    catalog = validate_catalog([patient, other], [])
    resolved = resolve_catalog(catalog, "2026-01-01", patient_ids=["PT-451"], source_reference_ids=["PT-452"])
    assert resolved["allowed_source_ids"] == ["PT-451"]
    assert resolved["inspection_source_ids"] == []
    row = next(row for row in resolved["source_windows"] if row["doc_id"] == "PT-451")
    assert row["date_semantics"] == "snapshot"
    assert row["effective_from"] is None
    assert row["record_date"] == "2026-09-01"


@pytest.mark.parametrize(
    "scope",
    [
        {"kind": "general", "patient_ids": []},
        {"kind": "patient", "patient_ids": ["PT-452"]},
    ],
)
def test_patient_record_cannot_widen_or_replace_its_subject(scope):
    patient, other = source("PT-451", patient=True), source("PT-452", patient=True)
    with pytest.raises(SourceMetadataError, match="patient record"):
        validate_catalog([patient, other], [{"doc_id": patient.doc_id, "applicability_scope": scope}])


@pytest.mark.parametrize(
    "field,value",
    [
        ("family", "other-family"),
        ("logical_id", "other-rule"),
        ("authority_domain", "other-domain"),
    ],
)
def test_supersession_cannot_cross_logical_family_or_domain(field, value):
    a, b = source("A"), source("B", supersedes=["A"])
    later = {**annotation(b, supersedes=["A"]), field: value}
    with pytest.raises(SourceMetadataError, match="cannot cross"):
        validate_catalog([a, b], [annotation(a), later], {"other-domain": {"Local": 1}})


def test_supersession_cannot_cross_patient_scope():
    patient = source("PT-451", patient=True)
    a, b = source("A"), source("B", supersedes=["A"])
    later = {
        **annotation(b, supersedes=["A"]),
        "applicability_scope": {"kind": "patient", "patient_ids": [patient.doc_id]},
    }
    with pytest.raises(SourceMetadataError, match="subjects"):
        validate_catalog([a, b, patient], [annotation(a), later])


@pytest.mark.parametrize(
    "identifiers,edges",
    [
        (["A"], {"A": ["A"]}),
        (["A", "B"], {"A": ["B"], "B": ["A"]}),
        (["A", "B", "C"], {"A": ["B"], "B": ["C"], "C": ["A"]}),
    ],
)
def test_supersession_graph_rejects_cycles(identifiers, edges):
    docs = [source(identifier, supersedes=edges[identifier]) for identifier in identifiers]
    with pytest.raises(SourceMetadataError, match="cycle"):
        validate_catalog(docs, [annotation(doc, supersedes=edges[doc.doc_id]) for doc in docs])


def test_supersession_rejects_missing_targets_and_lower_authority_override():
    a = source("A", supersedes=["MISSING"])
    with pytest.raises(SourceMetadataError, match="target"):
        validate_catalog([a], [annotation(a, supersedes=["MISSING"])])
    a, b = source("A", authority="Approved"), source("B", supersedes=["A"])
    with pytest.raises(SourceMetadataError, match="lower authority"):
        validate_catalog(
            [a, b],
            [annotation(a, domain="practice"), annotation(b, supersedes=["A"], domain="practice")],
            {"practice": {"Local": 1, "Approved": 5}},
        )


def test_date_provenance_is_exact_and_expiry_must_follow_start():
    a = source("A", end="2026-02-01")
    invalid = annotation(a, end="2026-02-01")
    invalid["effective_until"] = "2026-02-02"
    with pytest.raises(SourceMetadataError, match="source provenance"):
        validate_catalog([a], [invalid])
    a = source("A", end="2025-12-31")
    with pytest.raises(SourceMetadataError, match="ends before"):
        validate_catalog([a], [annotation(a, end="2025-12-31")])


@pytest.mark.parametrize("semantics", ["snapshot", "publication"])
def test_record_date_semantics_cannot_carry_effective_interval(semantics):
    a = source("A")
    with pytest.raises(SourceMetadataError, match="cannot become"):
        validate_catalog([a], [{**annotation(a), "date_semantics": semantics}])


def test_earlier_revision_cannot_supersede_future_revision():
    a = source("A", start="2027-01-01")
    b = source("B", supersedes=["A"])
    with pytest.raises(SourceMetadataError, match="later-effective"):
        validate_catalog([a, b], [annotation(a), annotation(b, supersedes=["A"])])


def test_long_form_date_provenance_and_original_date_field_are_supported_explicitly():
    a = source("A").model_copy(update={"text": "Effective January 1, 2026 through June 30, 2026."})
    metadata = annotation(a, end="2026-06-30")
    assert validate_catalog([a], [metadata])["sources"][0]["effective_until"] == "2026-06-30"
    metadata["provenance"][0] = {
        "field": "effective_from",
        "quote": "2026-01-01",
        "source_field": "effective_date",
    }
    assert validate_catalog([a], [metadata])["sources"][0]["effective_from"] == "2026-01-01"


@pytest.mark.parametrize(
    "change",
    [
        {"version": "2"},
        {"record_date": "2026-01-01"},
        {"authority_rank": 9},
        {"authority": "Approved"},
        {"unexpected": True},
        {"authority_domain": []},
        {"provenance": [{"field": "family", "quote": "Invented source text."}]},
    ],
)
def test_metadata_cannot_override_original_fields_or_invent_provenance(change):
    a = source("A")
    with pytest.raises(SourceMetadataError):
        validate_catalog([a], [{"doc_id": "A", **change}])


@pytest.mark.parametrize("authority", [[], {"x": []}, {"x": {"Local": True}}, {"x": {"Local": "10"}}])
def test_authority_manifest_rejects_noninteger_or_malformed_ranks(authority):
    with pytest.raises(SourceMetadataError):
        validate_catalog([source("A")], [], authority)


def test_scope_wrapper_preserves_original_warnings_and_generic_permissions():
    a = source("DOC-A")
    scope = resolve_scopes("Read DOC-A.", [a], as_of="2026-01-01")[0]
    scope.warnings.append("Existing warning")
    result = apply_source_metadata(scope, validate_catalog([a], []))
    assert result.applicable_source_ids == ["DOC-A"]
    assert result.allowed_source_ids == ["DOC-A"]
    assert "Existing warning" in result.warnings
