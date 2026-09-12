"""Correction fragments invalidate speech; only provider delegation starts work."""

import asyncio

from emer.contracts.voice import VoiceAnswer
from test_voice import controller, delegation, eventually, transcript


async def test_partial_split_correction_waits_for_new_delegation_and_coalesces():
    ctl = controller()
    await ctl.start()
    try:
        await ctl.handle_event(transcript(text="Ask about PT-006."))
        await ctl.handle_event(delegation())
        await eventually(lambda: ctl.snapshot.last_run_id == "run-1")
        initial_instructions = sum(
            event["type"] == "session.instructions.append" for event in ctl.provider.connection.sent
        )
        await ctl.handle_event(transcript("split-a", " Actu", 150))
        await ctl.handle_event(transcript("split-b", "ally", 170))
        assert ctl.snapshot.playback_blocked
        revision = ctl.snapshot.revision
        await ctl.handle_event(transcript("split-c", " I meant PT-005.", 190))
        await asyncio.sleep(0.05)
        assert len(ctl.hooks.intents) == 1
        assert ctl.snapshot.revision == revision
        assert sum(
            event["type"] == "session.instructions.append" for event in ctl.provider.connection.sent
        ) == initial_instructions
        await ctl.handle_event(delegation("new-event", "new-task", 220))
        await eventually(lambda: len(ctl.hooks.intents) == 2)
        assert sum(
            event["type"] == "session.instructions.append" for event in ctl.provider.connection.sent
        ) == initial_instructions + 1
        assert ctl.hooks.intents[-1].delegation_id == "new-task"
        assert ctl.hooks.intents[-1].question.endswith("Actually I meant PT-005.")
    finally:
        await ctl.close(cancel_work=True)


async def test_correction_cues_coalesce_until_fresh_delegation_then_can_interrupt_again():
    ctl = controller()
    await ctl.start()
    try:
        await ctl.handle_event(transcript(text="Ask about PT-006."))
        await ctl.handle_event(delegation())
        await eventually(lambda: ctl.snapshot.last_run_id == "run-1")
        first_revision = ctl.snapshot.revision
        await ctl.handle_event(transcript("no", " No,", 150))
        assert ctl.snapshot.revision == first_revision + 1
        assert ctl.snapshot.playback_blocked
        await ctl.handle_event(transcript("mean", " I mean", 170))
        await ctl.handle_event(transcript("patient", " patient four.", 190))
        assert ctl.snapshot.revision == first_revision + 1
        assert sum(
            event["type"] == "session.instructions.append" for event in ctl.provider.connection.sent
        ) == 1  # Only the initial delegation; no steering during the correction.

        # Duplicate and late old delegations must not reset the pending correction.
        await ctl.handle_event(delegation("duplicate-event", "item_opaque", 220))
        await ctl.handle_event(delegation("old-event", "old-task", 50))
        await ctl.handle_event(transcript("another-cue", " Actually patient five.", 210))
        assert ctl.snapshot.revision == first_revision + 1
        assert len(ctl.hooks.intents) == 1

        await ctl.handle_event(delegation("fresh-event", "fresh-task", 250))
        await eventually(lambda: len(ctl.hooks.intents) == 2 and not ctl.snapshot.playback_blocked)
        assert sum(
            event["type"] == "session.instructions.append" for event in ctl.provider.connection.sent
        ) == 2  # The fresh delegation sends one stop before replacement work.
        fresh_revision = ctl.snapshot.revision
        await ctl.handle_event(transcript("next-no", " No,", 270))
        assert ctl.snapshot.revision == fresh_revision + 1
        assert ctl.snapshot.playback_blocked
        await ctl.handle_event(transcript("next-mean", " I mean patient six.", 290))
        assert ctl.snapshot.revision == fresh_revision + 1
        assert sum(
            event["type"] == "session.instructions.append" for event in ctl.provider.connection.sent
        ) == 2  # The next correction also stays local until delegation.
    finally:
        await ctl.close(cancel_work=True)


async def test_correction_cue_cancels_work_without_steering_and_suppresses_late_result():
    ctl = controller()
    started, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def answer(intent):
        if intent.delegation_id == "item_opaque":
            started.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancelled.set()
                await release.wait()
            return VoiceAnswer(run_id="obsolete", spoken_text="Obsolete patient six answer.", accepted=True)
        # Replacement work starts only after its blocked revision and stop command.
        assert ctl.hooks.snapshots[-1].revision == intent.revision
        assert ctl.hooks.snapshots[-1].playback_blocked
        assert ctl.provider.connection.sent[-1]["type"] == "session.instructions.append"
        return VoiceAnswer(run_id="corrected", spoken_text="Current patient four answer.", accepted=True)

    ctl.hooks.handler = answer
    await ctl.start()
    old_task = None
    try:
        await ctl.handle_event(transcript(text="Ask about patient six."))
        await ctl.handle_event(delegation())
        await asyncio.wait_for(started.wait(), 1)
        old_task = ctl._active_answer
        first_revision = ctl.snapshot.revision
        sent_before_cue = list(ctl.provider.connection.sent)

        await ctl.handle_event(transcript("cue", " No,", 150))
        await asyncio.wait_for(cancelled.wait(), 0.5)
        assert ctl.snapshot.revision == first_revision + 1
        assert ctl.hooks.snapshots[-1].revision == first_revision + 1
        assert ctl.hooks.snapshots[-1].playback_blocked
        assert old_task.cancelling() == 1
        assert ctl.provider.connection.sent == sent_before_cue

        await ctl.handle_event(transcript("meaning", " I mean", 170))
        await ctl.handle_event(transcript("subject", " patient four.", 190))
        assert ctl.snapshot.revision == first_revision + 1
        assert ctl.provider.connection.sent == sent_before_cue
        release.set()
        await asyncio.wait_for(asyncio.shield(old_task), 1)
        await eventually(lambda: ctl._active_answer is None)
        assert ctl.snapshot.last_run_id is None
        assert ctl.snapshot.playback_blocked
        assert ctl.provider.connection.sent == sent_before_cue

        await ctl.handle_event(delegation("fresh-event", "fresh-task", 220))
        await eventually(lambda: ctl.snapshot.last_run_id == "corrected")
        assert ctl.hooks.intents[-1].question.endswith("No, I mean patient four.")
        assert [event["type"] for event in ctl.provider.connection.sent] == [
            "session.instructions.append", "session.instructions.append", "session.commentary.append",
        ]
        assert ctl.provider.connection.sent[-1]["content"] == "Current patient four answer."
    finally:
        release.set()
        await ctl.close(cancel_work=True)
        if old_task is not None:
            await asyncio.gather(old_task, return_exceptions=True)


async def test_old_negation_or_correction_does_not_retrigger_when_user_says_thanks():
    ctl = controller()
    await ctl.start()
    try:
        await ctl.handle_event(transcript(text="Actually, what is not documented for PT-007?"))
        await ctl.handle_event(delegation())
        await eventually(lambda: ctl.snapshot.last_run_id == "run-1")
        revision = ctl.snapshot.revision
        await ctl.handle_event(transcript("thanks", " Thanks.", 150))
        await asyncio.sleep(0.05)
        assert len(ctl.hooks.intents) == 1
        assert ctl.snapshot.revision == revision
        assert not ctl.snapshot.playback_blocked
    finally:
        await ctl.close(cancel_work=True)


async def test_late_old_correction_fragment_does_not_invalidate_current_task():
    ctl = controller()
    await ctl.start()
    try:
        await ctl.handle_event(transcript(text="Summarize the current request."))
        await ctl.handle_event(delegation(offset=500))
        await eventually(lambda: ctl.snapshot.last_run_id == "run-1")
        revision = ctl.snapshot.revision
        await ctl.handle_event(transcript("late-old", " Actually", 100))
        await asyncio.sleep(0.02)
        assert ctl.snapshot.revision == revision
        assert len(ctl.hooks.intents) == 1
    finally:
        await ctl.close(cancel_work=True)


async def test_correction_during_delegation_claim_does_not_dispatch_obsolete_answer():
    ctl = controller()
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def claim(intent):
        entered.set()
        await release.wait()
        return True

    async def answer(intent):
        calls.append(intent)
        raise AssertionError("Obsolete claimed delegation reached provider-backed answer")

    ctl.hooks.claim_delegation = claim
    ctl.hooks.handler = answer
    await ctl.start()
    try:
        await ctl.handle_event(transcript())
        await ctl.handle_event(delegation())
        await asyncio.wait_for(entered.wait(), 1)
        await ctl.handle_event(transcript("correction", " Actually", 150))
        release.set()
        await asyncio.sleep(0.02)
        assert not calls
        assert ctl.snapshot.playback_blocked
    finally:
        release.set()
        await ctl.close(cancel_work=True)


async def test_delegation_waits_for_persisted_block_and_stop_before_answer():
    ctl = controller()
    entered, release = asyncio.Event(), asyncio.Event()
    save = ctl.hooks.save_snapshot

    async def gated_save(session_id, fence, snapshot):
        if snapshot.revision == 1 and snapshot.status == "working":
            entered.set()
            await release.wait()
        return await save(session_id, fence, snapshot)

    ctl.hooks.save_snapshot = gated_save
    await ctl.start()
    pending = None
    try:
        await ctl.handle_event(transcript())
        pending = asyncio.create_task(ctl.handle_event(delegation()))
        await asyncio.wait_for(entered.wait(), 1)
        await asyncio.sleep(0.02)
        assert not ctl.hooks.intents
        release.set()
        await pending
        await eventually(lambda: ctl.snapshot.last_run_id == "run-1")
        kinds = [event["type"] for event in ctl.provider.connection.sent]
        assert kinds == ["session.instructions.append", "session.commentary.append"]
    finally:
        release.set()
        if pending:
            await pending
        await ctl.close(cancel_work=True)


async def test_no_patient_correction_blocks_previous_speech_and_exposes_delegation_identity():
    ctl = controller()
    await ctl.start()
    try:
        await ctl.handle_event(transcript(text="Ask about PT-006."))
        await ctl.handle_event(delegation())
        await eventually(lambda: ctl.snapshot.last_run_id == "run-1")
        old_delegation = ctl.snapshot.last_delegation_id
        await ctl.handle_event(transcript("no-correction", " No, PT-004.", 150))
        assert ctl.snapshot.playback_blocked
        await ctl.handle_event(delegation("fresh-event", "fresh-task", 220))
        await eventually(lambda: len(ctl.hooks.intents) == 2)
        assert ctl.snapshot.last_delegation_id == "fresh-task"
        assert old_delegation != "fresh-task"
    finally:
        await ctl.close(cancel_work=True)
