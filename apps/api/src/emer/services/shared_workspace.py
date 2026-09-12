"""One durable workspace for this explicitly shared, synthetic-data demo."""

from datetime import UTC, datetime
from hashlib import sha256

from emer.storage.database import Session
from emer.storage.models import (
    BrowserSession,
    Conversation,
    ConversationShare,
    Draft,
    LiveSession,
    ProviderAttempt,
    Run,
    utcnow,
)
from sqlalchemy import func, select, text, update

WORKSPACE_ID = "fa56cf6b-d9bd-48c2-9842-17f65ccdfca8"
# This is a database owner, not a visitor credential or a browser session.
WORKSPACE_EXPIRY = datetime(9999, 12, 31, tzinfo=UTC)


async def initialize() -> dict[str, int]:
    """Idempotent startup migration; preserve messages, IDs, citations and receipts."""
    async with Session.begin() as db:
        await db.execute(text("SELECT pg_advisory_xact_lock(7346209)"))
        owner = await db.get(BrowserSession, WORKSPACE_ID, with_for_update=True)
        if owner is None:
            owner = BrowserSession(id=WORKSPACE_ID, token_hash=sha256(b"emer-shared-workspace").hexdigest(),
                                   expires_at=WORKSPACE_EXPIRY)
            db.add(owner)
            await db.flush()
        owner.revoked, owner.expires_at = False, WORKSPACE_EXPIRY
        # Do not change ownership while another process is doing paid work.
        for model, lease in ((Run, Run.lease_expires_at), (LiveSession, LiveSession.lease_expires_at),
                             (Conversation, Conversation.summary_lease_expires_at),
                             (Draft, Draft.check_lease_expires_at)):
            if await db.scalar(select(model.id).where(model.session_id != WORKSPACE_ID,
                                                       lease > utcnow()).limit(1)):
                raise RuntimeError("Wait for active work to settle before migrating shared history.")
        # Old browser scopes could independently use the same Live idempotency
        # key. Preserve those closed sessions under unique legacy keys.
        duplicates = list(await db.scalars(select(LiveSession.idempotency_key)
            .group_by(LiveSession.idempotency_key).having(func.count() > 1)))
        if duplicates:
            for live in list(await db.scalars(select(LiveSession).where(
                LiveSession.idempotency_key.in_(duplicates), LiveSession.session_id != WORKSPACE_ID,
            ))):
                live.idempotency_key = "legacy:" + live.id
            await db.flush()
        counts = {}
        for model in (Conversation, Run, Draft, LiveSession, ProviderAttempt, ConversationShare):
            result = await db.execute(update(model).where(model.session_id != WORKSPACE_ID)
                                      .values(session_id=WORKSPACE_ID))
            counts[model.__tablename__] = result.rowcount
    return counts


async def current() -> BrowserSession:
    async with Session() as db:
        row = await db.get(BrowserSession, WORKSPACE_ID)
    if row is None:
        # Supports first bootstrap in ASGI hosts without lifespan initialization.
        await initialize()
        async with Session() as db:
            row = await db.get(BrowserSession, WORKSPACE_ID)
    return row
