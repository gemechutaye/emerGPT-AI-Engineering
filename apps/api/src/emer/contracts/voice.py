"""Transport-independent Live contracts. Raw audio is never part of persisted state."""

from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field


class VoiceOffer(BaseModel):
    automatic_context: bool = False
    model_config = ConfigDict(extra="forbid")
    conversation_id: str
    context_version: int = Field(ge=1)
    sdp: str = Field(min_length=10, max_length=65536)
    idempotency_key: str = Field(min_length=8, max_length=100)


class VoiceCreated(BaseModel):
    id: str
    provider_session_id: str
    sdp: str
    status: str = "connecting"
    max_duration_seconds: int = 300


class TranscriptFragment(BaseModel):
    event_id: str
    speaker: Literal["user", "assistant"]
    delta: str
    start_ms: int = Field(ge=0)
    end_ms: int = Field(ge=0)


class VoiceContext(BaseModel):
    session_id: str
    provider_session_id: str
    conversation_id: str
    context_version: int
    index_id: str
    fence: int


class VoiceIntent(VoiceContext):
    revision: int
    delegation_id: str
    question: str
    transcript: list[TranscriptFragment]
    request_start_ms: int = Field(default=0, ge=0)


class VoiceAnswer(BaseModel):
    run_id: str | None
    spoken_text: str
    accepted: bool


class VoiceSnapshot(BaseModel):
    id: str
    status: Literal["connecting", "listening", "working", "closing", "closed", "failed"] = "connecting"
    controller_connected: bool = False
    playback_blocked: bool = True
    revision: int = 0
    last_run_id: str | None = None
    last_delegation_id: str | None = None
    usage_seconds: float | None = None
    final_usage_confirmed: bool = False
    error: str | None = None
    close_reason: str | None = None
    provider_close_reason: str | None = None


class VoiceHooks(Protocol):
    """Implement atomically with ownership, expiry and current fence checks.

    Event and delegation uniqueness must be durable. Returning False rejects
    an obsolete controller or duplicate. answer() must commit the mapped run
    before returning, and observe cancellation without publishing stale work.
    """

    async def save_snapshot(self, session_id: str, fence: int, snapshot: VoiceSnapshot) -> bool: ...

    async def claim_event(
        self, session_id: str, fence: int, event_id: str, kind: str, payload: dict
    ) -> bool: ...

    async def claim_delegation(self, intent: VoiceIntent) -> bool: ...

    async def renew(self, session_id: str, fence: int) -> bool: ...

    async def answer(self, intent: VoiceIntent) -> VoiceAnswer: ...
