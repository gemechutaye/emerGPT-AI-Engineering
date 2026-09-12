"""Bounded asynchronous OpenRouter requests. No model fallback; no retry of uncertain attempts.

The only automatic retry is a short backoff after a rate-limit rejection, which is a certain,
unbilled failure with no completion; timeouts and other uncertain outcomes are never repeated.
"""

from __future__ import annotations

import asyncio
import json
import math
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeVar

import httpx
from emer.contracts.answer import ProviderUsage
from pydantic import BaseModel, ValidationError

T = TypeVar("T", bound=BaseModel)
ROUTE_CONFIG = json.loads(Path(__file__).with_name("routes.json").read_text())
ERROR_CODES = {
    400: "provider_request_invalid",
    401: "provider_authentication",
    402: "provider_capacity_exhausted",
    403: "provider_forbidden",
    404: "provider_model_unavailable",
    429: "provider_rate_limited",
}
RATE_LIMIT_BACKOFF_SECONDS = (1.5, 3.0)


class ProviderError(RuntimeError):
    def __init__(self, code: str, message: str, retryable: bool = False, usage: ProviderUsage | None = None):
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.usage = usage
        self.attempt_usage: list[dict[str, Any]] = []
        self.response_diagnostics: dict[str, Any] = {}


@dataclass(frozen=True)
class StructuredResult:
    value: BaseModel
    usage: ProviderUsage
    rate_limit_retries: int = 0


@dataclass(frozen=True)
class EmbeddingResult:
    model: str
    vectors: list[list[float]]
    usage: ProviderUsage
    rate_limit_retries: int = 0


class OpenRouterClient:
    def __init__(
        self,
        api_key: str | None,
        model: str,
        provider_order: list[str] | None = None,
        client: httpx.AsyncClient | None = None,
        reasoning_effort: str | None = None,
    ):
        self.api_key = api_key
        self.model = model
        self.provider_order = provider_order
        self.client = client
        self.reasoning_effort = reasoning_effort

    def _routing(self, model: str | None = None) -> dict[str, Any]:
        routing: dict[str, Any] = {
            "allow_fallbacks": False,
            "require_parameters": True,
            "data_collection": "deny",
        }
        order = self.provider_order or ROUTE_CONFIG["routes"].get(model or self.model, {}).get("endpoints")
        if order:
            routing["order"] = order
            routing["only"] = order
        return routing

    async def _post(self, path: str, body: dict[str, Any]) -> tuple[dict[str, Any], float]:
        if not self.api_key:
            raise ProviderError(
                "provider_unconfigured", "OpenRouter is not configured. Your question has been preserved."
            )
        if not body.get("model"):
            raise ProviderError("model_unconfigured", "A verified provider model must be configured.")
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "X-OpenRouter-Title": "EMER synthetic-data demo",
            "X-OpenRouter-Cache": "false",
        }
        started = time.perf_counter()
        owned = self.client is None
        client = self.client or httpx.AsyncClient(timeout=httpx.Timeout(75, connect=10))
        try:
            response = await client.post("https://openrouter.ai/api/v1/" + path, headers=headers, json=body)
        except httpx.TimeoutException as exc:
            raise ProviderError(
                "provider_timeout",
                "The provider timed out. No answer was published; the provider may have processed the request.",
            ) from exc
        except httpx.HTTPError as exc:
            raise ProviderError(
                "provider_unavailable",
                "The provider could not be reached. No answer was published.",
                retryable=True,
            ) from exc
        finally:
            if owned:
                await client.aclose()
        elapsed = round((time.perf_counter() - started) * 1000, 3)
        if response.status_code >= 400:
            raise ProviderError(
                ERROR_CODES.get(response.status_code, "provider_unavailable"),
                f"The provider returned HTTP {response.status_code}. No answer was published.",
                retryable=response.status_code in {429, 502, 503, 504},
            )
        try:
            data = response.json()
            if not isinstance(data, dict):
                raise TypeError("Response is not an object")
        except (ValueError, TypeError) as exc:
            raise ProviderError(
                "provider_invalid_response", "The provider returned unreadable data."
            ) from exc
        return data, elapsed

    @staticmethod
    def _raise_embedded_error(data: dict[str, Any], usage: ProviderUsage) -> None:
        # OpenRouter can commit HTTP 200 before an upstream generation fails.
        # Check its error envelope before attempting the answer/embedding schema.
        error = data.get("error")
        if not isinstance(error, dict):
            return
        status = error.get("code")
        status = status if type(status) is int else None
        failure = ProviderError(
            ERROR_CODES.get(status, "provider_unavailable"),
            "The provider reported an error in its response. No answer was published.",
            retryable=status in {429, 502, 503, 504},
            usage=usage,
        )
        # Do not retain upstream messages/metadata that may echo inputs or headers.
        failure.response_diagnostics = {
            "response_id": usage.request_id,
            "upstream_error_code": status,
            "error_transport": "response_body",
        }
        raise failure

    def _usage(self, data: dict[str, Any], elapsed: float, operation: str, model: str) -> ProviderUsage:
        values = data.get("usage") or {}
        return ProviderUsage(
            operation=operation,
            model=data.get("model") or model,
            provider=data.get("provider"),
            request_id=data.get("id"),
            prompt_tokens=values.get("prompt_tokens"),
            completion_tokens=values.get("completion_tokens"),
            total_tokens=values.get("total_tokens"),
            cost=values.get("cost"),
            latency_ms=elapsed,
        )

    def _schema(self, schema: type[BaseModel]) -> dict[str, Any]:
        value = schema.model_json_schema()
        if self.model.startswith("openai/"):
            # Strict Structured Outputs requires every declared field, including
            # locally defaulted fields, on the wire. Local defaults remain useful
            # for reading older stored contracts; they do not change the API schema.
            def strict(node):
                if isinstance(node, dict):
                    result = {key: strict(item) for key, item in node.items() if key != "default"}
                    if result.get("type") == "object" and "properties" in result:
                        result["required"] = list(result["properties"])
                        result["additionalProperties"] = False
                    return result
                if isinstance(node, list):
                    return [strict(item) for item in node]
                return node

            return strict(value)
        if not self.model.startswith("google/"):
            return value

        # Google's constrained decoder can reject nested bounded arrays as too complex.
        # Send the structural schema and retain every size bound in local Pydantic validation;
        # the provider output-token bound still caps work. This does not use free-form JSON repair.
        def supported(node):
            if isinstance(node, dict):
                return {
                    key: supported(item)
                    for key, item in node.items()
                    if key not in {"minLength", "maxLength", "minItems", "maxItems", "default", "title"}
                }
            if isinstance(node, list):
                return [supported(item) for item in node]
            return node

        return supported(value)

    async def _post_checked(
        self, path: str, body: dict[str, Any], operation: str, model: str
    ) -> tuple[dict[str, Any], ProviderUsage, int]:
        """Post once, or again after an unbilled rate-limit rejection; return data, usage, retry count."""
        for attempt in range(len(RATE_LIMIT_BACKOFF_SECONDS) + 1):
            try:
                data, elapsed = await self._post(path, body)
                usage = self._usage(data, elapsed, operation, model)
                self._raise_embedded_error(data, usage)
                return data, usage, attempt
            except ProviderError as exc:
                # Only a rate limit that produced no completion and no receipt is retried: a bounded
                # backoff then cannot duplicate or hide a provider attempt. Billed envelopes, timeouts
                # and every other failure propagate unchanged with their actual receipt.
                billed = exc.usage is not None and (
                    exc.usage.cost is not None or exc.usage.total_tokens is not None
                )
                if exc.code != "provider_rate_limited" or billed or attempt >= len(RATE_LIMIT_BACKOFF_SECONDS):
                    raise
                await asyncio.sleep(RATE_LIMIT_BACKOFF_SECONDS[attempt])
        raise AssertionError("unreachable")

    async def structured(
        self,
        system: str,
        payload: dict[str, Any],
        schema: type[T],
        operation: str = "generation",
        max_tokens: int = 4500,
    ) -> StructuredResult:
        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False, separators=(",", ":"))},
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": schema.__name__,
                    "strict": True,
                    "schema": self._schema(schema),
                },
            },
            ROUTE_CONFIG["routes"].get(self.model, {}).get("token_parameter", "max_tokens"): min(
                max_tokens, 6000
            ),
            "stream": False,
            "provider": self._routing(),
        }
        if self.reasoning_effort is not None:
            body["reasoning"] = {"effort": self.reasoning_effort, "exclude": True}
        data, usage, retries = await self._post_checked("chat/completions", body, operation, self.model)
        diagnostic = {
            "schema": schema.__name__,
            "response_id": data.get("id"),
            "max_completion_tokens": min(max_tokens, 6000),
        }
        try:
            choice = data["choices"][0]
            if not isinstance(choice, dict):
                raise TypeError("The first provider choice is not an object")
            message = choice.get("message")
            if not isinstance(message, dict):
                raise TypeError("The provider message is not an object")
            content = message.get("content")
            diagnostic.update(
                {
                    "finish_reason": choice.get("finish_reason"),
                    "native_finish_reason": choice.get("native_finish_reason"),
                    "content": content[:50000] if isinstance(content, str) else None,
                    "content_truncated_in_diagnostic": isinstance(content, str) and len(content) > 50000,
                }
            )
            if choice.get("finish_reason") not in {"stop", None}:
                error = ProviderError(
                    "provider_incomplete",
                    "The provider response was incomplete and was not published.",
                    usage=usage,
                )
                error.response_diagnostics = diagnostic
                raise error
            value = schema.model_validate_json(choice["message"]["content"])
        except (KeyError, IndexError, TypeError, ValueError, ValidationError) as exc:
            diagnostic["exception_type"] = type(exc).__name__
            if isinstance(exc, ValidationError):
                diagnostic["schema_errors"] = [
                    {"type": item["type"], "loc": list(item["loc"]), "message": item["msg"]}
                    for item in exc.errors(include_url=False, include_context=False, include_input=False)
                ]
            if isinstance(data.get("error"), dict):
                diagnostic["upstream_error_code"] = data["error"].get("code")
            error = ProviderError(
                "provider_schema_invalid",
                "The provider response did not match the answer contract.",
                usage=usage,
            )
            error.response_diagnostics = diagnostic
            raise error from exc
        return StructuredResult(value=value, usage=usage, rate_limit_retries=retries)

    async def embed(self, texts: list[str], model: str, input_type: str | None = None) -> EmbeddingResult:
        if (
            not 1 <= len(texts) <= 64
            or any(not text or len(text) > 20000 for text in texts)
            or sum(map(len, texts)) > 200000
        ):
            raise ValueError("Embedding inputs exceed the bounded corpus/query request")
        body: dict[str, Any] = {
            "model": model,
            "input": texts,
            "encoding_format": "float",
            "provider": self._routing(model),
        }
        if input_type:
            body["input_type"] = input_type
        data, usage, retries = await self._post_checked("embeddings", body, "embedding", model)
        try:
            items = sorted(data["data"], key=lambda item: item["index"])
            if [item["index"] for item in items] != list(range(len(texts))):
                raise ValueError("Missing or duplicate embedding index")
            vectors = [[float(value) for value in item["embedding"]] for item in items]
            if len({len(vector) for vector in vectors}) != 1 or any(
                not vector or not any(vector) or not all(math.isfinite(value) for value in vector)
                for vector in vectors
            ):
                raise ValueError("Invalid embedding values or dimensions")
            if data["model"] not in {model, model.split("/", 1)[-1]}:
                raise ValueError("Unexpected embedding model identity")
        except (KeyError, TypeError, ValueError) as exc:
            raise ProviderError(
                "provider_embedding_invalid",
                "The provider returned an incomplete or inconsistent embedding space.",
                usage=usage,
            ) from exc
        return EmbeddingResult(model=model, vectors=vectors, usage=usage, rate_limit_retries=retries)
