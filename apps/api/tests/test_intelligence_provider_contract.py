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


async def test_complete_input_budget_rejects_before_dispatch_without_truncating_evidence():
    import pytest
    from emer.providers.openrouter import ProviderError

    class NoDispatch(OpenRouterClient):
        async def _post(self, *args):
            raise AssertionError('An over-budget request must never reach the provider')

    client = NoDispatch('unused', 'declared/model', max_input_tokens=1000)
    payload = {'evidence': '東京 exact source qualification. ' * 500}
    original = payload.copy()
    with pytest.raises(ProviderError) as failure:
        await client.structured('Check the complete evidence.', payload, SupportCheck)
    assert failure.value.code == 'model_context_budget'
    assert failure.value.usage is None
    assert failure.value.response_diagnostics['estimated_input_tokens'] > 1000
    assert payload == original


async def test_complete_input_budget_includes_output_schema_not_only_source_text():
    import pytest
    from emer.providers.openrouter import ProviderError

    client = OpenRouterClient('unused', 'declared/model', max_input_tokens=500)
    with pytest.raises(ProviderError) as failure:
        await client.structured('Check.', {}, SupportCheck)
    assert failure.value.code == 'model_context_budget'


async def test_nonreasoning_verifier_uses_supported_azure_token_parameter():
    import json

    import httpx
    from emer.contracts.answer import ProviderDraft

    seen = []
    async def handle(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {
            "content": '{"statements":[],"gaps":[],"next_steps":[]}'}}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        client = OpenRouterClient("unused", "openai/gpt-4.1-mini", client=http,
                                  reasoning_effort="none")
        await client.structured("Check source evidence.", {}, ProviderDraft, max_tokens=1000)
    assert "reasoning" not in seen[0]
    assert "max_tokens" not in seen[0]
    assert seen[0]["max_completion_tokens"] == 1000
    assert seen[0]["provider"]["only"] == ["azure"]
    assert seen[0]["provider"]["allow_fallbacks"] is False


async def test_gemini_low_reasoning_keeps_bounded_json_headroom():
    import json

    import httpx
    from emer.contracts.answer import ProviderDraft

    seen = []
    async def handle(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {
            "content": '{"statements":[],"gaps":[],"next_steps":[]}'}}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        client = OpenRouterClient("unused", "google/gemini-3.8-flash", client=http,
                                  reasoning_effort="low", reasoning_token_reserve=4096)
        await client.structured("Answer from sources.", {}, ProviderDraft, max_tokens=2200)
    assert seen[0]["reasoning"] == {"effort": "low", "exclude": True}
    assert seen[0]["max_tokens"] == 6296
