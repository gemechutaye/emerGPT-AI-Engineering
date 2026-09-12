import asyncio
import json

import httpx
import pytest
from emer.contracts.voice import VoiceAnswer, VoiceContext
from emer.providers.openai_live import LiveProviderError, OpenAILive
from emer.providers.openrouter import ProviderError
from emer.services.voice import VoiceController, speech_chunks


class FakeConnection:
    def __init__(self):
        self.sent = []
        self.queue = asyncio.Queue()
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        value = await self.queue.get()
        if value is None:
            raise StopAsyncIteration
        return json.dumps(value)

    async def send(self, value):
        event = json.loads(value)
        self.sent.append(event)
        if event["type"] == "session.close":
            await self.queue.put({
                "type": "session.closed", "event_id": "closed",
                "usage": {"seconds": 3.5}, "reason": "close_requested",
            })

    async def close(self):
        self.closed = True
        await self.queue.put(None)


class FakeProvider:
    def __init__(self):
        self.connection = FakeConnection()
        self.hangups = []

    async def attach(self, session_id):
        return self.connection

    async def hangup(self, session_id):
        self.hangups.append(session_id)
        return True


class NoCloseAcknowledgment(FakeConnection):
    async def send(self, value):
        self.sent.append(json.loads(value))


class UnconfirmedProvider(FakeProvider):
    def __init__(self, succeeds_after=None):
        super().__init__()
        self.connection = NoCloseAcknowledgment()
        self.succeeds_after = succeeds_after

    async def hangup(self, session_id):
        self.hangups.append(session_id)
        return self.succeeds_after is not None and len(self.hangups) >= self.succeeds_after


class Hooks:
    def __init__(self):
        self.snapshots = []
        self.events = {}
        self.intents = []
        self.owned = True
        self.handler = None

    async def save_snapshot(self, session_id, fence, snapshot):
        self.snapshots.append(snapshot)
        return self.owned

    async def claim_event(self, session_id, fence, event_id, kind, payload):
        if event_id in self.events or not self.owned:
            return False
        self.events[event_id] = payload
        return True

    async def claim_delegation(self, intent):
        self.intents.append(intent)
        return self.owned

    async def renew(self, session_id, fence):
        return self.owned

    async def answer(self, intent):
        if self.handler:
            return await self.handler(intent)
        return VoiceAnswer(run_id="run-1", spoken_text="The requested exact price is not supplied.", accepted=True)


def controller(hooks=None, provider=None, **kwargs):
    context = VoiceContext(
        session_id="owned-1", provider_session_id="live_opaque", conversation_id="conversation-1",
        context_version=1, index_id="bundle-1", fence=3,
    )
    return VoiceController(
        context, provider or FakeProvider(), hooks or Hooks(), settle_seconds=0,
        close_timeout_seconds=0.02, **kwargs,
    )


def transcript(event_id="transcript-1", text="What is the price?", start=1):
    return {"type": "session.input_transcript.delta", "event_id": event_id, "delta": text,
            "start_ms": start, "end_ms": start + 10}


def delegation(event_id="delegation-event-1", delegation_id="item_opaque", offset=100):
    return {"type": "session.delegation.created", "event_id": event_id, "offset_ms": offset,
            "delegation": {"id": delegation_id, "target": "client"}}


async def eventually(predicate):
    async with asyncio.timeout(1):
        while not predicate():
            await asyncio.sleep(0.005)


async def test_provider_uses_exact_live_schema_and_preserves_session_id():
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(201, json={"session": {"id": "live_opaque-123"},
                                        "transport": {"type": "webrtc", "sdp": "v=0\r\nanswer"}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        provider = OpenAILive("private-test-key", client=client)
        result = await provider.create("v=0\r\nvalid offer")
    assert result.session_id == "live_opaque-123"
    assert str(requests[0].url) == "https://api.openai.com/v1/live/sessions"
    payload = json.loads(requests[0].content)
    assert payload["session"]["model"] == "gpt-live-1"
    assert payload["session"]["store"] is False
    assert payload["session"]["delegation"] == {"type": "client"}
    assert payload["transport"]["sdp"] == "v=0\r\nvalid offer"


async def test_uncertain_creation_is_not_retried_or_exposed():
    attempts = 0

    def respond(request):
        nonlocal attempts
        attempts += 1
        raise httpx.ReadTimeout("private transport detail", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(LiveProviderError) as caught:
            await OpenAILive("private-test-key", client=client).create("v=0\r\nvalid offer")
    assert attempts == 1
    assert caught.value.uncertain
    assert str(caught.value) == "live_creation_outcome_unknown"


async def test_answer_provider_busy_is_retained_through_confirmed_voice_close():
    hooks = Hooks()

    async def answer(intent):
        raise ProviderError("provider_rate_limited", "Provider response is private", retryable=True)

    hooks.handler = answer
    ctl = controller(hooks=hooks)
    await ctl.start()
    try:
        await ctl.handle_event(transcript())
        await ctl.handle_event(delegation())
        await eventually(lambda: ctl._shutdown_complete)
        assert ctl.snapshot.status == "closed"
        assert ctl.snapshot.error == "provider_rate_limited"
        assert ctl.snapshot.playback_blocked
        assert len(hooks.intents) == 1
    finally:
        await ctl.close()


async def test_transcript_and_delegation_duplicates_do_not_launch_duplicate_work():
    ctl = controller()
    await ctl.start()
    try:
        await ctl.handle_event(transcript())
        await ctl.handle_event(transcript())
        await ctl.handle_event(delegation())
        await ctl.handle_event(delegation("other-event-same-delegation"))
        await eventually(lambda: ctl.snapshot.last_run_id == "run-1")
        assert len(ctl.hooks.intents) == 1
        assert ctl.hooks.intents[0].question == "What is the price?"
        assert ctl.hooks.intents[0].index_id == "bundle-1"
        assert ctl.hooks.intents[0].fence == 3
    finally:
        await ctl.close()
    assert ctl.snapshot.final_usage_confirmed
    assert ctl.snapshot.usage_seconds == 3.5


async def test_missing_transcript_asks_for_repeat_without_backend_call():
    ctl = controller()
    await ctl.start()
    try:
        await ctl.handle_event(delegation())
        await eventually(lambda: ctl.snapshot.status == "listening")
        assert not ctl.hooks.intents
        assert "repeat" in ctl.provider.connection.sent[-1]["content"]
    finally:
        await ctl.close()


async def test_correction_cancels_previous_work_and_only_latest_result_is_spoken():
    hooks = Hooks()
    started, cancelled = asyncio.Event(), asyncio.Event()

    async def answer(intent):
        if len(hooks.intents) == 1:
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise
        return VoiceAnswer(run_id="latest-run", spoken_text="Current checked answer.", accepted=True)

    hooks.handler = answer
    ctl = controller(hooks=hooks)
    await ctl.start()
    try:
        await ctl.handle_event(transcript(text="Ask about PT-001."))
        await ctl.handle_event(delegation())
        await asyncio.wait_for(started.wait(), 1)
        await ctl.handle_event(transcript("correction", " Actually I meant PT-004.", 150))
        assert ctl.snapshot.playback_blocked
        await ctl.handle_event(delegation("delegation-new", "item_new", 200))
        await eventually(lambda: ctl.snapshot.last_run_id == "latest-run")
        assert cancelled.is_set()
        assert len(hooks.intents) == 2
        assert "PT-004" in hooks.intents[-1].question
        speech = [event["content"] for event in ctl.provider.connection.sent
                  if event["type"] == "session.commentary.append"]
        assert speech == ["Current checked answer."]
    finally:
        await ctl.close()


async def test_out_of_order_old_delegation_cannot_replace_latest():
    ctl = controller()
    await ctl.start()
    try:
        await ctl.handle_event(transcript())
        await ctl.handle_event(delegation("new-event", "item_new", 200))
        await ctl.handle_event(delegation("old-event", "item_old", 100))
        await eventually(lambda: ctl.snapshot.last_run_id == "run-1")
        assert [i.delegation_id for i in ctl.hooks.intents] == ["item_new"]
    finally:
        await ctl.close()


async def test_sideband_loss_blocks_playback_and_hangs_up():
    ctl = controller()
    await ctl.start()
    await ctl.provider.connection.queue.put(None)
    await eventually(lambda: bool(ctl.provider.hangups))
    assert ctl.snapshot.status == "failed"
    assert ctl.snapshot.playback_blocked
    assert not ctl.snapshot.controller_connected
    assert not ctl.snapshot.final_usage_confirmed
    assert ctl.provider.connection.closed


async def test_unconfirmed_end_retries_boundedly_until_session_deadline_without_claiming_closed():
    provider = UnconfirmedProvider()
    ctl = controller(provider=provider, max_duration_seconds=0.09, watch_interval_seconds=0.01)
    await ctl.start()
    snapshot = await ctl.close()
    assert snapshot.status == "closing"
    assert snapshot.error == "live_close_unconfirmed"
    assert not snapshot.final_usage_confirmed
    assert not provider.connection.closed
    await eventually(lambda: ctl._shutdown_complete)
    assert snapshot.status == "failed" and snapshot.error == "live_close_unconfirmed"
    assert len(provider.hangups) == 3  # initial close, one retry, final deadline attempt
    assert provider.connection.closed
    assert not snapshot.controller_connected and snapshot.playback_blocked


async def test_cleanup_retry_can_confirm_closure_without_inventing_final_usage():
    provider = UnconfirmedProvider(succeeds_after=2)
    ctl = controller(provider=provider, max_duration_seconds=0.2, watch_interval_seconds=0.01)
    await ctl.start()
    snapshot = await ctl.close()
    assert snapshot.status == "closing"
    await eventually(lambda: ctl._shutdown_complete)
    assert snapshot.status == "closed" and snapshot.error is None
    assert not snapshot.final_usage_confirmed
    assert len(provider.hangups) == 2
    assert provider.connection.closed


async def test_usage_is_cumulative_not_added_and_raw_audio_is_not_persisted():
    ctl = controller()
    await ctl.start()
    try:
        await ctl.handle_event({"type": "session.usage.updated", "event_id": "usage1", "usage": {"seconds": 10}})
        await ctl.handle_event({"type": "session.usage.updated", "event_id": "usage2", "usage": {"seconds": 12}})
        assert ctl.snapshot.usage_seconds == 12
        await ctl.provider.connection.queue.put({"type": "session.input_audio.append", "audio": "PRIVATE_AUDIO"})
        await asyncio.sleep(0.01)
        assert "PRIVATE_AUDIO" not in json.dumps(ctl.hooks.events)
    finally:
        await ctl.close()


async def test_end_preserves_requested_background_answer_but_never_speaks_it():
    hooks = Hooks()
    release, started = asyncio.Event(), asyncio.Event()

    async def answer(intent):
        started.set()
        await release.wait()
        return VoiceAnswer(run_id="background-run", spoken_text="Completed after End.", accepted=True)

    hooks.handler = answer
    ctl = controller(hooks=hooks)
    await ctl.start()
    await ctl.handle_event(transcript())
    await ctl.handle_event(delegation())
    await asyncio.wait_for(started.wait(), 1)
    await ctl.close(cancel_work=False)
    release.set()
    await asyncio.wait_for(ctl._worker, 1)
    assert not any(e["type"] == "session.commentary.append" for e in ctl.provider.connection.sent)


async def test_answer_timeout_returns_actionable_failure_and_releases_slot():
    hooks = Hooks()

    async def answer(intent):
        await asyncio.Event().wait()

    hooks.handler = answer
    ctl = controller(hooks=hooks, answer_timeout_seconds=0.01)
    await ctl.start()
    try:
        await ctl.handle_event(transcript())
        await ctl.handle_event(delegation())
        await eventually(lambda: any(e["type"] == "session.commentary.append"
                                    for e in ctl.provider.connection.sent))
        assert "timed out" in ctl.provider.connection.sent[-1]["content"]
        assert ctl._active_answer is None
    finally:
        await ctl.close()


def test_speech_chunks_preserve_words_and_obey_utf8_byte_bound():
    text = "A checked answer. " * 300
    chunks = speech_chunks(text)
    assert " ".join(chunks) == text.strip()
    assert all(len(chunk.encode()) <= 450 for chunk in chunks)
    assert all(len(chunk.encode()) <= 450 for chunk in speech_chunks("é" * 900))


@pytest.mark.parametrize('status,body,expected', [
    (200, {}, True), (204, None, True),
    (404, {'error': {'code': 'session_id_not_found'}}, True),
    (404, {'error': {'code': 'route_not_found'}}, False),
    (401, {'error': {'code': 'session_id_not_found'}}, False),
    (503, {}, False), (404, None, False),
])
async def test_hangup_reconciles_only_confirmed_session_absence(status, body, expected):
    async def handle(request):
        return httpx.Response(status, json=body) if body is not None else httpx.Response(status)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        assert await OpenAILive('test-only', client=client).hangup('live-known') is expected
