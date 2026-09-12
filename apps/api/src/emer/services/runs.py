import asyncio
import logging
import time
from datetime import timedelta
from hashlib import sha256
from uuid import uuid4

from emer.api.errors import problem
from emer.contracts.http import RunCreate
from emer.settings import settings
from emer.storage.bundles import load_bundle
from emer.storage.database import Session
from emer.storage.models import BrowserSession, Conversation, Draft, ProviderAttempt, Run, RunEvent, utcnow
from sqlalchemy import func, select, text
from sqlalchemy.exc import SQLAlchemyError

OWNER = str(uuid4())
TERMINAL = {"completed", "failed", "cancelled", "interrupted", "superseded"}
_tasks: dict[str, asyncio.Task] = {}


async def add_event(db, run: Run, kind: str, data: dict | None = None):
    sequence = (
        await db.scalar(select(func.max(RunEvent.sequence)).where(RunEvent.run_id == run.id)) or 0
    ) + 1
    db.add(RunEvent(run_id=run.id, sequence=sequence, type=kind, data=data or {}))


async def create_run(
    session_id: str,
    conversation_id: str,
    body: RunCreate,
    index_id: str | None = None,
    *,
    intent_resolved: bool = False,
) -> Run:
    excluded = {"idempotency_key"}
    if not body.automatic_context:
        excluded.add("automatic_context")  # Preserve hashes for requests created before this optional flag.
    fingerprint = sha256(body.model_dump_json(exclude=excluded).encode()).hexdigest()
    async with Session.begin() as db:
        # Global admission lock is transaction-scoped and only held for small DB operations.
        await db.execute(text("SELECT pg_advisory_xact_lock(7346201)"))
        browser = await db.scalar(
            select(BrowserSession).where(BrowserSession.id == session_id).with_for_update()
        )
        if not browser or browser.revoked or browser.expires_at <= utcnow():
            raise problem(
                401, "SESSION_EXPIRED", "Your browser session has expired. Start a new session to continue."
            )
        conversation = await db.scalar(
            select(Conversation)
            .where(Conversation.id == conversation_id, Conversation.session_id == session_id)
            .with_for_update()
        )
        if not conversation:
            raise problem(404, "NOT_FOUND", "Conversation not found.")
        old = await db.scalar(
            select(Run).where(
                Run.conversation_id == conversation_id, Run.idempotency_key == body.idempotency_key
            )
        )
        if old:
            if old.body_hash != fingerprint:
                raise problem(
                    409, "IDEMPOTENCY_CONFLICT", "This submission key belongs to a different question."
                )
            return old
        if body.context_version != conversation.context_version:
            raise problem(
                409, "CONTEXT_CHANGED", "The conversation scope changed. Review it and submit again."
            )
        active = await db.scalar(
            select(func.count()).select_from(Run).where(Run.status.in_(["queued", "running"]))
        )
        active += await db.scalar(
            select(func.count()).select_from(Draft).where(Draft.check_lease_expires_at > utcnow())
        )
        if active >= settings.global_concurrency:
            raise problem(429, "BUSY", "The demo is handling other questions. Try again shortly.", True)
        if await db.scalar(
            select(Run.id).where(
                Run.conversation_id == conversation_id, Run.status.in_(["queued", "running"])
            )
        ):
            raise problem(409, "RUN_ACTIVE", "This conversation already has an active answer.")
        if await db.scalar(
            select(Draft.id)
            .join(Run, Draft.run_id == Run.id)
            .where(Run.conversation_id == conversation_id, Draft.check_lease_expires_at > utcnow())
        ):
            raise problem(409, "WORK_ACTIVE", "Wait for the current draft recheck to finish.")
        recent = await db.scalar(
            select(func.count())
            .select_from(Run)
            .where(Run.session_id == session_id, Run.created_at > utcnow() - timedelta(minutes=1))
        )
        if recent >= 8:
            raise problem(429, "RATE_LIMITED", "Please wait a moment before asking another question.", True)
        bundle = await load_bundle(index_id)
        previous = (
            await db.scalars(
                select(Run)
                .where(
                    Run.conversation_id == conversation_id,
                    Run.context_version == body.context_version,
                    Run.status == "completed",
                )
                .order_by(Run.created_at.desc())
                .limit(6)
            )
        ).all()
        prior_questions = (
            [
                r.context.get("resolved_question", r.question)
                for r in reversed(previous)
                # A turn that only asked for clarification carries no resolved task to inherit.
                if (r.answer or {}).get("status") != "clarification" or r.evidence
            ]
            if not intent_resolved
            else []
        )
        effective_patient = None if body.automatic_context else conversation.patient_id
        from emer.domain.dialogue_state import build_dialogue_state

        state = build_dialogue_state(
            list(reversed(previous)) if not intent_resolved else [],
            conversation_id=conversation_id, context_version=body.context_version,
        )
        run = Run(
            session_id=session_id,
            conversation_id=conversation_id,
            index_id=bundle.id,
            idempotency_key=body.idempotency_key,
            body_hash=fingerprint,
            question=body.question.strip(),
            context_version=body.context_version,
            context={
                "patient_id": effective_patient,
                "as_of": None if body.automatic_context else conversation.as_of,
                "previous_questions": prior_questions,
                "dialogue_state": state.model_dump(mode="json"),
            },
        )
        if not run.question:
            raise problem(422, "EMPTY_QUESTION", "Enter a question first.")
        db.add(run)
        from emer.services.conversations import touch

        touch(conversation)
        from emer.services.conversation_titles import queue_title

        await queue_title(db, conversation)
        await db.flush()
        await add_event(db, run, "queued")
    dispatch(run.id)
    return run


def dispatch(run_id: str):
    if run_id not in _tasks:
        task = asyncio.create_task(execute(run_id))
        _tasks[run_id] = task
        task.add_done_callback(lambda _: _tasks.pop(run_id, None))


def effective_retrieval_mode(bundle) -> tuple[str, str | None]:
    """Fail explicitly when the configured retrieval space is unavailable."""
    from emer.services.ingestion import IngestionError

    requested = settings.retrieval_mode
    if requested not in {"lexical", "semantic", "hybrid"}:
        raise IngestionError("Full-context retrieval is not a production mode.")
    if not bundle.chunks:
        raise IngestionError("The active index has no source chunks; rebuild and activate it.")
    if requested in {"semantic", "hybrid"} and (bundle.config.get("embedding") or {}).get("unit") != "chunk":
        raise IngestionError("The configured chunk embedding index is not initialized.")
    return requested, None


async def retrieval_options(bundle, provider, question: str, usage_sink: list[dict]) -> dict:
    """Ranked modes embed the resolved question once; the receipt joins the run's usage."""
    mode, note = effective_retrieval_mode(bundle)
    options: dict = {
        "mode": mode,
        "top_k": settings.retrieval_top_k,
        "requested_mode": settings.retrieval_mode,
    }
    if mode in {"semantic", "hybrid"}:
        embedding = bundle.config["embedding"]
        result = await provider.embed([question], embedding["model"])
        usage_sink.append(result.usage.model_dump())
        options.update(query_vector=result.vectors[0], query_model=result.model)
    if note:
        logging.getLogger("emer.retrieval").warning("retrieval_mode_downgraded", extra={"note": note})
    return options


async def finish(
    run_id: str, fence: int, status: str, *, answer=None, evidence=None, error=None, metrics=None
):
    async with Session.begin() as db:
        run = await db.scalar(select(Run).where(Run.id == run_id).with_for_update())
        if (
            not run
            or run.fence != fence
            or run.owner_id != OWNER
            or run.status in TERMINAL
            or not run.lease_expires_at
            or run.lease_expires_at <= utcnow()
        ):
            return
        conversation = await db.get(Conversation, run.conversation_id)
        browser = await db.get(BrowserSession, run.session_id)
        if run.cancel_requested or browser.revoked or browser.expires_at <= utcnow():
            status, answer = "cancelled", None
        elif conversation.context_version != run.context_version:
            status, answer = "superseded", None
        run.status, run.answer, run.evidence, run.error = status, answer, evidence, error
        run.metrics = metrics or run.metrics
        attempts = (
            await db.scalars(
                select(ProviderAttempt)
                .where(ProviderAttempt.run_id == run.id)
                .order_by(ProviderAttempt.created_at, ProviderAttempt.id)
            )
        ).all()
        if attempts:
            # The durable dispatch ledger includes intent, embeddings, failed calls and
            # generation; summing only the answer service misses genuine billed work.
            run.metrics = {
                **run.metrics,
                "usage": [attempt.usage for attempt in attempts if attempt.usage is not None],
                "usage_source": "provider_attempt_ledger",
                "provider_attempt_count": len(attempts),
                "unreceipted_provider_attempts": sum(
                    attempt.usage is None and attempt.status in {"dispatched", "uncertain"}
                    for attempt in attempts
                ),
            }
        run.completed_at, run.lease_expires_at = utcnow(), None
        conversation.updated_at = run.completed_at
        await add_event(db, run, status, {"error": error} if error else {})
    from emer.services.conversation_memory import schedule_auto

    await schedule_auto(run.conversation_id, run.session_id)


async def monitor(run_id: str, fence: int, parent: asyncio.Task):
    while True:
        await asyncio.sleep(2)
        async with Session.begin() as db:
            run = await db.scalar(select(Run).where(Run.id == run_id).with_for_update())
            if (
                not run
                or run.owner_id != OWNER
                or run.fence != fence
                or run.status != "running"
                or not run.lease_expires_at
                or run.lease_expires_at <= utcnow()
            ):
                parent.cancel()
                return
            browser = await db.get(BrowserSession, run.session_id)
            if run.cancel_requested or browser.revoked or browser.expires_at <= utcnow():
                run.cancel_requested = True
                parent.cancel()
                return
            run.lease_expires_at = utcnow() + timedelta(seconds=20)


async def execute(run_id: str):
    start = time.monotonic()
    async with Session.begin() as db:
        run = await db.scalar(select(Run).where(Run.id == run_id).with_for_update())
        if not run or run.status != "queued":
            return
        run.owner_id, run.fence = OWNER, run.fence + 1
        run.status = "running"
        run.lease_expires_at = utcnow() + timedelta(seconds=20)
        fence = run.fence
        await add_event(db, run, "retrieving")
    watchdog = asyncio.create_task(monitor(run_id, fence, asyncio.current_task()))
    packet = None
    extra_usage: list[dict] = []
    try:
        from emer.contracts.answer import AnswerGap, PublishedAnswer
        from emer.services.intelligence import IntelligenceService
        from emer.services.provider_activity import TrackedOpenRouterClient
        from emer.services.text_intent import resolve_text_intent

        provider = TrackedOpenRouterClient(
            settings.openrouter_api_key,
            settings.openrouter_model,
            run.session_id,
            run_id=run.id,
            run_fence=fence,
            run_owner=OWNER,
        )
        bundle = await load_bundle(run.index_id)
        intent = await resolve_text_intent(
            provider,
            run.question,
            run.context.get("previous_questions", []),
            run.context.get("patient_id"),
            run.context.get("as_of"),
            dialogue_state=run.context.get("dialogue_state"),
        )
        if intent.clarification:
            answer = PublishedAnswer(
                status="clarification",
                statements=[],
                next_steps=[],
                gaps=[AnswerGap(part_id="intent", text=intent.clarification)],
                scopes=[],
                index_id=bundle.id,
                index_checksum=bundle.checksum,
                usage=[],
                diagnostics={"intent": "clarification_required"},
            )
            await finish(
                run_id,
                fence,
                "completed",
                answer=answer.model_dump(mode="json"),
                metrics={
                    "total_ms": round((time.monotonic() - start) * 1000),
                    "source_count": 0,
                    "model": settings.openrouter_model,
                    "usage": [],
                },
            )
            return
        question = intent.question
        async with Session.begin() as db:
            current = await db.scalar(select(Run).where(Run.id == run_id).with_for_update())
            if current.fence != fence or current.cancel_requested:
                raise asyncio.CancelledError()
            current.context = {**current.context, "resolved_question": question,
                               "reference_resolution": intent.reference_resolution.model_dump(mode="json")
                               if intent.reference_resolution else None}
        verifier = TrackedOpenRouterClient(
            settings.openrouter_api_key, settings.verifier_model, run.session_id,
            run_id=run.id, run_fence=fence, run_owner=OWNER,
            reasoning_effort=settings.verifier_reasoning_effort,
        )
        intelligence = IntelligenceService(provider, verifier, settings)

        async def on_evidence(selected_packet):
            nonlocal packet
            packet = selected_packet
            async with Session.begin() as db:
                current = await db.scalar(select(Run).where(Run.id == run_id).with_for_update())
                browser = await db.get(BrowserSession, current.session_id)
                conversation = await db.get(Conversation, current.conversation_id)
                if (
                    current.fence != fence
                    or current.cancel_requested
                    or current.owner_id != OWNER
                    or current.status != "running"
                    or not current.lease_expires_at
                    or current.lease_expires_at <= utcnow()
                    or not browser
                    or browser.revoked
                    or browser.expires_at <= utcnow()
                    or not conversation
                    or conversation.context_version != current.context_version
                ):
                    raise asyncio.CancelledError()
                current.attempt_started = True
                await add_event(
                    db, current, "generating", {"source_count": len(packet.sources), "index_id": bundle.id}
                )
        async with asyncio.timeout(settings.run_timeout_seconds):
            try:
                answer, packet = await intelligence.answer(
                    bundle, question, patient_id=run.context.get("patient_id"),
                    as_of=run.context.get("as_of"), on_evidence=on_evidence,
                )
            finally:
                packet = intelligence.packet
        await finish(
            run_id,
            fence,
            "completed",
            answer=answer.model_dump(mode="json"),
            evidence=packet.model_dump(mode="json"),
            metrics={
                "total_ms": round((time.monotonic() - start) * 1000),
                "source_count": len(packet.sources),
                "model": settings.openrouter_model,
                "retrieval_mode": packet.retrieval["mode"],
                "usage": [*extra_usage, *(u.model_dump() for u in answer.usage)],
            },
        )
    except asyncio.CancelledError:
        await finish(run_id, fence, "cancelled")
    except Exception as exc:  # noqa: BLE001 - persist sanitized terminal failure at the worker boundary
        code = getattr(exc, "code", "ANSWER_FAILED")
        message = "The answer service could not complete this request. Your question is preserved."
        if not settings.openrouter_api_key:
            code, message = (
                "PROVIDER_NOT_CONFIGURED",
                "Answer generation is unavailable while the demo provider is being configured.",
            )
        await finish(
            run_id,
            fence,
            "failed",
            error={"code": code, "message": message, "retryable": getattr(exc, "retryable", False)},
            evidence=packet.model_dump(mode="json") if packet else None,
            metrics={
                "total_ms": round((time.monotonic() - start) * 1000),
                "error_type": type(exc).__name__,
                # Validator/checker reasons quote only corpus text and item paths; they let the
                # developer view explain a closed failure instead of hiding it.
                "failure_detail": [str(item)[:400] for item in getattr(exc, "problems", [])[:12]],
                "usage": [
                    *extra_usage,
                    *(
                        u if isinstance(u, dict) else u.model_dump()
                        for u in getattr(exc, "attempt_usage", [])
                    ),
                ],
            },
        )
    finally:
        watchdog.cancel()
        await asyncio.gather(watchdog, return_exceptions=True)


async def recover():
    async with Session.begin() as db:
        rows = (
            await db.scalars(
                select(Run)
                .where(Run.status == "running", Run.lease_expires_at < utcnow())
                .with_for_update(skip_locked=True)
            )
        ).all()
        for run in rows:
            run.fence += 1
            run.status, run.completed_at = "interrupted", utcnow()
            run.error = {
                "code": "INTERRUPTED",
                "message": "Execution ownership expired during this answer. The provider request was not automatically repeated.",
                "retryable": True,
            }
            await add_event(db, run, "interrupted")
        queued = list(await db.scalars(select(Run.id).where(Run.status == "queued")))
    for run_id in queued:
        dispatch(run_id)


async def recovery_loop():
    while True:
        try:
            await recover()
        except SQLAlchemyError:
            logging.getLogger("emer.lifecycle").warning("Recovery deferred: database unavailable")
        await asyncio.sleep(5)


async def shutdown():
    for task in list(_tasks.values()):
        task.cancel()
    await asyncio.gather(*list(_tasks.values()), return_exceptions=True)
