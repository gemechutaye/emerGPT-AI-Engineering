from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import DateTime, ForeignKey, Index, Integer, LargeBinary, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def new_id() -> str:
    return str(uuid4())


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class BrowserSession(Base):
    __tablename__ = "browser_sessions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked: Mapped[bool] = mapped_column(default=False)


class CorpusIndex(Base):
    __tablename__ = "corpus_indexes"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    checksum: Mapped[str] = mapped_column(String(64), unique=True)
    corpus_checksum: Mapped[str] = mapped_column(String(64))
    bundle: Mapped[bytes] = mapped_column(LargeBinary)
    manifest: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ActiveIndex(Base):
    __tablename__ = "active_index"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    index_id: Mapped[str] = mapped_column(ForeignKey("corpus_indexes.id"))


class Conversation(Base):
    __tablename__ = "conversations"
    __table_args__ = (Index("ix_conversations_owned_activity", "session_id", "pinned", "updated_at", "id"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    session_id: Mapped[str] = mapped_column(ForeignKey("browser_sessions.id"), index=True)
    title: Mapped[str] = mapped_column(String(120), default="New conversation")
    title_origin: Mapped[str] = mapped_column(String(10), default="auto", server_default="auto")
    title_revision: Mapped[int] = mapped_column(default=0, server_default="0")
    title_generated: Mapped[bool] = mapped_column(default=False, server_default=text("false"))
    pinned: Mapped[bool] = mapped_column(default=False, server_default=text("false"))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, server_default=text("CURRENT_TIMESTAMP")
    )
    activity_version: Mapped[int] = mapped_column(default=1, server_default="1")
    summary: Mapped[str | None] = mapped_column(Text)
    summary_status: Mapped[str] = mapped_column(String(12), default="idle", server_default="idle")
    summary_error: Mapped[str | None] = mapped_column(String(240))
    summary_input_hash: Mapped[str | None] = mapped_column(String(64))
    summary_job_id: Mapped[str | None] = mapped_column(String(36))
    summary_fence: Mapped[int] = mapped_column(default=0, server_default="0")
    summary_attempts: Mapped[int] = mapped_column(default=0, server_default="0")
    summary_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    summary_owner: Mapped[str | None] = mapped_column(String(36))
    summary_lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    context_version: Mapped[int] = mapped_column(default=1)
    patient_id: Mapped[str | None] = mapped_column(String(30))
    as_of: Mapped[str | None] = mapped_column(String(10))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Run(Base):
    __tablename__ = "runs"
    __table_args__ = (
        UniqueConstraint("conversation_id", "idempotency_key"),
        Index(
            "one_active_run_per_conversation",
            "conversation_id",
            unique=True,
            postgresql_where=text("status IN ('queued','running')"),
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    session_id: Mapped[str] = mapped_column(ForeignKey("browser_sessions.id"), index=True)
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.id"), index=True)
    index_id: Mapped[str] = mapped_column(ForeignKey("corpus_indexes.id"))
    idempotency_key: Mapped[str] = mapped_column(String(100))
    body_hash: Mapped[str] = mapped_column(String(64))
    question: Mapped[str] = mapped_column(Text)
    context_version: Mapped[int] = mapped_column(Integer)
    context: Mapped[dict] = mapped_column(JSONB, default=dict)
    status: Mapped[str] = mapped_column(String(24), default="queued")
    answer: Mapped[dict | None] = mapped_column(JSONB)
    evidence: Mapped[dict | None] = mapped_column(JSONB)
    error: Mapped[dict | None] = mapped_column(JSONB)
    metrics: Mapped[dict] = mapped_column(JSONB, default=dict)
    cancel_requested: Mapped[bool] = mapped_column(default=False)
    owner_id: Mapped[str | None] = mapped_column(String(36))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fence: Mapped[int] = mapped_column(default=0)
    attempt_started: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RunEvent(Base):
    __tablename__ = "run_events"
    __table_args__ = (UniqueConstraint("run_id", "sequence"),)
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    sequence: Mapped[int] = mapped_column(Integer)
    type: Mapped[str] = mapped_column(String(40))
    data: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Draft(Base):
    __tablename__ = "drafts"
    __table_args__ = (UniqueConstraint("session_id", "run_id"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    session_id: Mapped[str] = mapped_column(ForeignKey("browser_sessions.id"), index=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"))
    index_id: Mapped[str] = mapped_column(ForeignKey("corpus_indexes.id"))
    text: Mapped[str] = mapped_column(Text)
    version: Mapped[int] = mapped_column(default=1)
    check: Mapped[dict | None] = mapped_column(JSONB)
    checking_version: Mapped[int | None] = mapped_column(Integer)
    check_owner: Mapped[str | None] = mapped_column(String(36))
    check_lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class LiveSession(Base):
    __tablename__ = "live_sessions"
    __table_args__ = (UniqueConstraint("session_id", "idempotency_key"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    session_id: Mapped[str] = mapped_column(ForeignKey("browser_sessions.id"), index=True)
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.id"))
    index_id: Mapped[str] = mapped_column(ForeignKey("corpus_indexes.id"))
    context_version: Mapped[int] = mapped_column(Integer)
    context: Mapped[dict] = mapped_column(JSONB)
    idempotency_key: Mapped[str] = mapped_column(String(100))
    body_hash: Mapped[str] = mapped_column(String(64))
    provider_session_id: Mapped[str | None] = mapped_column(String(150), unique=True)
    snapshot: Mapped[dict] = mapped_column(JSONB, default=dict)
    answer_pending: Mapped[bool] = mapped_column(default=False, server_default=text("false"))
    answer_pending_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sdp: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(30), default="creating")
    owner_id: Mapped[str | None] = mapped_column(String(36))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fence: Mapped[int] = mapped_column(default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class LiveEvent(Base):
    __tablename__ = "live_events"
    __table_args__ = (UniqueConstraint("live_session_id", "provider_event_id"),)
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    live_session_id: Mapped[str] = mapped_column(ForeignKey("live_sessions.id"))
    provider_event_id: Mapped[str] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(String(80))
    payload: Mapped[dict] = mapped_column(JSONB, default=dict)


class Delegation(Base):
    __tablename__ = "live_delegations"
    __table_args__ = (UniqueConstraint("live_session_id", "delegation_id"),)
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    live_session_id: Mapped[str] = mapped_column(ForeignKey("live_sessions.id"))
    delegation_id: Mapped[str] = mapped_column(String(200))
    run_id: Mapped[str | None] = mapped_column(ForeignKey("runs.id"))
    intent: Mapped[dict] = mapped_column(JSONB, default=dict)
    revision: Mapped[int] = mapped_column(default=0)
    run_history: Mapped[list] = mapped_column(JSONB, default=list)


class ProviderAttempt(Base):
    __tablename__ = "provider_attempts"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    session_id: Mapped[str] = mapped_column(ForeignKey("browser_sessions.id"), index=True)
    run_id: Mapped[str | None] = mapped_column(ForeignKey("runs.id"), index=True)
    draft_id: Mapped[str | None] = mapped_column(ForeignKey("drafts.id"))
    live_session_id: Mapped[str | None] = mapped_column(ForeignKey("live_sessions.id"))
    conversation_id: Mapped[str | None] = mapped_column(
        ForeignKey("conversations.id", ondelete="SET NULL"), index=True
    )
    metadata_job_id: Mapped[str | None] = mapped_column(String(36), index=True)
    operation: Mapped[str] = mapped_column(String(60))
    model: Mapped[str] = mapped_column(String(150))
    status: Mapped[str] = mapped_column(String(30))
    usage: Mapped[dict | None] = mapped_column(JSONB)
    error_code: Mapped[str | None] = mapped_column(String(80))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ConversationShare(Base):
    __tablename__ = "conversation_shares"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.id"), unique=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("browser_sessions.id"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    snapshot: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
