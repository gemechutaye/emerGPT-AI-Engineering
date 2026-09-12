"""Owned, idempotent Live-session admission and lifecycle endpoints."""

import asyncio
import logging
from datetime import timedelta
from hashlib import sha256
from typing import Annotated

from emer.api.dependencies import current_session
from emer.api.errors import problem
from emer.contracts.voice import VoiceContext, VoiceCreated, VoiceOffer, VoiceSnapshot
from emer.providers.openai_live import LiveProviderError, OpenAILive
from emer.services.conversation_memory import ConversationVoiceHooks, schedule_auto
from emer.services.conversations import touch
from emer.services.intent_context import live_startup_history, recent_intent_context
from emer.services.voice import VoiceController
from emer.services.voice_storage import ACTIVE_VOICE, VOICE_OWNER
from emer.settings import settings
from emer.storage.bundles import load_bundle
from emer.storage.database import Session
from emer.storage.models import BrowserSession, Conversation, LiveSession, utcnow
from fastapi import APIRouter, Depends
from sqlalchemy import func, or_, select, text

router = APIRouter(prefix="/api/v1/voice", tags=["voice"])
controllers: dict[str, VoiceController] = {}
provider = OpenAILive(settings.openai_api_key)
Owner = Annotated[BrowserSession, Depends(current_session)]


def snapshot_of(row: LiveSession) -> VoiceSnapshot:
    snapshot = VoiceSnapshot.model_validate(row.snapshot or {"id": row.id})
    if row.status in {"uncertain", "interrupted", "creating"}:
        snapshot.status = "failed" if row.status != "creating" else "connecting"
        snapshot.controller_connected = False
        snapshot.playback_blocked = True
        snapshot.error = snapshot.error or ("live_creation_outcome_unknown" if row.status == "uncertain" else None)
    return snapshot


async def owned_live(live_id: str, owner_id: str):
    async with Session() as db:
        row = await db.get(LiveSession, live_id)
        if row is None or row.session_id != owner_id:
            raise problem(404, "NOT_FOUND", "Live conversation not found.")
        return row


@router.post("/sessions", response_model=VoiceCreated, status_code=201)
async def create_session(body: VoiceOffer, owner: Owner):
    if not body.sdp.startswith("v=0"):
        raise problem(422, "INVALID_SDP", "The browser could not prepare an audio connection.")
    excluded = {"idempotency_key"}
    if not body.automatic_context:
        excluded.add("automatic_context")  # Preserve hashes for requests created before this optional flag.
    fingerprint = sha256(body.model_dump_json(exclude=excluded).encode()).hexdigest()
    async with Session.begin() as db:
        await db.execute(text("SELECT pg_advisory_xact_lock(7346202)"))
        previous = await db.scalar(select(LiveSession).where(
            LiveSession.session_id == owner.id, LiveSession.idempotency_key == body.idempotency_key
        ))
        if previous:
            if previous.body_hash != fingerprint:
                raise problem(409, "IDEMPOTENCY_CONFLICT", "This connection key belongs to a different audio offer.")
            if (previous.sdp and previous.provider_session_id and previous.status in ACTIVE_VOICE
                and previous.snapshot.get("controller_connected")):
                return VoiceCreated(id=previous.id, provider_session_id=previous.provider_session_id,
                                    sdp=previous.sdp, status=snapshot_of(previous).status)
            raise problem(409, "LIVE_ATTEMPT_EXISTS", "This voice connection attempt has already been recorded. "
                          "Check its status before starting another session.")
        browser = await db.scalar(select(BrowserSession).where(BrowserSession.id == owner.id).with_for_update())
        if browser is None or browser.revoked or browser.expires_at <= utcnow():
            raise problem(401, "SESSION_EXPIRED", "Start a new browser session before connecting voice.")
        if not settings.openai_api_key:
            raise problem(503, "LIVE_NOT_CONFIGURED", "Live voice is unavailable while its provider is configured.")
        conversation = await db.scalar(select(Conversation).where(
            Conversation.id == body.conversation_id, Conversation.session_id == owner.id
        ).with_for_update())
        if conversation is None:
            raise problem(404, "NOT_FOUND", "Conversation not found.")
        if conversation.context_version != body.context_version:
            raise problem(409, "CONTEXT_CHANGED", "The conversation scope changed. Review it before starting voice.")
        blocked = await db.scalar(select(LiveSession.id).where(
            LiveSession.session_id == owner.id, LiveSession.status.in_([*ACTIVE_VOICE, "uncertain"])
        ))
        if blocked:
            raise problem(409, "LIVE_ACTIVE", "A voice session is active or its creation outcome needs reconciliation.")
        active_count = await db.scalar(select(func.count()).select_from(LiveSession).where(
            LiveSession.status.in_([*ACTIVE_VOICE, "uncertain"])
        ))
        if active_count >= 2:
            raise problem(429, "LIVE_BUSY", "The demo is already handling two voice sessions. Try again shortly.", True)
        recent = await db.scalar(select(func.count()).select_from(LiveSession).where(
            LiveSession.session_id == owner.id, LiveSession.created_at > utcnow() - timedelta(minutes=10)
        ))
        if recent >= 4:
            raise problem(429, "LIVE_RATE_LIMITED", "Please wait before starting another voice session.", True)
        bundle = await load_bundle()
        saved_context = await recent_intent_context(db, conversation.id, conversation.context_version)
        context = {
            **saved_context,
            "patient_id": saved_context["patient_id"] if body.automatic_context else (
                conversation.patient_id or saved_context["patient_id"]
            ),
            "as_of": None if body.automatic_context else conversation.as_of,
            "automatic_context": body.automatic_context,
        }
        row = LiveSession(
            session_id=owner.id, conversation_id=conversation.id, context_version=conversation.context_version,
            context=context, index_id=bundle.id,
            idempotency_key=body.idempotency_key, body_hash=fingerprint, owner_id=VOICE_OWNER,
            lease_expires_at=utcnow() + timedelta(seconds=35), status="creating",
        )
        db.add(row)
        touch(conversation)
        await db.flush()
        row.snapshot = VoiceSnapshot(id=row.id).model_dump()
    try:
        history = live_startup_history(row.context)
        result = await provider.create(body.sdp, history=history) if history else await provider.create(body.sdp)
        async with Session.begin() as db:
            stored = await db.get(LiveSession, row.id)
            stored.provider_session_id, stored.sdp, stored.status = result.session_id, result.sdp, "connecting"
            stored.lease_expires_at = utcnow() + timedelta(seconds=20)
        controller = VoiceController(
            VoiceContext(session_id=row.id, provider_session_id=result.session_id,
                         conversation_id=row.conversation_id, context_version=row.context_version,
                         index_id=row.index_id, fence=row.fence), provider, ConversationVoiceHooks(),
        )
        controllers[row.id] = controller
        await controller.start()
        return VoiceCreated(id=row.id, provider_session_id=result.session_id, sdp=result.sdp,
                            status=controller.snapshot.status)
    except LiveProviderError as exc:
        if "result" in locals():
            # The provider session exists: controller.start has already entered
            # its bounded cleanup path. Do not overwrite that pending/uncertain
            # state with an ordinary failed-create row that permits rebilling.
            failure = problem(503, "LIVE_CONTROL_FAILED", "Live could not establish its source-checking connection.")
            failure.detail["voice_session_id"] = row.id
            raise failure from None
        async with Session.begin() as db:
            stored = await db.get(LiveSession, row.id)
            stored.status = "uncertain" if exc.uncertain else "failed"
            stored.snapshot = VoiceSnapshot(id=row.id, status="failed", error=exc.code).model_dump()
            stored.lease_expires_at = None
        failure = problem(503, "LIVE_CREATION_FAILED", "Live could not connect. The attempt was recorded; "
                          "an uncertain connection is not retried automatically.")
        failure.detail["voice_session_id"] = row.id
        raise failure from None
    except Exception:  # noqa: BLE001 - fail closed if persistence or controller startup fails
        if "result" in locals() and "controller" not in locals():
            confirmed = await provider.hangup(result.session_id)
            async with Session.begin() as db:
                stored = await db.get(LiveSession, row.id)
                stored.provider_session_id = result.session_id
                stored.status = "failed" if confirmed else "uncertain"
                stored.snapshot = VoiceSnapshot(
                    id=row.id, status="failed",
                    error="live_control_connection_failed" if confirmed else "live_close_unconfirmed",
                ).model_dump()
                stored.lease_expires_at = None
        raise problem(503, "LIVE_CONTROL_FAILED", "Live could not establish its source-checking connection.") from None


@router.get("/sessions")
async def list_sessions(owner: Owner):
    async with Session() as db:
        rows = (await db.scalars(select(LiveSession).where(LiveSession.session_id == owner.id)
                                .order_by(LiveSession.created_at.desc()).limit(20))).all()
        return {"items": [snapshot_of(row) for row in rows]}


@router.get("/sessions/{live_id}", response_model=VoiceSnapshot)
async def get_session(live_id: str, owner: Owner):
    row = await owned_live(live_id, owner.id)
    snapshot = snapshot_of(row)
    if row.status in ACTIVE_VOICE and (row.lease_expires_at is None or row.lease_expires_at <= utcnow()):
        snapshot.controller_connected = False
        snapshot.playback_blocked = True
        snapshot.error = "live_controller_ownership_lost"
    return snapshot


@router.post("/sessions/{live_id}/close", response_model=VoiceSnapshot)
async def close_session(live_id: str, owner: Owner):
    row = await owned_live(live_id, owner.id)
    return await close_row(row, "user", cancel_work=False)


async def close_row(row: LiveSession, reason: str, *, cancel_work: bool) -> VoiceSnapshot:
    controller = controllers.get(row.id)
    if (controller and controller._shutdown_complete
            and controller.snapshot.error == "live_close_unconfirmed" and not controller.has_pending_work):
        controllers.pop(row.id, None)
        controller = None
    if controller:
        result = await controller.close(reason, cancel_work=cancel_work)
        if controller._shutdown_complete and not controller.has_pending_work:
            controllers.pop(row.id, None)
        return result
    snapshot = snapshot_of(row)
    if row.status == "closed":
        return snapshot
    snapshot.playback_blocked, snapshot.controller_connected = True, False
    snapshot.close_reason = reason
    if row.status == "uncertain" and not row.provider_session_id:
        # No known session ID: cannot prove termination or turn an uncertain create into success.
        return snapshot
    confirmed = not row.provider_session_id or await provider.hangup(row.provider_session_id)
    snapshot.status = "closed" if confirmed else "failed"
    if not confirmed:
        snapshot.error = "live_close_unconfirmed"
    elif snapshot.error == "live_close_unconfirmed":
        snapshot.error = None
    async with Session.begin() as db:
        current = await db.get(LiveSession, row.id)
        current.fence += 1
        current.status = "closed" if confirmed else "uncertain"
        current.snapshot, current.lease_expires_at = snapshot.model_dump(), None
    if confirmed:
        await schedule_auto(row.conversation_id, row.session_id, voice_ended=True)
    return snapshot


async def close_owner_voice(owner_id: str) -> None:
    unfinished = [live_id for live_id, controller in controllers.items() if controller.has_pending_work]
    async with Session() as db:
        rows = (await db.scalars(select(LiveSession).where(
            LiveSession.session_id == owner_id,
            or_(LiveSession.status.in_([*ACTIVE_VOICE, "uncertain"]), LiveSession.id.in_(unfinished)),
        ))).all()
    await asyncio.gather(*(close_row(row, "owner_reset", cancel_work=True) for row in rows))


async def close_conversation_voice(conversation_id: str) -> None:
    unfinished = [live_id for live_id, controller in controllers.items() if controller.has_pending_work]
    async with Session() as db:
        rows = (await db.scalars(select(LiveSession).where(
            LiveSession.conversation_id == conversation_id,
            or_(LiveSession.status.in_([*ACTIVE_VOICE, "uncertain"]), LiveSession.id.in_(unfinished)),
        ))).all()
    await asyncio.gather(*(close_row(row, "context_changed", cancel_work=True) for row in rows))


async def recover_voice() -> None:
    async with Session() as db:
        expired = (await db.scalars(select(LiveSession).where(
            LiveSession.status.in_(ACTIVE_VOICE), LiveSession.lease_expires_at < utcnow()
        ))).all()
    for row in expired:
        async with Session.begin() as db:
            current = await db.scalar(select(LiveSession).where(LiveSession.id == row.id).with_for_update())
            if current.status not in ACTIVE_VOICE or not current.lease_expires_at or current.lease_expires_at > utcnow():
                continue
            # Fence only an expired claim. Never interrupt another worker's renewed claim.
            current.fence += 1
            current.lease_expires_at = None
            current.status = "interrupted" if current.provider_session_id else "uncertain"
            current.snapshot = VoiceSnapshot(
                id=row.id, status="failed", error="live_controller_expired" if current.provider_session_id
                else "live_creation_outcome_unknown",
            ).model_dump()
        controller = controllers.pop(row.id, None)
        unconfirmed = False
        if controller:
            result = await controller.close("controller_expired", cancel_work=True)
            unconfirmed = result.error == "live_close_unconfirmed"
        elif row.provider_session_id:
            unconfirmed = not await provider.hangup(row.provider_session_id)
        if unconfirmed:
            async with Session.begin() as db:
                current = await db.get(LiveSession, row.id)
                if current.status == "interrupted" and current.fence == row.fence + 1:
                    current.status = "uncertain"
                    current.snapshot = VoiceSnapshot(
                        id=row.id, status="failed", error="live_close_unconfirmed"
                    ).model_dump()


async def recovery_loop() -> None:
    while True:
        try:
            await recover_voice()
        except Exception:  # noqa: BLE001 - DB failure is surfaced by readiness; loop must resume
            logging.getLogger(__name__).warning("live_recovery_unavailable")
        for live_id, controller in list(controllers.items()):
            if controller.snapshot.status in {"closed", "failed"} and not controller.has_pending_work:
                controllers.pop(live_id, None)
        await asyncio.sleep(5)


def install(app) -> None:
    app.include_router(router)
    app.state.close_owner_voice = close_owner_voice
    app.state.close_conversation_voice = close_conversation_voice


async def shutdown() -> None:
    await asyncio.gather(*(controller.close("server_shutdown", cancel_work=True)
                           for controller in list(controllers.values())), return_exceptions=True)
    # A process that is stopping cannot promise a future cleanup retry.
    await asyncio.gather(*(controller._finalize_close(confirmed=False)
                           for controller in list(controllers.values()) if not controller._shutdown_complete),
                         return_exceptions=True)
    controllers.clear()
    await provider.aclose()
