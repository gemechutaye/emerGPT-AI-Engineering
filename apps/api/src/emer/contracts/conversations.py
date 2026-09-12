"""Conversation presentation is saved work, never source evidence."""

from datetime import datetime
from typing import Annotated, Literal

from emer.contracts.http import ConversationView, Input, RunView
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ConversationUpdate(Input):
    title: str | None = Field(default=None, min_length=1, max_length=120)
    pinned: bool | None = None

    @field_validator("title")
    @classmethod
    def clean_title(cls, value):
        if value is not None:
            value = " ".join(value.split())
            if not value:
                raise ValueError("A title cannot be blank")
        return value

    @model_validator(mode="after")
    def has_change(self):
        if self.title is None and self.pinned is None:
            raise ValueError("Supply a title or pin state")
        return self


class ConversationList(BaseModel):
    items: list[ConversationView]
    next_offset: int | None


class SavedTranscript(BaseModel):
    event_id: str
    live_session_id: str
    speaker: Literal["user", "assistant"]
    delta: str
    start_ms: int
    end_ms: int
    created_at: datetime


class TranscriptPage(BaseModel):
    items: list[SavedTranscript]
    next_cursor: int | None


class ConversationDetail(ConversationView):
    runs: list[RunView]
    next_cursor: str | None
    transcripts: list[SavedTranscript]
    transcripts_next_cursor: int | None


class MetadataDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=80)
    introduction: str = Field(min_length=1, max_length=240)
    points: list[Annotated[str, Field(min_length=1, max_length=220)]] = Field(min_length=2, max_length=4)

    @field_validator("title")
    @classmethod
    def topic_title(cls, value):
        value = " ".join(value.split()).strip()
        if not value or len(value.split()) > 8 or value.endswith("?"):
            raise ValueError("Use a topic title of at most eight words")
        return value

    @field_validator("introduction")
    @classmethod
    def nonempty_introduction(cls, value):
        value = " ".join(value.split())
        if not value:
            raise ValueError("A recap introduction cannot be blank")
        return value

    @field_validator("points")
    @classmethod
    def nonempty_points(cls, values):
        points = []
        for value in values:
            value = " ".join(value.split())
            if value.startswith(("- ", "• ")):
                value = value[2:].strip()
            if not value:
                raise ValueError("A recap point cannot be blank")
            points.append(value)
        return points

    @property
    def summary(self) -> str:
        return self.introduction + "\n" + "\n".join(f"- {point}" for point in self.points)


class SharedMessage(BaseModel):
    role: Literal["user", "assistant"]
    text: str
    source: Literal["saved_answer", "voice_transcript"]


class SharedSnapshot(BaseModel):
    title: str
    summary: str | None
    summary_label: str = "Conversation recap — not source evidence"
    created_at: datetime
    disclosure: str
    messages: list[SharedMessage]


class ShareCreated(BaseModel):
    url: str
    created_at: datetime
    disclosure: str


class MetadataAttempt(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    metadata_job_id: str | None
    operation: str
    model: str
    status: str
    usage: dict | None
    error_code: str | None
    created_at: datetime
    completed_at: datetime | None


class MetadataAttempts(BaseModel):
    items: list[MetadataAttempt]
    next_offset: int | None
