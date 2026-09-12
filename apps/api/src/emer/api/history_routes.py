"""Owned database change notifications, including writes from other tabs/processes."""

import asyncio
from hashlib import sha256
from typing import Annotated

from emer.api.dependencies import current_session
from emer.storage.database import Session
from emer.storage.models import BrowserSession, Conversation, LiveEvent, LiveSession, Run, utcnow
from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import String, cast, func, literal, select
from sqlalchemy.dialects.postgresql import aggregate_order_by

router = APIRouter(prefix="/api/v1/history", tags=["conversations"])


async def revision(owner_id: str) -> str | None:
    async with Session() as db:
        owner = await db.get(BrowserSession, owner_id)
        if not owner or owner.revoked or owner.expires_at <= utcnow():
            return None
        # Database aggregates avoid transferring histories or transcript bodies.
        conversations = await db.scalar(select(func.md5(func.string_agg(
            func.concat_ws(":", Conversation.id, cast(Conversation.updated_at, String),
                           Conversation.title_revision, Conversation.activity_version,
                           Conversation.summary_fence, Conversation.summary_status,
                           cast(Conversation.pinned, String)), aggregate_order_by(literal(","), Conversation.id))
        )).where(Conversation.session_id == owner_id))
        runs = (await db.execute(select(func.count(), func.max(Run.created_at), func.max(Run.completed_at))
                                .where(Run.session_id == owner_id))).one()
        transcripts = (await db.execute(select(func.count(), func.max(LiveEvent.id)).join(
            LiveSession, LiveSession.id == LiveEvent.live_session_id
        ).where(LiveSession.session_id == owner_id,
                LiveEvent.kind.in_(("session.input_transcript.delta", "session.output_transcript.delta"))))).one()
    return sha256(repr((conversations, tuple(runs), tuple(transcripts))).encode()).hexdigest()


@router.get("/events")
async def history_events(request: Request, owner: Annotated[BrowserSession, Depends(current_session)]):
    async def stream():
        previous = None
        yield "retry: 1500\n\n"
        for tick in range(60):
            if await request.is_disconnected():
                return
            current = await revision(owner.id)
            if current is None:
                yield "event: expired\ndata: {}\n\n"
                return
            if current != previous:
                yield f"event: changed\ndata: {current}\n\n"
                previous = current
            elif tick % 15 == 0:
                yield ": keep-alive\n\n"
            await asyncio.sleep(1)
    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})
