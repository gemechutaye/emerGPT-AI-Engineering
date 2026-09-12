from types import SimpleNamespace

import pytest
from emer.contracts.answer import ProviderDraft, SourceUnitReview
from emer.providers.openrouter import ProviderError
from emer.services.source_coverage import source_audit_units, source_coverage_problems
from test_evidence_assessment import packet


def draft():
    return ProviderDraft.model_validate({"statements": [{"part_id": "part-1", "text": "Calibration is complete.",
        "citations": [{"doc_id": "OPS-901", "quote": "Calibration is complete."}]}], "gaps": [], "next_steps": []})


def test_audit_covers_source_context_not_just_the_model_selected_citation():
    evidence = packet()
    units = source_audit_units(evidence, draft())
    assert [unit["text"] for unit in units] == ["Calibration is complete.", "Packaging remains unfinished.", "Release requires safety clearance."]
    assert len({unit["unit_id"] for unit in units}) == len(units)
    assert all(evidence.sources[0].text[unit["start"]:unit["end"]] == unit["text"] for unit in units)
    review = SimpleNamespace(source_review=[SourceUnitReview(unit_id=unit["unit_id"],
        disposition="required_omission" if "requires" in unit["text"] else "represented", reason="Declared source requirement.") for unit in units])
    assert source_coverage_problems(evidence, draft(), review) == [
        "coverage part-1: Missing source context or qualification: Release requires safety clearance. (Declared source requirement.)"]


@pytest.mark.parametrize("mutation", ["missing", "duplicate", "foreign"])
def test_incomplete_or_fabricated_source_audit_cannot_approve_coverage(mutation):
    evidence = packet()
    values = [SourceUnitReview(unit_id=unit["unit_id"], disposition="represented", reason="Declared fixture.")
              for unit in source_audit_units(evidence, draft())]
    if mutation == "missing":
        values.pop()
    elif mutation == "duplicate":
        values.append(values[0])
    else:
        values[0].unit_id = "foreign"
    with pytest.raises(ProviderError, match="every cited-source unit"):
        source_coverage_problems(evidence, draft(), SimpleNamespace(source_review=values))


def test_audit_does_not_read_unretrieved_tail_or_other_parts():
    visible = "Calibration is complete."
    evidence = packet(text=visible + " Hidden release rule.", selection=len(visible), parts=2)
    units = source_audit_units(evidence, draft())
    assert len(units) == 1 and units[0]["part_id"] == "part-1"
    assert units[0]["text"] == visible


def test_exact_short_field_quotes_are_valid_evidence():
    from emer.services.answering import validate_draft
    evidence = packet(text="Age: 41")
    value = ProviderDraft.model_validate({"statements": [{"part_id": "part-1", "text": "The documented age is 41.",
        "citations": [{"doc_id": "OPS-901", "quote": "Age: 41"}]}], "gaps": [], "next_steps": []})
    published, _ = validate_draft(evidence, value)
    assert published[0].citations[0].quote == "Age: 41"
