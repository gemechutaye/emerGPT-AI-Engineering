"""Bounded deletion of expired anonymous work; immutable corpus indexes are never pruned."""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

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
from sqlalchemy import delete, exists, func, or_, select, text
from sqlalchemy.exc import SQLAlchemyError

RETENTION_GRACE = timedelta(hours=24)
RETENTION_INTERVAL_SECONDS = 3600
MAX_BATCH_SIZE = 100


async def prune_expired_sessions(*, apply: bool = False, batch_size: int = MAX_BATCH_SIZE) -> dict:
    """Inspect/delete at most one batch, atomically and without provider or filesystem I/O.

    Revocation does not move the original expiry. Existing work remains protected
    until its normal recovery/lease logic settles it. A pruned unknown usage receipt
    remains unknown; deletion neither reconciles billing nor proves a Live close.
    """
    if not 1 <= batch_size <= MAX_BATCH_SIZE:
        raise ValueError(f"batch_size must be between 1 and {MAX_BATCH_SIZE}")
    now = utcnow()
    cutoff = now - RETENTION_GRACE
    protected = or_(
        exists().where(
            Run.session_id == BrowserSession.id,
            or_(Run.status.in_(["queued", "running"]), Run.lease_expires_at > now),
        ),
        exists().where(
            Conversation.session_id == BrowserSession.id,
            Conversation.summary_lease_expires_at > now,
        ),
        exists().where(Draft.session_id == BrowserSession.id, Draft.check_lease_expires_at > now),
        exists().where(
            LiveSession.session_id == BrowserSession.id,
            or_(
                LiveSession.status.in_(["creating", "connecting", "listening", "working", "closing"]),
                LiveSession.lease_expires_at > now,
                LiveSession.answer_pending.is_(True) & (LiveSession.answer_pending_until > now),
            ),
        ),
    )
    async with asyncio.timeout(30), Session.begin() as db:
        await db.execute(text("SET LOCAL lock_timeout = '2s'"))
        await db.execute(text("SET LOCAL statement_timeout = '10s'"))
        protected_count = await db.scalar(select(func.count()).select_from(BrowserSession).where(
            BrowserSession.expires_at < cutoff, protected,
        ))
        # Admissions lock the browser owner too. Skip a concurrent owner operation;
        # re-evaluate eligibility on the next pass instead of blocking foreground work.
        ids = list(await db.scalars(select(BrowserSession.id).where(
            BrowserSession.expires_at < cutoff, ~protected,
        ).order_by(BrowserSession.expires_at, BrowserSession.id).limit(batch_size)
            .with_for_update(skip_locked=True)))
        runs = select(Run.id).where(Run.session_id.in_(ids))
        lives = select(LiveSession.id).where(LiveSession.session_id.in_(ids))
        unknown_provider_usage = await db.scalar(select(func.count()).select_from(ProviderAttempt).where(
            ProviderAttempt.session_id.in_(ids),
            or_(ProviderAttempt.usage["total_tokens"].as_integer().is_(None),
                ProviderAttempt.usage["cost"].as_float().is_(None)),
        ))
        unconfirmed_live_usage = await db.scalar(select(func.count()).select_from(LiveSession).where(
            LiveSession.session_id.in_(ids),
            func.coalesce(LiveSession.snapshot["final_usage_confirmed"].as_boolean(), False).is_(False),
        ))
        # Child tables precede every referenced parent; CorpusIndex and ActiveIndex
        # are deliberately absent so source history and reconstructable indexes survive.
        owned_rows = (
            (ProviderAttempt, ProviderAttempt.session_id.in_(ids)),
            (ConversationShare, ConversationShare.session_id.in_(ids)),
            (LiveEvent, LiveEvent.live_session_id.in_(lives)),
            (Delegation, Delegation.live_session_id.in_(lives)),
            (LiveSession, LiveSession.session_id.in_(ids)),
            (Draft, Draft.session_id.in_(ids)),
            (RunEvent, RunEvent.run_id.in_(runs)),
            (Run, Run.session_id.in_(ids)),
            (Conversation, Conversation.session_id.in_(ids)),
            (BrowserSession, BrowserSession.id.in_(ids)),
        )
        counts = {}
        for model, predicate in owned_rows:
            if apply:
                result = await db.execute(delete(model).where(predicate))
                counts[model.__tablename__] = result.rowcount
            else:
                counts[model.__tablename__] = await db.scalar(
                    select(func.count()).select_from(model).where(predicate)
                )
    return {
        "mode": "applied" if apply else "dry_run",
        "cutoff_exclusive": cutoff.isoformat(),
        "batch_limit": batch_size,
        "selected_sessions": len(ids),
        "deleted_sessions": len(ids) if apply else 0,
        "protected_sessions": protected_count,
        "rows": counts,
        "unknown_provider_usage_receipts": unknown_provider_usage,
        "unconfirmed_live_usage_sessions": unconfirmed_live_usage,
    }


async def retention_loop() -> None:
    """One bounded batch per hour while running; cancellation rolls back an in-flight batch."""
    logger = logging.getLogger("emer.retention")
    while True:
        # No startup purge: recovery gets time to settle abandoned work first.
        await asyncio.sleep(RETENTION_INTERVAL_SECONDS)
        try:
            result = await prune_expired_sessions(apply=True)
        except (SQLAlchemyError, TimeoutError):
            # Database URLs, content and exception strings may contain private data.
            logger.warning("retention_pass_unavailable")
        else:
            if result["deleted_sessions"]:
                logger.info(
                    "retention_pruned sessions=%s unknown_usage_receipts=%s unconfirmed_live_usage=%s",
                    result["deleted_sessions"], result["unknown_provider_usage_receipts"],
                    result["unconfirmed_live_usage_sessions"],
                )
