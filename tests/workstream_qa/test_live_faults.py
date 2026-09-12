"""W5 controller event races using declared media/provider doubles, never real audio."""

import asyncio

from test_voice import (
    FakeProvider,
    NoCloseAcknowledgment,
    controller,
    delegation,
    eventually,
    transcript,
)


async def test_correction_fragment_cancels_without_dispatch_until_fresh_delegation():
    ctl = controller()
    await ctl.start()
    try:
        await ctl.handle_event(transcript(text="Summarize PT-006."))
        await ctl.handle_event(delegation())
        await eventually(lambda: ctl.snapshot.last_run_id == "run-1")
        assert len(ctl.hooks.intents) == 1
        await ctl.handle_event(transcript("correction-fragment", " Actually", 150))
        assert ctl.snapshot.playback_blocked
        # More than the configured settling period: a pause is not a new delegation.
        await asyncio.sleep(0.05)
        assert len(ctl.hooks.intents) == 1, "Correction fragment redispatched the old delegation"
        await ctl.handle_event(transcript("correction-remainder", " I meant PT-005.", 170))
        await ctl.handle_event(delegation("new-event", "new-delegation", 210))
        await eventually(lambda: len(ctl.hooks.intents) == 2)
        assert ctl.hooks.intents[-1].delegation_id == "new-delegation"
        assert ctl.hooks.intents[-1].question.endswith("Actually I meant PT-005.")
    finally:
        await ctl.close(cancel_work=True)


async def test_close_ack_during_pending_hangup_is_terminal_and_cannot_be_overwritten():
    started, release = asyncio.Event(), asyncio.Event()

    class GatedHangup(FakeProvider):
        def __init__(self):
            super().__init__()
            self.connection = NoCloseAcknowledgment()

        async def hangup(self, session_id):
            started.set()
            await release.wait()
            return False

    ctl = controller(provider=GatedHangup())
    await ctl.start()
    closing = asyncio.create_task(ctl.close())
    try:
        await asyncio.wait_for(started.wait(), 1)
        await ctl.provider.connection.queue.put({
            "type": "session.closed", "event_id": "late-close-ack", "usage": {"seconds": 4.0},
            "reason": "close_requested",
        })
        await eventually(lambda: ctl._shutdown_complete)
        assert ctl.snapshot.status == "closed"
        release.set()
        result = await asyncio.wait_for(closing, 1)
        assert result.status == "closed", "Confirmed terminal close regressed to unconfirmed cleanup"
        assert result.error is None and result.final_usage_confirmed
        assert not ctl._cleanup_pending
        assert ctl.hooks.snapshots[-1].status == "closed"
    finally:
        release.set()
        await asyncio.gather(closing, return_exceptions=True)
