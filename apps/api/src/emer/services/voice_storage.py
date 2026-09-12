"""Postgres ownership and run mapping for the transport-independent Live controller."""

import asyncio
from datetime import timedelta
from uuid import uuid4

from emer.contracts.http import RunCreate
from emer.contracts.voice import VoiceAnswer, VoiceIntent, VoiceSnapshot
from emer.domain.dialogue_state import DialogueState, append_user_intent
from emer.services import runs
from emer.services.intent_context import recent_intent_context
from emer.services.provider_activity import TrackedOpenRouterClient
from emer.services.text_intent import resolve_text_intent
from emer.settings import settings
from emer.storage.database import Session
from emer.storage.models import BrowserSession, Conversation, Delegation, LiveEvent, LiveSession, Run, utcnow
from sqlalchemy import select

VOICE_OWNER = str(uuid4())
ACTIVE_VOICE = {"creating", "connecting", "listening", "working", "closing"}


class PostgresVoiceHooks:
    async def _owned(self, db, session_id: str, fence: int, *, active: bool = True):
        row = await db.scalar(select(LiveSession).where(LiveSession.id == session_id).with_for_update())
        if not row or row.owner_id != VOICE_OWNER or row.fence != fence:
            return None
        if active and (
            row.status not in ACTIVE_VOICE or not row.lease_expires_at or row.lease_expires_at <= utcnow()
        ):
            return None
        browser = await db.get(BrowserSession, row.session_id)
        if active and (not browser or browser.revoked or browser.expires_at <= utcnow()):
            return None
        return row

    async def save_snapshot(self, session_id: str, fence: int, snapshot: VoiceSnapshot) -> bool:
        async with Session.begin() as db:
            row = await self._owned(db, session_id, fence, active=False)
            if row is None or snapshot.revision < row.snapshot.get("revision", 0):
                return False
            if snapshot.revision > row.snapshot.get("revision", 0):
                pending = (
                    await db.scalars(
                        select(Delegation).where(
                            Delegation.live_session_id == session_id, Delegation.run_id.is_not(None)
                        )
                    )
                ).all()
                for delegation in pending:
                    run = await db.get(Run, delegation.run_id)
                    if run and run.status in {"queued", "running"}:
                        run.cancel_requested = True
            row.snapshot = snapshot.model_dump(mode="json")
            row.status = (
                "uncertain" if snapshot.status == "failed" and snapshot.error == "live_close_unconfirmed"
                else snapshot.status
            )
            if snapshot.status in {"closed", "failed"}:
                row.lease_expires_at = None
            elif snapshot.status == "closing" and row.lease_expires_at is None:
                # Startup failure can have published a terminal error before
                # discovering that provider closure still needs confirmation.
                # Only the same fenced owner may retain this cleanup-only lease.
                row.lease_expires_at = utcnow() + timedelta(seconds=20)
            return True

    async def claim_event(self, session_id: str, fence: int, event_id: str, kind: str, payload: dict) -> bool:
        async with Session.begin() as db:
            if await self._owned(db, session_id, fence, active=False) is None:
                return False
            exists = await db.scalar(
                select(LiveEvent.id).where(
                    LiveEvent.live_session_id == session_id, LiveEvent.provider_event_id == event_id
                )
            )
            if exists:
                return False
            db.add(
                LiveEvent(live_session_id=session_id, provider_event_id=event_id, kind=kind, payload=payload)
            )
            if kind == "session.input_transcript.delta" and payload.get("delta"):
                from emer.services.conversation_titles import queue_title
                from emer.services.conversations import touch

                live = await db.get(LiveSession, session_id)
                conversation = await db.get(Conversation, live.conversation_id, with_for_update=True)
                if conversation:
                    touch(conversation)
                    await queue_title(db, conversation, debounce=True)
            return True

    async def claim_delegation(self, intent: VoiceIntent) -> bool:
        async with Session.begin() as db:
            try:
                live = await self._current_intent(db, intent)
            except asyncio.CancelledError:
                return False
            if live.snapshot.get("close_reason") == "user":
                # A closed user session may admit only the exact delegation
                # already received by its fenced controller, never a fresh one.
                if live.snapshot.get("last_delegation_id") != intent.delegation_id:
                    return False
                received = await db.scalar(select(LiveEvent.id).where(
                    LiveEvent.live_session_id == intent.session_id,
                    LiveEvent.kind == "session.delegation.created",
                    LiveEvent.payload["delegation"]["id"].astext == intent.delegation_id,
                ).limit(1))
                if received is None:
                    return False
            row = await db.scalar(
                select(Delegation)
                .where(
                    Delegation.live_session_id == intent.session_id,
                    Delegation.delegation_id == intent.delegation_id,
                )
                .with_for_update()
            )
            if row and row.revision >= intent.revision:
                return False
            if row is None:
                row = Delegation(live_session_id=intent.session_id, delegation_id=intent.delegation_id)
                db.add(row)
            elif row.run_id:
                row.run_history = [*row.run_history, {"revision": row.revision, "run_id": row.run_id}]
                row.run_id = None
            row.revision = intent.revision
            row.intent = intent.model_dump(mode="json")
            return True

    async def renew(self, session_id: str, fence: int) -> bool:
        async with Session.begin() as db:
            row = await self._owned(db, session_id, fence, active=False)
            if (row is None or row.status not in ACTIVE_VOICE or not row.lease_expires_at
                    or row.lease_expires_at <= utcnow()):
                return False
            if row.status != "closing":
                browser = await db.get(BrowserSession, row.session_id)
                if not browser or browser.revoked or browser.expires_at <= utcnow():
                    return False
                conversation = await db.get(Conversation, row.conversation_id)
                if conversation is None or conversation.context_version != row.context_version:
                    return False
            # Closing can outlive a browser reset/context change: retain only
            # the fenced cleanup lease so the existing provider session is ended.
            row.lease_expires_at = utcnow() + timedelta(seconds=20)
            return True

    async def _current_delegation(self, db, intent: VoiceIntent):
        row = await db.scalar(
            select(Delegation)
            .where(
                Delegation.live_session_id == intent.session_id,
                Delegation.delegation_id == intent.delegation_id,
            )
            .with_for_update()
        )
        if row is None or row.revision != intent.revision:
            raise asyncio.CancelledError
        return row

    async def _cancel_run(self, run_id: str, *, timeout_seconds: float = 5) -> None:
        async with Session.begin() as db:
            run = await db.get(Run, run_id)
            if run and run.status in {"queued", "running"}:
                run.cancel_requested = True
        # Root's persisted run watchdog observes this flag and finishes the old slot.
        try:
            async with asyncio.timeout(timeout_seconds):
                while True:
                    async with Session() as db:
                        run = await db.get(Run, run_id)
                        if run is None or run.status in runs.TERMINAL:
                            return
                    await asyncio.sleep(0.1)
        except TimeoutError:
            # This is an unconfirmed old execution slot, not a lookup timeout.
            # It must close voice rather than permit the pending correction.
            raise RuntimeError("live_run_cancellation_unconfirmed") from None

    async def _current_intent(self, db, intent: VoiceIntent):
        live = await self._owned(db, intent.session_id, intent.fence, active=False)
        if live is None:
            raise asyncio.CancelledError
        browser = await db.get(BrowserSession, live.session_id)
        conversation = await db.get(Conversation, intent.conversation_id)
        if (
            live.snapshot.get("revision") != intent.revision
            or live.conversation_id != intent.conversation_id
            or live.context_version != intent.context_version
            or live.index_id != intent.index_id
            or live.provider_session_id != intent.provider_session_id
            or not browser or browser.revoked or browser.expires_at <= utcnow()
            or not conversation or conversation.session_id != live.session_id
            or conversation.context_version != intent.context_version
            or live.snapshot.get("close_reason") not in {None, "user"}
            or (live.snapshot.get("close_reason") != "user" and (
                live.status not in ACTIVE_VOICE or not live.lease_expires_at or live.lease_expires_at <= utcnow()
            ))
        ):
            raise asyncio.CancelledError
        return live

    async def answer(self, intent: VoiceIntent) -> VoiceAnswer:
        mapped_run = None
        try:
            async with Session() as db:
                live = await self._current_intent(db, intent)
                owner_id = live.session_id
                context = {**live.context, **await recent_intent_context(db, intent.conversation_id, intent.context_version)}
                previous = await db.scalar(select(Delegation).where(
                    Delegation.live_session_id == intent.session_id,
                    Delegation.revision < intent.revision,
                ).order_by(Delegation.revision.desc()).limit(1))
                state = DialogueState.model_validate(context["dialogue_state"])
                if previous:
                    prior = previous.intent
                    previous_question = prior.get("resolved_question") or "".join(
                        fragment["delta"] for fragment in prior.get("transcript", [])
                        if fragment["speaker"] == "user" and fragment["end_ms"] > prior.get("request_start_ms", 0)
                    )
                    state = append_user_intent(state, previous_question, "live:" + previous.delegation_id)
            user_text = "".join(
                f.delta for f in intent.transcript
                if f.speaker == "user" and f.end_ms > intent.request_start_ms
            )
            # The voice model's delegation metadata has no task text. Resolve the current
            # task from server-observed fragments; all source answering still uses the same run service.
            provider = TrackedOpenRouterClient(
                settings.openrouter_api_key,
                settings.openrouter_model,
                session_id=owner_id,
                live_session_id=intent.session_id,
                live_revision=intent.revision,
                live_context_version=intent.context_version,
                live_fence=intent.fence,
                reasoning_effort=settings.openrouter_reasoning_effort,
                reasoning_token_reserve=settings.openrouter_reasoning_token_reserve,
                         operation_reasoning=settings.generator_operation_reasoning,
                max_input_tokens=settings.model_input_token_budget,
            )
            async with asyncio.timeout(30):
                resolved = await resolve_text_intent(
                    provider, user_text, context.get("previous_questions", []),
                    None if context.get("automatic_context") else context.get("patient_id"),
                    None if context.get("automatic_context") else context.get("as_of"),
                    dialogue_state=state,
                )
            # Deltas may split a word. Do not insert spaces into "patient s" + "ix".
            question, clarification = resolved.question, resolved.clarification
            async with Session.begin() as db:
                await self._current_intent(db, intent)
                delegation = await self._current_delegation(db, intent)
                delegation.intent = {
                    **delegation.intent,
                    "resolved_question": question,
                    "clarification": clarification,
                    "dialogue_state": state.model_dump(mode="json"),
                    "reference_resolution": resolved.reference_resolution.model_dump(mode="json")
                    if resolved.reference_resolution else None,
                    "usage_source": "provider_attempt_ledger",
                }
            if clarification or not question:
                return VoiceAnswer(
                    run_id=None,
                    accepted=True,
                    spoken_text=clarification or "Please repeat your question.",
                )
            run = await runs.create_run(
                owner_id,
                intent.conversation_id,
                RunCreate(
                    automatic_context=bool(context.get("automatic_context", False)),
                    question=question,
                    context_version=intent.context_version,
                    idempotency_key=f"voice:{intent.session_id}:{intent.revision}",
                ),
                index_id=intent.index_id,
                intent_resolved=True,
            )
            mapped_run = run.id
            async with Session.begin() as db:
                delegation = await self._current_delegation(db, intent)
                delegation.run_id = run.id
            while True:
                async with Session() as db:
                    saved = await db.get(Run, run.id)
                    if saved.status in runs.TERMINAL:
                        break
                await asyncio.sleep(0.2)
            if saved.status != "completed" or saved.answer is None:
                return VoiceAnswer(
                    run_id=saved.id, accepted=False, spoken_text="No verified answer was completed."
                )
            answer = saved.answer
            # No additional speech rewrite: checked statement/gap text is the only factual payload.
            statements = [item["text"] for item in answer.get("statements", [])]
            gaps = [item["text"] for item in answer.get("gaps", [])]
            next_steps = [item["text"] for item in answer.get("next_steps", [])]
            return VoiceAnswer(
                run_id=saved.id, accepted=True, spoken_text=" ".join([*statements, *gaps, *next_steps])
            )
        except asyncio.CancelledError:
            if mapped_run:
                await self._cancel_run(mapped_run)
            raise
