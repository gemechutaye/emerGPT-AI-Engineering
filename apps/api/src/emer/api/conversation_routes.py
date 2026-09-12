"""Anonymous ownership is required for every action except reading an explicit share."""

from typing import Annotated

from emer.api.dependencies import current_session
from emer.contracts.conversations import (
    ConversationUpdate,
    MetadataAttempt,
    MetadataAttempts,
    ShareCreated,
    SharedSnapshot,
    TranscriptPage,
)
from emer.contracts.http import ConversationView
from emer.services import conversation_memory, conversations
from emer.storage.database import Session
from emer.storage.models import BrowserSession, ProviderAttempt
from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy import select

router = APIRouter(prefix="/api/v1", tags=["conversations"])
Owner = Annotated[BrowserSession, Depends(current_session)]


@router.patch("/conversations/{conversation_id}", response_model=ConversationView)
async def update_conversation(conversation_id: str, body: ConversationUpdate, owner: Owner):
    return await conversations.update_owned(owner.id, conversation_id, body)


@router.delete("/conversations/{conversation_id}", status_code=204)
async def delete_conversation(conversation_id: str, owner: Owner):
    await conversations.delete_owned(owner.id, conversation_id)
    return Response(status_code=204)


@router.post("/conversations/{conversation_id}/summary", response_model=ConversationView, status_code=202)
async def request_summary(conversation_id: str, owner: Owner):
    return await conversation_memory.request_summary(owner.id, conversation_id)


@router.get("/conversations/{conversation_id}/summary/attempts", response_model=MetadataAttempts)
async def summary_attempts(conversation_id: str, owner: Owner, offset: int = 0):
    async with Session() as db:
        await conversations.owned(db, conversation_id, owner.id)
        rows = list(await db.scalars(select(ProviderAttempt).where(
            ProviderAttempt.conversation_id == conversation_id
        ).order_by(ProviderAttempt.created_at.desc(), ProviderAttempt.id.desc()).offset(max(0, offset)).limit(51)))
    return MetadataAttempts(
        items=[MetadataAttempt.model_validate(row) for row in rows[:50]],
        next_offset=max(0, offset) + 50 if len(rows) > 50 else None,
    )


@router.get("/conversations/{conversation_id}/transcripts", response_model=TranscriptPage)
async def transcripts(conversation_id: str, owner: Owner, after: Annotated[int | None, Query(ge=1)] = None):
    async with Session() as db:
        await conversations.owned(db, conversation_id, owner.id)
        return await conversations.transcript_page(db, conversation_id, after)


@router.post("/conversations/{conversation_id}/share", response_model=ShareCreated, status_code=201)
async def share_conversation(conversation_id: str, owner: Owner):
    return await conversations.create_share(owner.id, conversation_id)


@router.delete("/conversations/{conversation_id}/share", status_code=204)
async def revoke_share(conversation_id: str, owner: Owner):
    await conversations.revoke_share(owner.id, conversation_id)
    return Response(status_code=204)


@router.get("/shared/{token}", response_model=SharedSnapshot)
async def read_share(token: str):
    return await conversations.read_share(token)
