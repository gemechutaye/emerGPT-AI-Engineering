"""Dispatch races use provider doubles; these tests do not establish audible acceptance."""

import asyncio

import pytest
from emer.contracts.voice import VoiceAnswer
from test_voice import Hooks, UnconfirmedProvider, controller, delegation, transcript


class PausedDispatchHooks(Hooks):
    def __init__(self):
        super().__init__()
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.pause_next_dispatch = True

    async def renew(self, session_id, fence):
        task = asyncio.current_task()
        if self.pause_next_dispatch and task.get_name().startswith("live-work-"):
            self.pause_next_dispatch = False
            self.entered.set()
            await self.release.wait()
        return self.owned


def outgoing_for(connection, delegation_id):
    return [event for event in connection.sent if event.get("delegation_id") == delegation_id]


async def begin_paused_dispatch(kind, hooks, provider=None):
    ctl = controller(
        hooks=hooks, provider=provider, answer_timeout_seconds=0.1,
        max_duration_seconds=2, watch_interval_seconds=0.1,
    )
    await ctl.start()
    if kind != "clarification":
        await ctl.handle_event(transcript(text="Ask about PT-001."))
    await ctl.handle_event(delegation())
    await asyncio.wait_for(hooks.entered.wait(), 1)
    return ctl


@pytest.mark.parametrize("kind", ["answer", "clarification", "timeout"])
async def test_correction_during_dispatch_renewal_suppresses_old_output_and_keeps_playback_blocked(kind):
    hooks = PausedDispatchHooks()
    next_started = asyncio.Event()

    async def answer(intent):
        if intent.delegation_id == "item_new":
            next_started.set()
            await asyncio.Event().wait()
        if kind == "answer":
            return VoiceAnswer(run_id="old-run", spoken_text="Old answer for PT-001.", accepted=True)
        await asyncio.Event().wait()

    hooks.handler = answer
    ctl = await begin_paused_dispatch(kind, hooks)
    try:
        await ctl.handle_event(transcript("correction", " Actually I meant PT-004.", 150))
        await ctl.handle_event(delegation("new-delegation-event", "item_new", 200))
        hooks.release.set()
        await asyncio.wait_for(next_started.wait(), 1)

        assert outgoing_for(ctl.provider.connection, "item_opaque") == []
        assert ctl.snapshot.status == "working"
        assert ctl.snapshot.playback_blocked
        assert ctl.snapshot.controller_connected
    finally:
        hooks.release.set()
        await ctl.close(cancel_work=True)


@pytest.mark.parametrize("kind", ["answer", "clarification", "timeout"])
async def test_end_during_dispatch_renewal_suppresses_output_while_cleanup_is_pending(kind):
    hooks = PausedDispatchHooks()

    async def answer(intent):
        if kind == "answer":
            return VoiceAnswer(run_id="old-run", spoken_text="Old answer for PT-001.", accepted=True)
        await asyncio.Event().wait()

    hooks.handler = answer
    ctl = await begin_paused_dispatch(kind, hooks, UnconfirmedProvider())
    try:
        result = await ctl.close()
        assert result.status == "closing"
        hooks.release.set()
        await asyncio.wait_for(ctl._worker, 1)

        assert outgoing_for(ctl.provider.connection, "item_opaque") == []
        assert ctl.snapshot.status == "closing"
        assert ctl.snapshot.playback_blocked
        assert not ctl.snapshot.controller_connected
        assert ctl.snapshot.error == "live_close_unconfirmed"
    finally:
        hooks.release.set()
        await ctl._finalize_close(confirmed=False)
