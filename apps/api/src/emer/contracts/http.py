from datetime import datetime
from typing import Any, Literal

from emer.contracts.answer import PublishedAnswer
from emer.domain.recap_policy import current_recap_hash
from pydantic import BaseModel, ConfigDict, Field, model_validator


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SessionView(BaseModel):
    id: str
    expires_at: datetime


class ConversationCreate(Input):
    title: str = Field(default="New conversation", max_length=120)


class ContextUpdate(Input):
    expected_version: int = Field(ge=1)
    patient_id: str | None = Field(default=None, max_length=30)
    as_of: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")


class ConversationView(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    title: str
    context_version: int
    patient_id: str | None
    as_of: str | None
    created_at: datetime
    updated_at: datetime
    pinned: bool
    title_origin: Literal["auto", "manual"]
    summary: str | None
    summary_status: Literal["idle", "pending", "generating", "ready", "failed"]
    summary_error: str | None

    @model_validator(mode="before")
    @classmethod
    def hide_retired_recap(cls, value):
        # Persist old presentation for audit, but never advertise it as current.
        # Public dictionary projections already passed this ORM boundary.
        if (hasattr(value, "summary_input_hash")
                and getattr(value, "summary_status", None) in {"ready", "failed"}
                and not current_recap_hash(value.summary_input_hash)):
            projected = {name: getattr(value, name) for name in cls.model_fields if hasattr(value, name)}
            return {**projected, "summary": None, "summary_status": "idle", "summary_error": None}
        return value

    @model_validator(mode="after")
    def current_recap_only(self):
        if self.summary_status != "ready":
            self.summary = None
        return self


class RunCreate(Input):
    automatic_context: bool = False
    question: str = Field(min_length=1, max_length=6000)
    idempotency_key: str = Field(min_length=8, max_length=100)
    context_version: int = Field(ge=1)


class RunView(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    conversation_id: str
    question: str
    context_version: int
    index_id: str
    status: str
    answer: PublishedAnswer | None
    error: dict[str, Any] | None
    metrics: dict[str, Any]
    created_at: datetime
    completed_at: datetime | None


class DraftCreate(Input):
    run_id: str


class DraftUpdate(Input):
    text: str = Field(min_length=1, max_length=18000)
    expected_version: int = Field(ge=1)


class DraftView(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    run_id: str
    index_id: str
    text: str
    version: int
    check: dict[str, Any] | None
    created_at: datetime
