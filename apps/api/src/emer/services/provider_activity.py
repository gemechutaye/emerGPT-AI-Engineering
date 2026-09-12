"""Durable receipts for structured OpenRouter calls, independent of final run publication."""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, TypeVar

import httpx
from emer.contracts.answer import ProviderUsage
from emer.providers.openrouter import EmbeddingResult, OpenRouterClient, ProviderError, StructuredResult
from emer.storage.database import Session
from emer.storage.models import BrowserSession, Conversation, Draft, LiveSession, ProviderAttempt, Run, utcnow
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

T = TypeVar("T", bound=BaseModel)
NO_RECEIPT_FAILURES = {
    "provider_unconfigured", "model_unconfigured", "provider_authentication",
    "provider_capacity_exhausted", "provider_forbidden", "provider_model_unavailable",
    "provider_rate_limited", "provider_request_invalid",
}
_pending_receipts: set[asyncio.Task] = set()


def _receipt_finished(task: asyncio.Task) -> None:
    _pending_receipts.discard(task)
    if not task.cancelled():
        task.exception()  # Retrieve failures even if repeated caller cancellation ended its wait.


def usage_record(usage: ProviderUsage | dict[str, Any] | None) -> dict[str, Any] | None:
    """Keep only typed, public usage metadata; never provider bodies or arbitrary exception attributes."""
    if usage is None:
        return None
    return ProviderUsage.model_validate(usage).model_dump(mode="json")


def error_code(error: ProviderError) -> str:
    value = getattr(error, "code", "provider_failed")
    return value if isinstance(value, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,79}", value) else "provider_failed"


class TrackedOpenRouterClient(OpenRouterClient):
    """Track each .structured call separately. No automatic HTTP retries are added.

    A dispatched row is committed before HTTP starts. Completed and failed receipts
    remain valid if later generation/checking fails, the run is cancelled, or the
    caller's ownership expires. Missing receipts remain explicitly uncertain.
    """

    def __init__(
        self, api_key: str | None, model: str, session_id: str,
        run_id: str | None = None, draft_id: str | None = None, live_session_id: str | None = None,
        *, provider_order: list[str] | None = None, client: httpx.AsyncClient | None = None,
        ledger_timeout_seconds: float = 5,
        run_fence: int | None = None, run_owner: str | None = None, draft_claim: str | None = None,
        live_revision: int | None = None, live_context_version: int | None = None,
        live_fence: int | None = None,
        reasoning_effort: str | None = None,
    ):
        super().__init__(api_key, model, provider_order=provider_order, client=client, reasoning_effort=reasoning_effort)
        self.session_id = session_id
        self.run_id = run_id
        self.draft_id = draft_id
        self.live_session_id = live_session_id
        self.ledger_timeout = ledger_timeout_seconds
        self.run_fence, self.run_owner, self.draft_claim = run_fence, run_owner, draft_claim
        self.live_revision, self.live_context_version, self.live_fence = (
            live_revision, live_context_version, live_fence,
        )

    async def _dispatch(self, operation: str, requested_model: str | None = None) -> str:
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,59}", operation):
            raise ValueError("Provider operation must be a short application identifier")
        async with asyncio.timeout(self.ledger_timeout), Session.begin() as db:
            owner = await db.scalar(select(BrowserSession).where(
                BrowserSession.id == self.session_id
            ).with_for_update())
            if owner is None or owner.revoked or owner.expires_at <= utcnow():
                raise ProviderError("provider_owner_inactive", "The browser session is no longer active.")
            for model, resource_id in ((Run, self.run_id), (Draft, self.draft_id), (LiveSession, self.live_session_id)):
                if resource_id is None:
                    continue
                resource = await db.get(model, resource_id, with_for_update=model is LiveSession)
                if resource is None or resource.session_id != self.session_id:
                    raise ProviderError("provider_scope_invalid", "The provider request does not belong to this browser session.")
                if model is Run and self.run_fence is not None:
                    conversation = await db.get(Conversation, resource.conversation_id)
                    if (
                        resource.status != "running" or resource.cancel_requested
                        or resource.fence != self.run_fence or resource.owner_id != self.run_owner
                        or not resource.lease_expires_at or resource.lease_expires_at <= utcnow()
                        or not conversation or conversation.context_version != resource.context_version
                    ):
                        raise ProviderError("provider_claim_inactive", "This answer is no longer active.")
                if model is Draft and self.draft_claim is not None and (
                        resource.check_owner != self.draft_claim
                        or not resource.check_lease_expires_at or resource.check_lease_expires_at <= utcnow()
                        or resource.checking_version != resource.version
                ):
                    raise ProviderError("provider_claim_inactive", "This draft check is no longer active.")
                if model is LiveSession:
                    conversation = await db.get(Conversation, resource.conversation_id)
                    ended_by_user = resource.snapshot.get("close_reason") == "user"
                    live_active = (
                        resource.status in {"listening", "working"}
                        and resource.lease_expires_at is not None
                        and resource.lease_expires_at > utcnow()
                    )
                    if (
                        self.live_revision is None or self.live_context_version is None
                        or self.live_fence is None or resource.fence != self.live_fence
                        or resource.snapshot.get("revision") != self.live_revision
                        or resource.context_version != self.live_context_version
                        or conversation is None
                        or conversation.context_version != self.live_context_version
                        or not (live_active or ended_by_user)
                    ):
                        raise ProviderError("provider_claim_inactive", "This voice request is no longer active.")
            row = ProviderAttempt(
                session_id=self.session_id, run_id=self.run_id, draft_id=self.draft_id,
                live_session_id=self.live_session_id, operation=operation, model=requested_model or self.model,
                status="dispatched", usage=None,
            )
            db.add(row)
            await db.flush()
            attempt_id = row.id
        return attempt_id

    async def _write_receipt(
        self, attempt_id: str, status: str, usage: dict | None, failure: str | None,
    ) -> None:
        async with asyncio.timeout(self.ledger_timeout), Session.begin() as db:
            row = await db.scalar(select(ProviderAttempt).where(
                ProviderAttempt.id == attempt_id, ProviderAttempt.session_id == self.session_id
            ).with_for_update())
            if row is None:
                raise RuntimeError("Provider dispatch record missing")
            if row.status != "dispatched":
                return
            row.status, row.usage, row.error_code = status, usage, failure
            row.completed_at = utcnow()

    async def _receipt(
        self, attempt_id: str, status: str, usage: dict | None, failure: str | None = None,
    ) -> bool:
        # Cancellation of application work must not discard an already received usage receipt.
        # The independent writer has its own finite timeout and never performs provider I/O.
        task = asyncio.create_task(self._write_receipt(attempt_id, status, usage, failure))
        _pending_receipts.add(task)
        task.add_done_callback(_receipt_finished)
        cancelled = False
        try:
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                cancelled = True
                await asyncio.shield(task)
            return True
        except (SQLAlchemyError, TimeoutError, RuntimeError):
            logging.getLogger("emer.provider_activity").warning("provider_receipt_persistence_unavailable")
            return False
        finally:
            if cancelled:
                raise asyncio.CancelledError

    async def structured(
        self, system: str, payload: dict[str, Any], schema: type[T], operation: str = "generation",
        max_tokens: int = 4500,
    ) -> StructuredResult:
        attempt_id = await self._dispatch(operation)
        try:
            result = await super().structured(system, payload, schema, operation=operation, max_tokens=max_tokens)
        except asyncio.CancelledError:
            await self._receipt(attempt_id, "uncertain", None, "provider_cancelled")
            raise
        except ProviderError as exc:
            usage = usage_record(exc.usage)
            code = error_code(exc)
            status = "failed" if usage is not None or code in NO_RECEIPT_FAILURES else "uncertain"
            await self._receipt(attempt_id, status, usage, code)
            raise
        except Exception:
            await self._receipt(attempt_id, "uncertain", None, "provider_processing_failed")
            raise
        if not await self._receipt(attempt_id, "completed", usage_record(result.usage)):
            # The dispatch survives a ledger outage. Do not report a fully persisted success.
            raise ProviderError(
                "provider_receipt_unavailable", "The provider completed, but its usage receipt could not be saved.",
                usage=result.usage,
            )
        return result

    async def embed(self, texts: list[str], model: str, input_type: str | None = None) -> EmbeddingResult:
        attempt_id = await self._dispatch("query_embedding", requested_model=model)
        try:
            result = await super().embed(texts, model, input_type=input_type)
        except asyncio.CancelledError:
            await self._receipt(attempt_id, "uncertain", None, "provider_cancelled")
            raise
        except ProviderError as exc:
            usage = usage_record(exc.usage)
            code = error_code(exc)
            status = "failed" if usage is not None or code in NO_RECEIPT_FAILURES else "uncertain"
            await self._receipt(attempt_id, status, usage, code)
            raise
        except Exception:
            await self._receipt(attempt_id, "uncertain", None, "provider_processing_failed")
            raise
        if not await self._receipt(attempt_id, "completed", usage_record(result.usage)):
            raise ProviderError(
                "provider_receipt_unavailable", "The provider completed, but its usage receipt could not be saved.",
                usage=result.usage,
            )
        return result


async def drain_provider_receipts() -> None:
    """Call at shutdown after cancelling run/draft/voice workers, before disposing Postgres."""
    if _pending_receipts:
        await asyncio.gather(*list(_pending_receipts), return_exceptions=True)
