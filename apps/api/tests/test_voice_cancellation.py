"""Cancellation/cleanup uses task and provider doubles, never a real provider or database."""

import asyncio

import pytest
from emer.contracts.voice import VoiceAnswer
from test_voice import Hooks, controller, delegation, eventually, transcript


async def start_answer(ctl, started):
    await ctl.start()
    await ctl.handle_event(transcript(text="Ask about PT-001."))
    await ctl.handle_event(delegation())
    await asyncio.wait_for(started.wait(), 1)


async def test_repeated_corrections_cancel_once_and_wait_for_cleanup_before_latest_delegation():
    hooks = Hooks()
    started, cleaning, release, finished = (asyncio.Event() for _ in range(4))
    cleanup_interrupted = False

    async def answer(intent):
        nonlocal cleanup_interrupted
        if intent.delegation_id != "item_opaque":
            assert finished.is_set()
            return VoiceAnswer(run_id="latest-run", spoken_text="Latest checked answer.", accepted=True)
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cleaning.set()
            try:
                await release.wait()
                finished.set()
            except asyncio.CancelledError:
                cleanup_interrupted = True
                raise
            raise

    hooks.handler = answer
    ctl = controller(hooks=hooks)
    await start_answer(ctl, started)
    old_task = ctl._active_answer
    try:
        await ctl.handle_event(transcript("correction", " Actually I meant PT-004.", 150))
        await asyncio.wait_for(cleaning.wait(), 1)
        await ctl.handle_event(delegation("second-event", "item_second", 200))
        await ctl.handle_event(transcript("third-correction", " Correction, use PT-005.", 250))
        await ctl.handle_event(delegation("latest-event", "item_latest", 300))
        await asyncio.sleep(0.02)

        assert old_task.cancelling() == 1
        assert ctl._active_answer is old_task and not old_task.done()
        assert len(hooks.intents) == 1
        assert not cleanup_interrupted
        release.set()
        await eventually(lambda: ctl.snapshot.last_run_id == "latest-run")
        assert [intent.delegation_id for intent in hooks.intents] == ["item_opaque", "item_latest"]
        assert finished.is_set() and not cleanup_interrupted
    finally:
        release.set()
        await ctl.close(cancel_work=True)
        await asyncio.gather(old_task, return_exceptions=True)


@pytest.mark.parametrize("late_failure", [False, True])
async def test_stalled_cancellation_closes_promptly_retains_slot_and_consumes_late_completion(late_failure):
    hooks = Hooks()
    started, release = asyncio.Event(), asyncio.Event()
    cancellations = 0

    async def answer(intent):
        nonlocal cancellations
        started.set()
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancellations += 1
        if late_failure:
            raise RuntimeError("late provider-double failure")
        return VoiceAnswer(run_id="obsolete", spoken_text="Obsolete answer.", accepted=True)

    hooks.handler = answer
    ctl = controller(hooks=hooks, answer_timeout_seconds=10, cancellation_timeout_seconds=0.03)
    await start_answer(ctl, started)
    old_task = ctl._active_answer
    try:
        await ctl.handle_event(delegation("corrected-event", "item_latest", 200))
        # This must close from the short cancellation bound, without waiting
        # for the ten-second answer timeout or launching another answer.
        await asyncio.wait_for(asyncio.gather(ctl._worker, return_exceptions=True), 0.5)
        assert ctl._shutdown_complete
        assert ctl.snapshot.playback_blocked and not ctl.snapshot.controller_connected
        assert ctl._active_answer is old_task and not old_task.done()
        assert len(hooks.intents) == 1 and cancellations == 1
        await ctl.close(cancel_work=True)
        assert old_task.cancelling() == 1

        release.set()
        await eventually(lambda: ctl._active_answer is None)
        assert old_task.done()
        assert not any(e["type"] == "session.commentary.append" for e in ctl.provider.connection.sent)
    finally:
        release.set()
        await asyncio.gather(old_task, return_exceptions=True)
        await ctl.close(cancel_work=True)


async def test_end_preserves_requested_answer_until_completion_without_cancelling_or_speaking():
    hooks = Hooks()
    started, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def answer(intent):
        started.set()
        await release.wait()
        finished.set()
        return VoiceAnswer(run_id="background", spoken_text="Finished after End.", accepted=True)

    hooks.handler = answer
    ctl = controller(hooks=hooks, cancellation_timeout_seconds=0.01)
    await start_answer(ctl, started)
    task = ctl._active_answer
    try:
        await ctl.close(cancel_work=False)
        await asyncio.sleep(0.02)
        assert ctl._active_answer is task and not task.done()
        assert task.cancelling() == 0 and not ctl._answer_cancelled.is_set()
        release.set()
        await asyncio.wait_for(ctl._worker, 1)
        assert finished.is_set() and ctl._active_answer is None
        assert not any(e["type"] == "session.commentary.append" for e in ctl.provider.connection.sent)
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await ctl.close(cancel_work=True)


async def test_failed_persisted_run_cleanup_closes_without_dispatching_pending_correction():
    hooks = Hooks()
    started = asyncio.Event()

    async def answer(intent):
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            raise RuntimeError("live_run_cancellation_unconfirmed") from None

    hooks.handler = answer
    ctl = controller(hooks=hooks)
    await start_answer(ctl, started)
    try:
        await ctl.handle_event(delegation("corrected-event", "item_latest", 200))
        await eventually(lambda: ctl._shutdown_complete)
        assert len(hooks.intents) == 1
        assert ctl.snapshot.playback_blocked and not ctl.snapshot.controller_connected
        assert not any(e["type"] == "session.commentary.append" for e in ctl.provider.connection.sent)
    finally:
        await ctl.close(cancel_work=True)


async def test_answer_timeout_cannot_hide_failed_persisted_run_cleanup():
    hooks = Hooks()
    started = asyncio.Event()

    async def answer(intent):
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            raise RuntimeError("live_run_cancellation_unconfirmed") from None

    hooks.handler = answer
    ctl = controller(hooks=hooks, answer_timeout_seconds=0.01)
    await start_answer(ctl, started)
    try:
        await eventually(lambda: ctl._shutdown_complete)
        assert ctl.snapshot.playback_blocked and not ctl.snapshot.controller_connected
        assert not any(e["type"] == "session.commentary.append" for e in ctl.provider.connection.sent)
    finally:
        await ctl.close(cancel_work=True)


@pytest.mark.parametrize("reason", ["user", "owner_reset", "context_changed"])
async def test_provider_close_ack_preserves_application_close_reason(reason):
    ctl = controller()
    await ctl.start()
    snapshot = await ctl.close(reason, cancel_work=reason != "user")
    assert snapshot.close_reason == reason
    assert snapshot.provider_close_reason == "close_requested"
    assert snapshot.status == "closed" and snapshot.final_usage_confirmed
    assert ctl.hooks.snapshots[-1].close_reason == reason


async def test_unsolicited_provider_closure_is_never_a_user_end_decision():
    ctl = controller()
    await ctl.start()
    await ctl.provider.connection.queue.put({
        "type": "session.closed", "event_id": "provider-close", "reason": "connection_lost",
        "usage": {"seconds": 2.0},
    })
    await eventually(lambda: ctl._shutdown_complete)
    assert ctl.snapshot.close_reason == "provider_closed"
    assert ctl.snapshot.provider_close_reason == "connection_lost"
