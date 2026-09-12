"""Real provider wire contract shape, independent of successful provider doubles."""

from emer.contracts.answer import SupportCheck
from emer.providers.openrouter import OpenRouterClient


def test_nested_coverage_default_is_required_on_strict_openai_wire():
    client = OpenRouterClient("not-used", "openai/gpt-5.6-luna")
    schema = client._schema(SupportCheck)
    coverage = schema["$defs"]["AnswerCoverage"]
    assert "missing_supported_facts" in coverage["required"]
    assert coverage["required"] == list(coverage["properties"])
    assert coverage["additionalProperties"] is False
    assert "default" not in coverage["properties"]["missing_supported_facts"]
    # Compatibility when loading older stored evaluations remains separate.
    assert (
        SupportCheck.model_validate(
            {
                "verdicts": [],
                "coverage": [{"part_id": "part-1", "status": "answered", "reason": "Old stored result."}],
            }
        )
        .coverage[0]
        .missing_supported_facts
        == []
    )
