"""Regress the observed upstream error inside HTTP 200 without making paid requests."""

import json

import httpx
import pytest
from emer.contracts.answer import ProviderDraft
from emer.providers import openrouter
from emer.providers.openrouter import OpenRouterClient, ProviderError
from emer.services.answering import AnsweringService
from emer.services.ingestion import ingest
from emer.services.retrieval import RetrievalService


@pytest.mark.parametrize("operation", ["generation", "embedding"])
@pytest.mark.parametrize(
    "status,code,retryable",
    [
        (429, "provider_rate_limited", True),
        (402, "provider_capacity_exhausted", False),
        (503, "provider_unavailable", True),
    ],
)
async def test_success_http_error_envelope_is_operational_with_unknown_usage(
    operation, status, code, retryable, monkeypatch
):
    monkeypatch.setattr(openrouter, "RATE_LIMIT_BACKOFF_SECONDS", (0, 0))
    calls = []

    async def handle(request):
        calls.append(request)
        return httpx.Response(
            200,
            json={
                "id": "saved-error-receipt",
                "error": {
                    "code": status,
                    "message": "secret-bearing upstream detail",
                    "metadata": {"Authorization": "private"},
                },
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        provider = OpenRouterClient("test-only-key", "model", client=client)
        with pytest.raises(ProviderError) as raised:
            if operation == "generation":
                await provider.structured("", {}, ProviderDraft)
            else:
                await provider.embed(["source"], "embedding-model")
    error = raised.value
    assert error.code == code and error.retryable is retryable
    # An unbilled rate limit is retried with bounded backoff; every other envelope is final.
    assert len(calls) == (1 + len(openrouter.RATE_LIMIT_BACKOFF_SECONDS) if status == 429 else 1)
    assert error.usage.request_id == "saved-error-receipt"
    assert error.usage.total_tokens is None and error.usage.cost is None
    assert error.response_diagnostics["upstream_error_code"] == status
    retained = str(error) + json.dumps(error.response_diagnostics)
    assert all(
        secret not in retained for secret in ["secret-bearing", "Authorization", "private", "test-only-key"]
    )


async def test_error_envelope_overrides_plausible_answer_and_preserves_actual_receipt():
    calls = 0

    async def handle(request):
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            json={
                "id": "failed-after-output",
                "error": {"code": 429},
                "usage": {"total_tokens": 19, "cost": 0.0002},
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": '{"statements":[],"gaps":[],"next_steps":[]}'},
                    }
                ],
            },
        )

    packet = RetrievalService(ingest("config/corpus.json")).evidence("PT-006 status")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(ProviderError) as raised:
            await AnsweringService(OpenRouterClient("test-only-key", "model", client=client)).answer(
                packet, "PT-006 status"
            )
    error = raised.value
    assert error.code == "provider_rate_limited" and calls == 1
    assert len(error.attempt_usage) == 1
    assert error.attempt_usage[0]["request_id"] == "failed-after-output"
    assert error.attempt_usage[0]["total_tokens"] == 19
    assert error.attempt_usage[0]["cost"] == 0.0002


async def test_http_rate_limit_is_retried_once_it_clears_and_counted(monkeypatch):
    monkeypatch.setattr(openrouter, "RATE_LIMIT_BACKOFF_SECONDS", (0, 0))
    calls = 0

    async def handle(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, json={"error": {"code": 429, "message": "slow down"}})
        return httpx.Response(
            200,
            json={
                "id": "after-backoff",
                "usage": {"total_tokens": 12, "cost": 0.0001},
                "choices": [
                    {"finish_reason": "stop", "message": {"content": '{"statements":[],"gaps":[],"next_steps":[]}'}}
                ],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        result = await OpenRouterClient("test-only-key", "model", client=client).structured("", {}, ProviderDraft)
    assert calls == 2
    assert result.rate_limit_retries == 1
    assert result.usage.request_id == "after-backoff" and result.usage.cost == 0.0001


async def test_timeout_and_billed_rate_limit_are_never_retried(monkeypatch):
    monkeypatch.setattr(openrouter, "RATE_LIMIT_BACKOFF_SECONDS", (0, 0))
    calls = 0

    async def timeout(request):
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("slow", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(timeout)) as client:
        with pytest.raises(ProviderError) as raised:
            await OpenRouterClient("test-only-key", "model", client=client).structured("", {}, ProviderDraft)
    assert raised.value.code == "provider_timeout" and calls == 1
