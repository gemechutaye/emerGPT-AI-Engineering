"""Owned history management and explicitly published, bounded read-only copies."""

import json
import secrets
from hashlib import sha256

from emer.api.errors import problem
from emer.contracts.conversations import (
    ConversationList,
    ConversationUpdate,
    SavedTranscript,
    ShareCreated,
    SharedMessage,
    SharedSnapshot,
    TranscriptPage,
)
from emer.contracts.http import ConversationView
from emer.domain.recap_policy import RECAP_HASH_PREFIX
from emer.settings import settings
from emer.storage.database import Session
from emer.storage.models import (
    BrowserSession,
    Conversation,
    ConversationShare,
    Delegation,
    Draft,
    LiveEvent,
    LiveSession,
    ProviderAttempt,
    Run,
    RunEvent,
    utcnow,
)
from sqlalchemy import Text, cast, delete, exists, func, literal, or_, select, update
from sqlalchemy.dialects.postgresql import JSONPATH, aggregate_order_by

TRANSCRIPT_KINDS = ("session.input_transcript.delta", "session.output_transcript.delta")
ACTIVE_VOICE = ("creating", "connecting", "listening", "working", "closing", "uncertain")
SHARE_DISCLOSURE = (
    "Anyone with this link can read this frozen conversation copy. "
    "It is an unlisted synthetic-data demo, not access-controlled or for clinical care. "
    "A localhost link works only on the same computer."
)
SNAPSHOT_MAX_BYTES = 1_000_000
SNAPSHOT_MAX_RUNS = 200
SNAPSHOT_MAX_FRAGMENTS = 5000


async def owned(db, conversation_id: str, owner_id: str, *, lock: bool = False) -> Conversation:
    if lock:
        owner = await db.scalar(select(BrowserSession).where(
            BrowserSession.id == owner_id
        ).with_for_update())
        if not owner or owner.revoked or owner.expires_at <= utcnow():
            raise problem(401, "SESSION_EXPIRED", "Your browser session has expired.")
    statement = select(Conversation).where(
        Conversation.id == conversation_id, Conversation.session_id == owner_id
    )
    row = await db.scalar(statement.with_for_update() if lock else statement)
    if row is None:
        raise problem(404, "NOT_FOUND", "Conversation not found.")
    return row


async def work_active(db, conversation_id: str) -> bool:
    if await db.scalar(select(Run.id).where(
        Run.conversation_id == conversation_id, Run.status.in_(("queued", "running"))
    ).limit(1)):
        return True
    if await db.scalar(select(LiveSession.id).where(
        LiveSession.conversation_id == conversation_id,
        or_(LiveSession.status.in_(ACTIVE_VOICE),
            LiveSession.lease_expires_at > utcnow(),
            (LiveSession.answer_pending.is_(True)) & (LiveSession.answer_pending_until > utcnow())),
    ).limit(1)):
        return True
    return bool(await db.scalar(select(Draft.id).join(Run, Run.id == Draft.run_id).where(
        Run.conversation_id == conversation_id, Draft.check_lease_expires_at > utcnow()
    ).limit(1)))


async def require_settled(db, conversation_id: str) -> None:
    if await work_active(db, conversation_id):
        raise problem(409, "WORK_ACTIVE", "End voice and wait for the current answer or check to finish.", True)


def touch(row: Conversation) -> None:
    row.updated_at = utcnow()
    row.activity_version += 1
    if row.summary_status == "ready":
        row.summary_status, row.summary_error = "idle", None


async def list_owned(owner_id: str, offset: int = 0, q: str | None = None) -> ConversationList:
    statement = select(Conversation).where(Conversation.session_id == owner_id)
    if settings.shared_workspace:
        # Starting voice can create a container before permission/input succeeds.
        # Keep it in Postgres, but history begins with an actual saved message.
        statement = statement.where(or_(
            exists().where(Run.conversation_id == Conversation.id),
            exists(select(LiveEvent.id).join(LiveSession, LiveSession.id == LiveEvent.live_session_id).where(
                LiveSession.conversation_id == Conversation.id,
                LiveEvent.kind == "session.input_transcript.delta",
                func.length(func.trim(LiveEvent.payload["delta"].as_string())) > 0,
            )),
        ))
    if q and q.strip():
        pattern = "%" + q.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        transcript_text = select(func.string_agg(
            LiveEvent.payload["delta"].as_string(), aggregate_order_by(literal(""), LiveEvent.id),
        )).join(LiveSession, LiveSession.id == LiveEvent.live_session_id).where(
            LiveSession.conversation_id == Conversation.id, LiveEvent.kind.in_(TRANSCRIPT_KINDS),
            func.jsonb_typeof(LiveEvent.payload["delta"]) == "string",
        ).correlate(Conversation).scalar_subquery()
        statement = statement.where(or_(
            Conversation.title.ilike(pattern, escape="\\"),
            (Conversation.summary_status == "ready")
            & Conversation.summary_input_hash.startswith(RECAP_HASH_PREFIX)
            & Conversation.summary.ilike(pattern, escape="\\"),
            exists().where(Run.conversation_id == Conversation.id, or_(
                Run.question.ilike(pattern, escape="\\"),
                *(func.jsonb_path_query_array(Run.answer, cast(f"$.{key}[*].text", JSONPATH))
                  .cast(Text).ilike(pattern, escape="\\") for key in ("statements", "gaps", "next_steps")),
            )),
            transcript_text.ilike(pattern, escape="\\"),
        ))
    offset = max(offset, 0)
    async with Session() as db:
        rows = list(await db.scalars(statement.order_by(
            Conversation.pinned.desc(), Conversation.updated_at.desc(), Conversation.id.desc()
        ).offset(offset).limit(51)))
    return ConversationList(
        items=[ConversationView.model_validate(row) for row in rows[:50]],
        next_offset=offset + 50 if len(rows) > 50 else None,
    )


async def update_owned(owner_id: str, conversation_id: str, change: ConversationUpdate) -> Conversation:
    async with Session.begin() as db:
        row = await owned(db, conversation_id, owner_id, lock=True)
        if change.title is not None:
            row.title = change.title
            row.title_origin = "manual"
            row.title_revision += 1
        if change.pinned is not None:
            row.pinned = change.pinned
        # Renaming must not cancel a valid recap; title_revision fences just the generated title.
        row.updated_at = utcnow()
    return row


def answer_text(answer: dict | None) -> str:
    if not answer:
        return ""
    sections = []
    for key in ("statements", "gaps", "next_steps"):
        entries = [item["text"] for item in answer.get(key, []) if isinstance(item.get("text"), str)]
        if entries:
            label = {"statements": "", "gaps": "Uncertainty / not established:\n",
                     "next_steps": "Saved source-linked next steps:\n"}[key]
            sections.append(label + "\n".join(entries))
    return "\n\n".join(sections)


def transcript_query(conversation_id: str):
    return select(LiveEvent, LiveSession.created_at).join(
        LiveSession, LiveSession.id == LiveEvent.live_session_id
    ).where(
        LiveSession.conversation_id == conversation_id, LiveEvent.kind.in_(TRANSCRIPT_KINDS),
        func.jsonb_typeof(LiveEvent.payload["delta"]) == "string",
    ).order_by(LiveEvent.id)


def saved_fragment(event: LiveEvent, created_at) -> SavedTranscript:
    payload = event.payload
    start = payload.get("start_ms", 0)
    end = payload.get("end_ms", 0)
    return SavedTranscript(
        event_id=str(event.id), live_session_id=event.live_session_id,
        speaker="user" if event.kind == TRANSCRIPT_KINDS[0] else "assistant",
        delta=payload.get("delta", ""),
        start_ms=max(0, start) if type(start) is int else 0,
        end_ms=max(0, end) if type(end) is int else 0,
        created_at=created_at,
    )


async def transcript_page(db, conversation_id: str, after: int | None = None) -> TranscriptPage:
    statement = transcript_query(conversation_id)
    if after is not None:
        cursor = await db.scalar(select(LiveEvent.id).join(
            LiveSession, LiveSession.id == LiveEvent.live_session_id
        ).where(LiveEvent.id == after, LiveSession.conversation_id == conversation_id,
                LiveEvent.kind.in_(TRANSCRIPT_KINDS)))
        if cursor is None:
            raise problem(404, "NOT_FOUND", "Transcript cursor not found.")
        statement = statement.where(LiveEvent.id > after)
    rows = (await db.execute(statement.limit(501))).all()
    return TranscriptPage(
        items=[saved_fragment(event, created) for event, created in rows[:500]],
        next_cursor=rows[499][0].id if len(rows) > 500 else None,
    )


async def conversation_content(db, conversation_id: str, *, max_bytes: int) -> dict:
    """Read all saved content within explicit limits; never slice an answer or transcript."""
    runs = list(await db.scalars(select(Run).where(
        Run.conversation_id == conversation_id
    ).order_by(Run.created_at, Run.id).limit(SNAPSHOT_MAX_RUNS + 1)))
    fragments = (await db.execute(transcript_query(conversation_id).limit(SNAPSHOT_MAX_FRAGMENTS + 1))).all()
    if len(runs) > SNAPSHOT_MAX_RUNS or len(fragments) > SNAPSHOT_MAX_FRAGMENTS:
        raise problem(422, "CONVERSATION_TOO_LARGE",
                      "This conversation exceeds the bounded snapshot limit (200 answers or 5,000 transcript fragments).")
    turns = []
    last_live = None
    for event, created in fragments:
        fragment = saved_fragment(event, created)
        if not fragment.delta:
            continue
        if turns and event.live_session_id == last_live and turns[-1]["speaker"] == fragment.speaker:
            turns[-1]["text"] += fragment.delta
            turns[-1]["end_ms"] = max(turns[-1]["end_ms"], fragment.end_ms)
        else:
            turns.append({"speaker": fragment.speaker, "text": fragment.delta,
                          "start_ms": fragment.start_ms, "end_ms": fragment.end_ms,
                          "session_started_at": created.isoformat()})
        last_live = event.live_session_id
    content = {
        "saved_answers": [
            {"question": run.question, "status": run.status, "answer": answer_text(run.answer),
             "created_at": run.created_at.isoformat()}
            for run in runs
        ],
        "voice_transcript": turns,
    }
    if len(json.dumps(content, ensure_ascii=False).encode()) > max_bytes:
        raise problem(422, "CONVERSATION_TOO_LARGE",
                      f"This conversation exceeds the {max_bytes:,}-byte content limit. Nothing was truncated.")
    return content


async def create_share(owner_id: str, conversation_id: str) -> ShareCreated:
    async with Session.begin() as db:
        row = await owned(db, conversation_id, owner_id, lock=True)
        await require_settled(db, conversation_id)
        content = await conversation_content(db, conversation_id, max_bytes=SNAPSHOT_MAX_BYTES)
        messages = []
        for run in content["saved_answers"]:
            messages.append(SharedMessage(role="user", text=run["question"], source="saved_answer"))
            if run["answer"]:
                messages.append(SharedMessage(role="assistant", text=run["answer"], source="saved_answer"))
        for fragment in content["voice_transcript"]:
            messages.append(SharedMessage(
                role=fragment["speaker"], text=fragment["text"], source="voice_transcript"
            ))
        if not messages:
            raise problem(409, "CONVERSATION_EMPTY", "Save an answer or finish a voice conversation before sharing.")
        snapshot = SharedSnapshot(
            title=row.title, summary=ConversationView.model_validate(row).summary,
            created_at=utcnow(), disclosure=SHARE_DISCLOSURE, messages=messages,
        )
        if len(snapshot.model_dump_json().encode()) > SNAPSHOT_MAX_BYTES:
            raise problem(422, "CONVERSATION_TOO_LARGE", "The complete share copy exceeds 1 MB. Nothing was truncated.")
        token = secrets.token_urlsafe(32)
        await db.execute(delete(ConversationShare).where(ConversationShare.conversation_id == conversation_id))
        db.add(ConversationShare(
            conversation_id=row.id, session_id=owner_id, token_hash=sha256(token.encode()).hexdigest(),
            snapshot=snapshot.model_dump(mode="json"), created_at=snapshot.created_at,
        ))
    return ShareCreated(url=f"/share/{token}", created_at=snapshot.created_at, disclosure=SHARE_DISCLOSURE)


async def revoke_share(owner_id: str, conversation_id: str) -> None:
    async with Session.begin() as db:
        await owned(db, conversation_id, owner_id, lock=True)
        await db.execute(delete(ConversationShare).where(ConversationShare.conversation_id == conversation_id))


async def read_share(token: str) -> SharedSnapshot:
    if not 40 <= len(token) <= 100:
        raise problem(404, "NOT_FOUND", "This shared conversation is unavailable or has been revoked.")
    async with Session() as db:
        row = await db.scalar(select(ConversationShare).join(
            BrowserSession, BrowserSession.id == ConversationShare.session_id
        ).where(
            ConversationShare.token_hash == sha256(token.encode()).hexdigest(),
            BrowserSession.revoked.is_(False), BrowserSession.expires_at > utcnow(),
        ))
        if row is None:
            raise problem(404, "NOT_FOUND", "This shared conversation is unavailable or has been revoked.")
        return SharedSnapshot.model_validate(row.snapshot)


async def delete_owned(owner_id: str, conversation_id: str) -> None:
    async with Session.begin() as db:
        await owned(db, conversation_id, owner_id, lock=True)
        await require_settled(db, conversation_id)
        run_ids = select(Run.id).where(Run.conversation_id == conversation_id)
        live_ids = select(LiveSession.id).where(LiveSession.conversation_id == conversation_id)
        draft_ids = select(Draft.id).where(Draft.run_id.in_(run_ids))
        # Preserve actual accounting independently of deletable content. This is
        # an archive receipt, not another provider call or a claim of zero cost.
        lives = list(await db.scalars(select(LiveSession).where(LiveSession.conversation_id == conversation_id)))
        for live in lives:
            if live.provider_session_id:
                db.add(ProviderAttempt(
                    session_id=owner_id, operation="live_usage_archive", model=settings.openai_live_model,
                    status="completed" if live.snapshot.get("final_usage_confirmed") else "uncertain",
                    usage={"request_id": live.provider_session_id,
                           "duration_seconds": live.snapshot.get("usage_seconds"),
                           "final_usage_confirmed": bool(live.snapshot.get("final_usage_confirmed")),
                           "cost": None},
                    created_at=live.created_at, completed_at=utcnow(),
                ))
        await db.execute(update(ProviderAttempt).where(or_(
            ProviderAttempt.run_id.in_(run_ids), ProviderAttempt.draft_id.in_(draft_ids),
            ProviderAttempt.live_session_id.in_(live_ids), ProviderAttempt.conversation_id == conversation_id,
        )).values(run_id=None, draft_id=None, live_session_id=None, conversation_id=None))
        for model, predicate in (
            (ConversationShare, ConversationShare.conversation_id == conversation_id),
            (LiveEvent, LiveEvent.live_session_id.in_(live_ids)),
            (Delegation, or_(Delegation.live_session_id.in_(live_ids), Delegation.run_id.in_(run_ids))),
            (LiveSession, LiveSession.id.in_(live_ids)),
            (Draft, Draft.id.in_(draft_ids)),
            (RunEvent, RunEvent.run_id.in_(run_ids)),
            (Run, Run.id.in_(run_ids)),
            (Conversation, Conversation.id == conversation_id),
        ):
            await db.execute(delete(model).where(predicate))
