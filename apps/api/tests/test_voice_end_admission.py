"""End races at actual await boundaries; all media and answer providers are doubles."""

import asyncio

import pytest
from emer.contracts.voice import VoiceAnswer
from test_voice import Hooks, controller, delegation, eventually, transcript


class AdmissionGate(Hooks):
    def __init__(self, boundary):
        super().__init__()
        self.boundary = boundary
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def pause(self, boundary):
        if boundary == self.boundary:
            self.entered.set()
            await self.release.wait()

    async def claim_event(self, session_id, fence, event_id, kind, payload):
        if kind == "session.delegation.created":
            await self.pause("event")
        return await super().claim_event(session_id, fence, event_id, kind, payload)

    async def save_snapshot(self, session_id, fence, snapshot):
        if snapshot.revision == 1 and snapshot.status == "working":
            await self.pause("snapshot")
        return await super().save_snapshot(session_id, fence, snapshot)

    async def claim_delegation(self, intent):
        await self.pause("claim")
        return await super().claim_delegation(intent)


@pytest.mark.parametrize("boundary", ["event", "snapshot", "stop", "settle", "claim"])
async def test_end_preserves_received_delegation_at_each_admission_boundary(boundary):
    hooks = AdmissionGate(boundary)
    ctl = controller(hooks=hooks)
    answer_started, finish_answer = asyncio.Event(), asyncio.Event()
    calls = []

    async def answer(intent):
        calls.append(intent)
        answer_started.set()
        await finish_answer.wait()
        return VoiceAnswer(run_id="last-request", spoken_text="No deposit is documented.", accepted=True)

    hooks.handler = answer
    original_send = ctl._send

    async def send(kind, **kwargs):
        if kind == "session.instructions.append" and kwargs.get("delegation_id") is None:
            await hooks.pause("stop")
        return await original_send(kind, **kwargs)

    ctl._send = send
    if boundary == "settle":
        ctl.settle_seconds = 0.1
    await ctl.start()
    incoming = closing = None
    try:
        await ctl.handle_event(transcript(text="What is the exact deposit patient four already paid?"))
        incoming = asyncio.create_task(ctl.handle_event(delegation()))
        if boundary == "settle":
            await incoming
            assert ctl._pending is not None and ctl._active_answer is None
        else:
            await asyncio.wait_for(hooks.entered.wait(), 1)
        closing = asyncio.create_task(ctl.close())
        await eventually(lambda: ctl._closing)
        assert ctl.snapshot.playback_blocked and not ctl.snapshot.controller_connected
        if boundary == "snapshot":
            # The durable blocked write must finish before the closed write.
            hooks.release.set()
        ended = await asyncio.wait_for(closing, 1)
        assert ended.close_reason == "user" and ended.status == "closed"
        assert ctl.has_pending_work and not ctl._worker.done()
        await ctl.handle_event({
            "type": "error", "event_id": "late-injection-error",
            "error": {"code": "context_injection_incomplete", "type": "server_error"},
        })
        assert "late-injection-error" in hooks.events
        assert ctl.snapshot.error is None and ctl.snapshot.close_reason == "user"
        assert ctl.snapshot.final_usage_confirmed and ctl.snapshot.usage_seconds == 3.5
        hooks.release.set()
        await asyncio.wait_for(incoming, 1)
        await asyncio.wait_for(answer_started.wait(), 1)
        assert len(calls) == len(hooks.intents) == 1
        assert calls[0].delegation_id == "item_opaque"
        finish_answer.set()
        await asyncio.wait_for(ctl._worker, 1)
        await eventually(lambda: not ctl.has_pending_work)
        assert ctl.snapshot.last_run_id == hooks.snapshots[-1].last_run_id == "last-request"
        assert hooks.snapshots[-1].status == "closed"
        assert not any(e["type"] == "session.commentary.append" for e in ctl.connection.sent)

        # A new event after End is telemetry only, never another accepted task.
        await ctl.handle_event(delegation("too-late", "late-task", 200))
        assert len(hooks.intents) == 1 and ctl.snapshot.last_delegation_id == "item_opaque"
    finally:
        hooks.release.set()
        finish_answer.set()
        await ctl.close("test_cleanup", cancel_work=True)
        await asyncio.gather(*(t for t in [incoming, closing, ctl._worker] if t), return_exceptions=True)


@pytest.mark.parametrize("reason", ["owner_reset", "context_changed", "server_shutdown"])
@pytest.mark.parametrize("boundary", ["event", "claim"])
async def test_end_then_revocation_cancels_admission_before_an_answer_exists(reason, boundary):
    hooks = AdmissionGate(boundary)
    ctl = controller(hooks=hooks)
    answered = []

    async def answer(intent):
        answered.append(intent)
        return VoiceAnswer(run_id="must-not-exist", spoken_text="Must not speak.", accepted=True)

    hooks.handler = answer
    await ctl.start()
    incoming = None
    try:
        await ctl.handle_event(transcript())
        incoming = asyncio.create_task(ctl.handle_event(delegation()))
        await asyncio.wait_for(hooks.entered.wait(), 1)
        await ctl.close()
        revision = ctl.snapshot.revision
        assert ctl.has_pending_work and ctl._active_answer is None
        await ctl.close(reason, cancel_work=True)
        assert ctl.snapshot.revision == revision + 1
        hooks.release.set()
        await asyncio.wait_for(incoming, 1)
        await asyncio.wait_for(ctl._worker, 1)
        assert answered == [] and not ctl.has_pending_work
        assert ctl.snapshot.close_reason == reason and ctl.snapshot.last_run_id is None
    finally:
        hooks.release.set()
        await ctl.close("test_cleanup", cancel_work=True)
        await asyncio.gather(*(t for t in [incoming, ctl._worker] if t), return_exceptions=True)


async def test_unexpected_provider_error_still_cancels_current_answer_and_records_error():
    ctl = controller()
    started, cancelled = asyncio.Event(), asyncio.Event()

    async def answer(intent):
        started.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    ctl.hooks.handler = answer
    await ctl.start()
    await ctl.handle_event(transcript())
    await ctl.handle_event(delegation())
    await asyncio.wait_for(started.wait(), 1)
    await ctl.handle_event({
        "type": "error", "event_id": "unexpected-error", "error": {"code": "invalid_request"},
    })
    await asyncio.wait_for(cancelled.wait(), 1)
    await asyncio.gather(ctl._worker, return_exceptions=True)
    assert ctl.snapshot.close_reason == ctl.snapshot.error == "live_provider_command_failed"
    assert "unexpected-error" in ctl.hooks.events and ctl.snapshot.playback_blocked
    assert ctl.snapshot.last_run_id is None


async def test_end_drains_cancelled_previous_slot_before_preserving_received_replacement():
    ctl = controller()
    started, cancelled, release_old = asyncio.Event(), asyncio.Event(), asyncio.Event()
    answered = []

    async def answer(intent):
        answered.append(intent.delegation_id)
        if intent.delegation_id == "item_opaque":
            started.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                cancelled.set()
                await release_old.wait()
                raise
        return VoiceAnswer(run_id="replacement", spoken_text="No paid deposit is documented.", accepted=True)

    ctl.hooks.handler = answer
    await ctl.start()
    try:
        await ctl.handle_event(transcript(text="Summarize patient six."))
        await ctl.handle_event(delegation())
        await asyncio.wait_for(started.wait(), 1)
        await ctl.handle_event(transcript("new-question", " What deposit did patient four pay?", 150))
        await ctl.handle_event(delegation("new-event", "replacement-task", 200))
        await asyncio.wait_for(cancelled.wait(), 1)
        await ctl.close()
        assert answered == ["item_opaque"] and ctl._pending == ("replacement-task", 2)
        release_old.set()
        await asyncio.wait_for(ctl._worker, 1)
        assert answered == ["item_opaque", "replacement-task"]
        assert ctl.snapshot.last_run_id == "replacement"
        assert not any(e["type"] == "session.commentary.append" for e in ctl.connection.sent)
    finally:
        release_old.set()
        await ctl.close("test_cleanup", cancel_work=True)
        await asyncio.gather(ctl._worker, return_exceptions=True)
