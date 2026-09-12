"""One fenced controller and one bounded execution slot per Live session."""

import asyncio
import contextlib
import json
import time
from uuid import uuid4

from emer.contracts.voice import TranscriptFragment, VoiceContext, VoiceHooks, VoiceIntent, VoiceSnapshot
from emer.domain.utterance import CORRECTION
from emer.providers.openai_live import OpenAILive, send_event
from emer.providers.openrouter import ProviderError


class ControlLost(RuntimeError):
    pass


class VoiceController:
    def __init__(
        self, context: VoiceContext, provider: OpenAILive, hooks: VoiceHooks,
        *, max_duration_seconds: float = 300, answer_timeout_seconds: float = 100,
        cancellation_timeout_seconds: float = 7,
        close_timeout_seconds: float = 4, settle_seconds: float = 0.25, watch_interval_seconds: float = 5,
    ):
        self.context = context
        self.provider = provider
        self.hooks = hooks
        self.snapshot = VoiceSnapshot(id=context.session_id)
        self.max_duration = max_duration_seconds
        self.answer_timeout = answer_timeout_seconds
        self.cancellation_timeout = cancellation_timeout_seconds
        self.close_timeout = close_timeout_seconds
        self.settle_seconds = settle_seconds
        self.watch_interval = watch_interval_seconds
        self.transcript: list[TranscriptFragment] = []
        self.connection = None
        self._seen: set[str] = set()
        self._delegations: set[str] = set()
        self._closed = asyncio.Event()
        self._closing = False
        self._shutdown_complete = False
        self._cleanup_pending = False
        self._cleanup_retried = False
        self._receiver: asyncio.Task | None = None
        self._watchdog: asyncio.Task | None = None
        self._worker: asyncio.Task | None = None
        self._active_answer: asyncio.Task | None = None
        self._answer_cancelled = asyncio.Event()
        self._answer_cancel_deadline: float | None = None
        self._pending: tuple[str, int] | None = None
        self._pending_ready = asyncio.Event()
        self._admitting = False
        self._receiving_delegations = 0
        self._preserve_on_end = False
        self._snapshot_lock = asyncio.Lock()
        self._wake = asyncio.Event()
        self._start_time = time.monotonic()
        self._last_delegation_offset = -1
        self._request_start_ms = 0
        self._correction_pending = False
        self._latest_delegation: str | None = None

    async def _persist(self) -> None:
        # End and provider events may overlap. Serialize copies as well as writes
        # so a delayed working snapshot cannot replace a confirmed closed one.
        async with self._snapshot_lock:
            if not await self.hooks.save_snapshot(
                self.context.session_id, self.context.fence, self.snapshot.model_copy(deep=True)
            ):
                raise ControlLost("live_controller_ownership_lost")

    @property
    def has_pending_work(self) -> bool:
        return bool(self._pending is not None or self._admitting or self._receiving_delegations
                    or self._active_answer is not None)

    def _current_work(self, revision: int) -> bool:
        return revision == self.snapshot.revision and (
            self._preserve_on_end if self._closing else not self._closed.is_set()
        )

    async def start(self) -> None:
        try:
            if not await self.hooks.renew(self.context.session_id, self.context.fence):
                raise ControlLost("live_controller_ownership_lost")
            self.connection = await self.provider.attach(self.context.provider_session_id)
            self.snapshot.controller_connected = True
            self.snapshot.playback_blocked = False
            self.snapshot.status = "listening"
            await self._persist()
            self._receiver = asyncio.create_task(self._receive(), name=f"live-receive-{self.context.session_id}")
            self._worker = asyncio.create_task(self._work(), name=f"live-work-{self.context.session_id}")
            self._watchdog = asyncio.create_task(self._watch(), name=f"live-watch-{self.context.session_id}")
        except Exception:
            await self._fail("live_control_connection_failed")
            raise

    async def _send(self, kind: str, *, expected_revision: int | None = None, **payload) -> bool:
        if (self._closing or self._closed.is_set()
                or expected_revision is not None and expected_revision != self.snapshot.revision):
            return False
        if self.connection is None:
            raise ControlLost("live_control_unavailable")
        renewed = await self.hooks.renew(self.context.session_id, self.context.fence)
        # Renewal yields to correction and End handlers. Recheck immediately
        # before dispatch so an obsolete result cannot follow their stop signal.
        if (self._closing or self._closed.is_set()
                or expected_revision is not None and expected_revision != self.snapshot.revision):
            return False
        if not renewed:
            raise ControlLost("live_controller_ownership_lost")
        await send_event(self.connection, kind, event_id=uuid4().hex, **payload)
        return True

    async def _receive(self) -> None:
        try:
            async for raw in self.connection:
                event = json.loads(raw)
                # Reflected audio has no event ID and is deliberately discarded, never persisted.
                if event.get("type") in {"session.input_audio.append", "session.output_audio.delta"}:
                    continue
                await self.handle_event(event)
                if self._closed.is_set():
                    if self._closing:
                        await self._finalize_close(confirmed=True)
                    else:
                        await self.close(reason=self.snapshot.close_reason or "provider_closed")
                    break
            if not self._closed.is_set() and not self._closing:
                await self._fail("live_control_connection_lost")
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - any control-loop failure must close the media session
            if not self._closing:
                await self._fail("live_control_connection_lost")

    async def handle_event(self, event: dict) -> None:
        # Receipt precedes the durable event write. End can occur during that
        # write, but only this already-received delegation may finish admission.
        received_revision = self.snapshot.revision
        received_delegation = event.get("type") == "session.delegation.created" and not self._closing
        if received_delegation:
            self._receiving_delegations += 1
        try:
            await self._handle_event(event, received_revision, received_delegation)
        finally:
            if received_delegation:
                self._receiving_delegations -= 1
                if self._closing:
                    self._wake.set()

    async def _handle_event(self, event: dict, received_revision: int, received_delegation: bool) -> None:
        kind = event.get("type", "")
        event_id = event.get("event_id")
        if not isinstance(event_id, str) or event_id in self._seen:
            return
        payload = {k: v for k, v in event.items() if k not in {"audio", "delta"}}
        if kind.endswith("_transcript.delta"):
            payload["delta"] = event.get("delta", "")
        if not await self.hooks.claim_event(
            self.context.session_id, self.context.fence, event_id, kind, payload
        ):
            return
        self._seen.add(event_id)
        if len(self._seen) > 12000:
            raise ControlLost("live_event_limit")
        if kind in {"session.input_transcript.delta", "session.output_transcript.delta"}:
            fragment = TranscriptFragment(
                event_id=event_id, speaker="user" if kind.startswith("session.input_") else "assistant",
                delta=event["delta"], start_ms=event["start_ms"], end_ms=event["end_ms"],
            )
            self.transcript.append(fragment)
            if len(self.transcript) > 4000:
                raise ControlLost("live_transcript_limit")
            # Never infer a completed turn from gaps. Only explicit correction language
            # invalidates work before the next provider delegation arrives.
            if (not self._closing and fragment.speaker == "user"
                    and fragment.end_ms > self._last_delegation_offset
                    and self._latest_delegation is not None):
                # Only text after the latest provider delegation can revise it.
                # Late historical fragments and ordinary negation are not a new correction.
                user_text = "".join(
                    f.delta for f in sorted(self.transcript, key=lambda f: (f.start_ms, f.end_ms))
                    if f.speaker == "user" and f.end_ms > self._last_delegation_offset
                )
                if not self._correction_pending and CORRECTION.search(user_text):
                    # One utterance may contain "No, I mean ..." across deltas.
                    # Fence immediately, then avoid repeatedly steering the model
                    # while the corrected request is still arriving.
                    self._correction_pending = True
                    await self._invalidate(schedule=False)
            return
        if (kind == "session.delegation.created" and received_delegation
                and self._current_work(received_revision)):
            delegation = event.get("delegation", {})
            delegation_id = delegation.get("id")
            if delegation.get("target") != "client" or not isinstance(delegation_id, str):
                return
            if delegation_id in self._delegations:
                return
            self._delegations.add(delegation_id)
            offset = int(event.get("offset_ms", 0))
            # A late old delegation cannot replace a newer one. Timestamps are provider
            # timeline metadata, not fabricated sequence numbers or turn boundaries.
            if offset < self._last_delegation_offset:
                return
            self._request_start_ms = max(0, self._last_delegation_offset)
            self._last_delegation_offset = offset
            self._latest_delegation = delegation_id
            self.snapshot.last_delegation_id = delegation_id
            self._correction_pending = False
            await self._invalidate(schedule=True)
        elif kind in {"session.usage.updated", "session.closed"}:
            seconds = event.get("usage", {}).get("seconds")
            if isinstance(seconds, (float, int)) and seconds >= 0:
                self.snapshot.usage_seconds = float(seconds)  # cumulative snapshot, never summed
            if kind == "session.closed":
                self.snapshot.final_usage_confirmed = True
                self.snapshot.provider_close_reason = event.get("reason")
                # Transport closure is evidence, not the application decision to
                # preserve or cancel already-requested background work.
                if self.snapshot.close_reason is None:
                    self.snapshot.close_reason = "provider_closed"
                self.snapshot.status = "closed"
                self.snapshot.controller_connected = False
                self.snapshot.playback_blocked = True
                self._closed.set()
            await self._persist()
        elif kind == "error":
            # The event remains durable telemetry. A provider rejecting an
            # in-flight injection during End must not revoke the user's request.
            if not self._closing:
                await self._fail("live_provider_command_failed")

    async def _invalidate(self, *, schedule: bool) -> None:
        self.snapshot.revision += 1
        revision = self.snapshot.revision
        self.snapshot.playback_blocked = True
        if not self._closing:
            self.snapshot.status = "working"
        self._cancel_answer()
        self._pending = (self._latest_delegation, revision) if schedule and self._latest_delegation else None
        self._pending_ready.clear()
        await self._persist()
        if not schedule:
            # A correction cue can arrive before the patient/date words. Keep
            # fencing local until delegation so steering cannot interrupt them.
            return
        sent = await self._send(
            "session.instructions.append", expected_revision=revision, delegation_id=None,
            content="Stop the previous factual answer immediately. The request is being checked. "
            "Do not use an older result. Wait for the current verified result; ask for clarification if needed.",
        )
        if self._current_work(revision) and (sent or self._preserve_on_end):
            # Normally steering follows the durable blocked revision. End may
            # drain the same pending request with media already disabled.
            self._pending_ready.set()
            self._wake.set()

    def _cancel_answer(self) -> None:
        task = self._active_answer
        if task is None or task.done() or self._answer_cancelled.is_set():
            return
        # Repeated corrections must not inject another cancellation into the
        # storage hook while it is waiting for the old persisted run to stop.
        self._answer_cancel_deadline = time.monotonic() + self.cancellation_timeout
        self._answer_cancelled.set()
        task.cancel()

    def _answer_finished(self, task: asyncio.Task) -> None:
        # A stalled task remains owned until it actually finishes, even after
        # the controller has closed. Retrieve late errors without losing the slot.
        with contextlib.suppress(asyncio.CancelledError):
            task.exception()
        if self._active_answer is task:
            self._active_answer = None

    async def _await_answer(self, task: asyncio.Task):
        cancellation = asyncio.create_task(self._answer_cancelled.wait())
        try:
            done, _ = await asyncio.wait(
                {task, cancellation}, timeout=self.answer_timeout, return_when=asyncio.FIRST_COMPLETED,
            )
            if task in done:
                return task.result()
            timed_out = not done
            if timed_out:
                self._cancel_answer()
            # Begin the drain at the first cancellation, not after the original
            # answer timeout. Later corrections cannot extend this deadline.
            remaining = max(0, (self._answer_cancel_deadline or time.monotonic()) - time.monotonic())
            stopped, _ = await asyncio.wait({task}, timeout=remaining)
            if not stopped:
                raise ControlLost("live_answer_cancellation_stalled")
            if timed_out:
                if not task.cancelled() and task.exception() is not None:
                    raise task.exception()
                raise TimeoutError
            return task.result()
        finally:
            cancellation.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await cancellation

    async def _work(self) -> None:
        try:
            while not self._closing or (self._preserve_on_end and (
                self._pending is not None or self._receiving_delegations
            )):
                await self._wake.wait()
                self._wake.clear()
                # This only allows metadata/transcript delivery to settle. It never creates a turn.
                await asyncio.sleep(self.settle_seconds)
                if self._pending is not None:
                    await self._pending_ready.wait()
                pending, self._pending = self._pending, None
                if pending is None:
                    continue
                delegation_id, revision = pending
                if not self._current_work(revision):
                    continue
                fragments = sorted(self.transcript, key=lambda f: (f.start_ms, f.end_ms))
                question = "".join(f.delta for f in fragments if f.speaker == "user").strip()
                if not question:
                    sent = await self._send(
                        "session.instructions.append", expected_revision=revision, delegation_id=delegation_id,
                        content="The server has no reliable transcript for this request. Ask the user to repeat it.",
                    )
                    if (sent and revision == self.snapshot.revision
                            and not self._closing and not self._closed.is_set()):
                        self.snapshot.status = "listening"
                        self.snapshot.playback_blocked = False
                        await self._persist()
                    continue
                intent = VoiceIntent(
                    **self.context.model_dump(), revision=revision, delegation_id=delegation_id,
                    question=question[-12000:], transcript=fragments, request_start_ms=self._request_start_ms,
                )
                self._admitting = True
                try:
                    claimed = await self.hooks.claim_delegation(intent)
                finally:
                    self._admitting = False
                if not claimed or not self._current_work(revision):
                    continue
                self._answer_cancelled.clear()
                self._answer_cancel_deadline = None
                task = asyncio.create_task(self.hooks.answer(intent))
                self._active_answer = task
                task.add_done_callback(self._answer_finished)
                try:
                    answer = await self._await_answer(task)
                except asyncio.CancelledError:
                    if self._closing and not self._preserve_on_end:
                        return
                    continue
                except TimeoutError:
                    if revision == self.snapshot.revision:
                        sent = await self._send(
                            "session.commentary.append", expected_revision=revision, delegation_id=delegation_id,
                            content="The source lookup timed out. No answer was verified. You can retry the question.",
                        )
                        if (sent and revision == self.snapshot.revision
                                and not self._closing and not self._closed.is_set()):
                            self.snapshot.status = "listening"
                            self.snapshot.playback_blocked = False
                            await self._persist()
                    continue
                if not self._current_work(revision):
                    continue
                self.snapshot.last_run_id = answer.run_id
                if self._closing:
                    # Persist the resulting run link without reopening playback.
                    await self._persist()
                    continue
                text = answer.spoken_text if answer.accepted else (
                    "I could not verify an answer from the supplied sources. Please inspect the written result."
                )
                # The API append limit is 500 tokens. UTF-8 byte bounds conservatively
                # bound BPE tokens without an extra rewrite call.
                for chunk in speech_chunks(text):
                    if revision != self.snapshot.revision or self._closing:
                        break
                    if not await self._send(
                        "session.commentary.append", expected_revision=revision,
                        delegation_id=delegation_id, content=chunk,
                    ):
                        break
                if revision == self.snapshot.revision and not self._closing and not self._closed.is_set():
                    self.snapshot.status = "listening"
                    self.snapshot.playback_blocked = False
                    await self._persist()
        except asyncio.CancelledError:
            raise
        except ProviderError as exc:
            if not self._closing:
                await self._fail(exc.code)
        except Exception:  # noqa: BLE001 - application/provider failures must close unsafe playback
            if not self._closing:
                await self._fail("live_delegation_failed")

    async def _watch(self) -> None:
        try:
            while not self._shutdown_complete:
                await asyncio.sleep(min(self.watch_interval, self.max_duration))
                if self._closing:
                    if not self._cleanup_pending:
                        continue
                    expired = time.monotonic() - self._start_time >= self.max_duration
                    if self._closed.is_set():
                        await self._finalize_close(confirmed=True)
                        return
                    if expired or not self._cleanup_retried:
                        self._cleanup_retried = True
                        confirmed = await self.provider.hangup(self.context.provider_session_id)
                        if confirmed or expired:
                            await self._finalize_close(confirmed=confirmed)
                            return
                    if not await self.hooks.renew(self.context.session_id, self.context.fence):
                        await self._finalize_close(confirmed=False)
                        return
                    continue
                if time.monotonic() - self._start_time >= self.max_duration:
                    await self.close(reason="duration_limit")
                    return
                if not await self.hooks.renew(self.context.session_id, self.context.fence):
                    await self._fail("live_controller_ownership_lost")
                    return
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - failed ownership renewal must fail closed
            if self._closing:
                await self._finalize_close(confirmed=False)
            else:
                await self._fail("live_controller_ownership_lost")

    async def _fail(self, error: str) -> None:
        self.snapshot.error = error
        self.snapshot.status = "failed"
        self.snapshot.playback_blocked = True
        self.snapshot.controller_connected = False
        with contextlib.suppress(Exception):
            await self._persist()
        await self.close(reason=error, cancel_work=True)

    async def close(self, reason: str = "user", cancel_work: bool = False) -> VoiceSnapshot:
        if self._closing:
            if cancel_work:
                self._preserve_on_end = False
                self._pending = None
                self._pending_ready.set()
                self._wake.set()
                if self.snapshot.close_reason != reason:
                    # Reset/context changes can arrive after End confirmed media
                    # closure. Revoke preserved work durably before cancelling it.
                    self.snapshot.close_reason = reason
                    self.snapshot.revision += 1
                    self._pending = None
                    with contextlib.suppress(Exception):
                        await self._persist()
                self._cancel_answer()
            return self.snapshot
        self._closing = True
        self._preserve_on_end = reason == "user" and not cancel_work
        self.snapshot.playback_blocked = True
        self.snapshot.controller_connected = False
        self.snapshot.close_reason = reason
        if self.snapshot.status != "failed":
            self.snapshot.status = "closing"
        if not self._preserve_on_end:
            self._pending = None
        if cancel_work:
            self._cancel_answer()
        # End only removes voice playback. Already requested app work can finish and persist.
        with contextlib.suppress(Exception):
            await self._persist()
        self._pending_ready.set()
        self._wake.set()
        if self.connection is not None and not self._closed.is_set():
            with contextlib.suppress(Exception):
                await asyncio.wait_for(
                    send_event(self.connection, "session.close", event_id=uuid4().hex),
                    timeout=self.close_timeout,
                )
            if asyncio.current_task() is not self._receiver:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._closed.wait(), timeout=self.close_timeout)
        confirmed = self._closed.is_set()
        if not confirmed:
            confirmed = await self.provider.hangup(self.context.provider_session_id)
        # A session.closed event can finalize while hangup is awaiting its response.
        # Its confirmed terminal state wins over a late failed HTTP result.
        if self._shutdown_complete:
            return self.snapshot
        confirmed = confirmed or self._closed.is_set()
        if confirmed or time.monotonic() - self._start_time >= self.max_duration:
            await self._finalize_close(confirmed=confirmed)
        else:
            self.snapshot.status = "closing"
            self.snapshot.error = "live_close_unconfirmed"
            self._cleanup_pending = True
            with contextlib.suppress(Exception):
                await self._persist()
            # An attach failure has no watchdog yet. Keep the same bounded
            # cleanup path even when creation failed before the controller started.
            if (self._watchdog is None or self._watchdog.done()
                    or asyncio.current_task() is self._watchdog):
                self._watchdog = asyncio.create_task(
                    self._watch(), name=f"live-cleanup-{self.context.session_id}"
                )
        return self.snapshot

    async def _finalize_close(self, *, confirmed: bool) -> None:
        if self._shutdown_complete:
            return
        self._shutdown_complete = True
        self._cleanup_pending = False
        if not confirmed:
            self.snapshot.status = "failed"
            self.snapshot.error = "live_close_unconfirmed"
        elif self.snapshot.error == "live_close_unconfirmed":
            self.snapshot.status = "closed"
            self.snapshot.error = None
        elif self.snapshot.status != "failed":
            self.snapshot.status = "closed"
        if self.connection is not None:
            with contextlib.suppress(Exception):
                await self.connection.close()
        for task in (self._receiver, self._watchdog):
            if task is not None and task is not asyncio.current_task():
                if task is self._receiver and self._preserve_on_end and self._receiving_delegations:
                    continue
                task.cancel()
        if self._worker is not None and not self.has_pending_work and self._worker is not asyncio.current_task():
            self._worker.cancel()
        with contextlib.suppress(Exception):
            await self._persist()


def speech_chunks(text: str, max_bytes: int = 450) -> list[str]:
    """UTF-8 byte bound also bounds BPE tokens to fewer than the API's 500 limit."""
    chunks: list[str] = []
    current = ""
    for word in text.split():
        candidate = f"{current} {word}".strip()
        if len(candidate.encode()) > max_bytes:
            if current:
                chunks.append(current)
            current = ""
            for character in word:
                if len((current + character).encode()) > max_bytes:
                    chunks.append(current)
                    current = ""
                current += character
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks
