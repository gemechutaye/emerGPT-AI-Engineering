"""Delegation timeline bounds separate the current request from retained history."""

import pytest
from emer.contracts.voice import VoiceIntent
from emer.domain.utterance import correction_suffix
from pydantic import ValidationError
from test_voice import controller, delegation, eventually, transcript


def test_request_start_is_backward_compatible_and_nonnegative():
    intent = VoiceIntent(
        session_id="live", provider_session_id="provider", conversation_id="conversation",
        context_version=1, index_id="index", fence=1, revision=1, delegation_id="task",
        question="What is documented?", transcript=[],
    )
    assert intent.request_start_ms == 0
    with pytest.raises(ValidationError):
        VoiceIntent.model_validate({**intent.model_dump(), "request_start_ms": -1})


async def test_new_followup_segment_excludes_old_correction_without_discarding_history():
    ctl = controller()
    await ctl.start()
    try:
        await ctl.handle_event(transcript(text="What is patient six's status?"))
        await ctl.handle_event(delegation(offset=100))
        await eventually(lambda: len(ctl.hooks.intents) == 1)
        assert ctl.hooks.intents[0].request_start_ms == 0

        await ctl.handle_event(transcript("correction", " No, I mean patient four. What is their status?", 150))
        await ctl.handle_event(delegation("correction-event", "corrected-task", 220))
        await eventually(lambda: len(ctl.hooks.intents) == 2)
        assert ctl.hooks.intents[1].request_start_ms == 100

        await ctl.handle_event(transcript("followup", " What is not documented?", 270))
        await ctl.handle_event(delegation("followup-event", "followup-task", 320))
        await eventually(lambda: len(ctl.hooks.intents) == 3)
        intent = ctl.hooks.intents[-1]
        assert intent.request_start_ms == 220
        current = "".join(
            fragment.delta for fragment in intent.transcript
            if fragment.speaker == "user" and fragment.end_ms > intent.request_start_ms
        ).strip()
        assert current == "What is not documented?"
        assert correction_suffix(current) is None
        assert "I mean patient four" in intent.question
        assert [fragment.event_id for fragment in intent.transcript] == [
            "transcript-1", "correction", "followup",
        ]
    finally:
        await ctl.close(cancel_work=True)


async def test_duplicate_and_late_delegations_do_not_advance_request_start():
    ctl = controller()
    await ctl.start()
    try:
        await ctl.handle_event(transcript(text="Summarize patient six."))
        await ctl.handle_event(delegation(offset=100))
        await eventually(lambda: len(ctl.hooks.intents) == 1)
        await ctl.handle_event(delegation("duplicate-event", "item_opaque", 900))
        await ctl.handle_event(delegation("late-event", "late-task", 50))
        assert len(ctl.hooks.intents) == 1

        await ctl.handle_event(transcript("second-input", " What is their follow-up?", 150))
        await ctl.handle_event(delegation("second-event", "second-task", 220))
        await eventually(lambda: len(ctl.hooks.intents) == 2)
        assert ctl.hooks.intents[-1].request_start_ms == 100

        await ctl.handle_event(delegation("duplicate-second", "second-task", 1000))
        await ctl.handle_event(delegation("late-second", "another-late-task", 170))
        await ctl.handle_event(transcript("third-input", " What is not documented?", 270))
        await ctl.handle_event(delegation("third-event", "third-task", 320))
        await eventually(lambda: len(ctl.hooks.intents) == 3)
        assert ctl.hooks.intents[-1].request_start_ms == 220
        assert ctl.hooks.intents[-1].delegation_id == "third-task"
    finally:
        await ctl.close(cancel_work=True)
