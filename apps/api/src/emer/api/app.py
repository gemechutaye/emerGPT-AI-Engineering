import asyncio
import json
import secrets
from contextlib import asynccontextmanager
from datetime import date, timedelta
from pathlib import Path
from uuid import uuid4

from emer.api import conversation_routes, history_routes, voice_routes
from emer.api.body_limit import BodyLimitMiddleware
from emer.api.dependencies import COOKIE, current_session, token_hash
from emer.api.errors import problem
from emer.contracts.conversations import ConversationDetail, ConversationList
from emer.contracts.http import (
    ContextUpdate,
    ConversationCreate,
    ConversationView,
    DraftCreate,
    DraftUpdate,
    DraftView,
    RunCreate,
    RunView,
    SessionView,
)
from emer.services import conversation_memory, conversation_titles, runs
from emer.services import conversations as conversation_service
from emer.services.ingestion import IngestionError
from emer.settings import settings
from emer.storage.bundles import load_bundle
from emer.storage.database import Session, engine
from emer.storage.models import BrowserSession, Conversation, Draft, ProviderAttempt, Run, RunEvent, utcnow
from emer.storage.retention import retention_loop
from fastapi import Depends, FastAPI, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError


@asynccontextmanager
async def lifespan(app):
    if settings.shared_workspace:
        from emer.services.shared_workspace import WORKSPACE_ID, initialize

        await initialize()
        await conversation_titles.queue_missing_titles(WORKSPACE_ID)
    recovery = asyncio.create_task(runs.recovery_loop())
    voice_recovery = asyncio.create_task(voice_routes.recovery_loop())
    retention = asyncio.create_task(retention_loop())
    metadata_recovery = asyncio.create_task(conversation_memory.recovery_loop())
    title_recovery = asyncio.create_task(conversation_titles.recovery_loop())
    yield
    recovery.cancel()
    voice_recovery.cancel()
    retention.cancel()
    metadata_recovery.cancel()
    title_recovery.cancel()
    await asyncio.gather(recovery, voice_recovery, retention, metadata_recovery, return_exceptions=True)
    await asyncio.gather(title_recovery, return_exceptions=True)
    await voice_routes.shutdown()
    await runs.shutdown()
    await conversation_memory.shutdown()
    await conversation_titles.shutdown()
    from emer.services.provider_activity import drain_provider_receipts

    await drain_provider_receipts()
    await engine.dispose()


app = FastAPI(title="EMER", version="0.1.0", lifespan=lifespan)
app.include_router(history_routes.router)
SESSION_DEPENDENCY = Depends(current_session)
voice_routes.install(app)
app.include_router(conversation_routes.router)
app.add_middleware(BodyLimitMiddleware)


@app.middleware("http")
async def boundary(request: Request, call_next):
    request_id = str(uuid4())
    origin = request.headers.get("origin")
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        if (origin and origin.rstrip("/") != settings.app_origin.rstrip("/")) or request.headers.get(
            "sec-fetch-site"
        ) == "cross-site":
            return JSONResponse(
                {
                    "error": {
                        "code": "ORIGIN_REJECTED",
                        "message": "Use the demo from its own page.",
                        "retryable": False,
                    },
                    "request_id": request_id,
                },
                status_code=403,
            )
        if (
            request.headers.get("content-length", "0").isdigit()
            and int(request.headers.get("content-length", "0")) > 100000
        ):
            return JSONResponse(
                {
                    "error": {
                        "code": "BODY_TOO_LARGE",
                        "message": "The request is too large.",
                        "retryable": False,
                    }
                },
                status_code=413,
            )
    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = (
        "no-referrer" if request.url.path.startswith("/share/") else "same-origin"
    )
    response.headers["X-Robots-Tag"] = "noindex, nofollow"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; connect-src 'self' https://api.openai.com wss://api.openai.com; media-src 'self' blob:; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self'; frame-ancestors 'none'; base-uri 'self'"
    )
    response.headers["Permissions-Policy"] = "microphone=(self), camera=(), geolocation=()"
    if request.url.path.startswith(("/api/", "/share/")):
        response.headers["Cache-Control"] = "no-store"
    return response


@app.exception_handler(SQLAlchemyError)
async def database_error(request, exc):
    return JSONResponse(
        {
            "error": {
                "code": "DATABASE_UNAVAILABLE",
                "message": "Saved work is temporarily unavailable. Please try again.",
                "retryable": True,
            }
        },
        status_code=503,
    )


@app.exception_handler(IngestionError)
async def corpus_error(request, exc):
    return JSONResponse(
        {
            "error": {
                "code": "CORPUS_UNAVAILABLE",
                "message": "The source index could not be verified. Please try again after it is restored.",
                "retryable": True,
            }
        },
        status_code=503,
    )


@app.exception_handler(RequestValidationError)
async def validation_error(request, exc):
    return JSONResponse(
        {
            "error": {
                "code": "INVALID_REQUEST",
                "message": "Check the request fields and try again.",
                "retryable": False,
            }
        },
        status_code=422,
    )


@app.post("/api/v1/session", response_model=SessionView)
async def bootstrap(request: Request, response: Response):
    if settings.shared_workspace:
        from emer.services.shared_workspace import current

        owner = await current()
        return SessionView(id=owner.id, expires_at=owner.expires_at)
    token = request.cookies.get(COOKIE)
    async with Session.begin() as db:
        existing = (
            await db.scalar(select(BrowserSession).where(BrowserSession.token_hash == token_hash(token)))
            if token
            else None
        )
        if existing and not existing.revoked and existing.expires_at > utcnow():
            return SessionView(id=existing.id, expires_at=existing.expires_at)
        token = secrets.token_urlsafe(32)
        session = BrowserSession(
            token_hash=token_hash(token), expires_at=utcnow() + timedelta(days=settings.session_days)
        )
        db.add(session)
        await db.flush()
    response.set_cookie(
        COOKIE,
        token,
        max_age=settings.session_days * 86400,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="strict",
        path="/",
    )
    return SessionView(id=session.id, expires_at=session.expires_at)


@app.post("/api/v1/session/reset", response_model=SessionView)
async def reset(request: Request, response: Response, owner=SESSION_DEPENDENCY):
    if settings.shared_workspace:
        # Resetting a browser never revokes the shared workspace or other browsers.
        return SessionView(id=owner.id, expires_at=owner.expires_at)
    async with Session.begin() as db:
        session = await db.get(BrowserSession, owner.id)
        session.revoked = True
        active = (
            await db.scalars(
                select(Run).where(Run.session_id == owner.id, Run.status.in_(["queued", "running"]))
            )
        ).all()
        for run in active:
            run.cancel_requested = True
    if hasattr(app.state, "close_owner_voice"):
        await app.state.close_owner_voice(owner.id)
    request.scope["headers"] = [(k, v) for k, v in request.scope["headers"] if k != b"cookie"]
    if hasattr(request, "_cookies"):
        request._cookies = {}
    return await bootstrap(request, response)


@app.get("/api/v1/corpus")
async def corpus():
    bundle = await load_bundle()
    return {
        "index_id": bundle.id,
        "checksum": bundle.checksum,
        "count": len(bundle.documents),
        "sources": [doc.model_dump(exclude={"text"}) for doc in bundle.documents],
    }


@app.get("/api/v1/patients")
async def patients():
    bundle = await load_bundle()
    return {
        "items": [
            {"id": doc.doc_id, "title": doc.title}
            for doc in bundle.documents
            if doc.category == "synthetic_patient"
        ]
    }


@app.get("/api/v1/sources/{doc_id}")
async def source(doc_id: str, index_id: str | None = None):
    try:
        bundle = await load_bundle(index_id)
    except LookupError:
        raise problem(404, "NOT_FOUND", "Source index not found.") from None
    doc = next((doc for doc in bundle.documents if doc.doc_id == doc_id), None)
    if not doc:
        raise problem(404, "NOT_FOUND", "Source not found in this index.")
    return {**doc.model_dump(), "index_id": bundle.id}


@app.get("/api/v1/conversations", response_model=ConversationList)
async def conversations(
    offset: int = 0, q: str | None = Query(default=None, max_length=200), owner=SESSION_DEPENDENCY
):
    return await conversation_service.list_owned(owner.id, offset, q)


@app.post("/api/v1/conversations", response_model=ConversationView, status_code=201)
async def create_conversation(body: ConversationCreate, owner=SESSION_DEPENDENCY):
    async with Session.begin() as db:
        row = Conversation(session_id=owner.id, title=body.title, title_origin="auto")
        db.add(row)
        await db.flush()
    return row


@app.get("/api/v1/conversations/{conversation_id}", response_model=ConversationDetail)
async def get_conversation(conversation_id: str, before: str | None = None, owner=SESSION_DEPENDENCY):
    from sqlalchemy import tuple_

    async with Session() as db:
        row = await db.get(Conversation, conversation_id)
        if not row or row.session_id != owner.id:
            raise problem(404, "NOT_FOUND", "Conversation not found.")
        statement = select(Run).where(Run.conversation_id == conversation_id)
        if before:
            cursor = await db.get(Run, before)
            if not cursor or cursor.conversation_id != conversation_id or cursor.session_id != owner.id:
                raise problem(
                    404, "NOT_FOUND", "Earlier answers could not be found. Reload the conversation."
                )
            statement = statement.where(tuple_(Run.created_at, Run.id) < (cursor.created_at, cursor.id))
        history = (await db.scalars(statement.order_by(Run.created_at.desc(), Run.id.desc()).limit(51))).all()
        page = history[:50]
        transcripts = await conversation_service.transcript_page(db, conversation_id)
        return {
            **ConversationView.model_validate(row).model_dump(),
            "runs": [RunView.model_validate(r) for r in reversed(page)],
            "next_cursor": page[-1].id if len(history) > 50 else None,
            "transcripts": transcripts.items,
            "transcripts_next_cursor": transcripts.next_cursor,
        }


@app.patch("/api/v1/conversations/{conversation_id}/context", response_model=ConversationView)
async def update_context(conversation_id: str, body: ContextUpdate, owner=SESSION_DEPENDENCY):
    bundle = await load_bundle()
    if body.patient_id and body.patient_id not in {
        d.doc_id for d in bundle.documents if d.category == "synthetic_patient"
    }:
        raise problem(422, "UNKNOWN_PATIENT", "That patient identifier is not in the supplied corpus.")
    if body.as_of:
        try:
            date.fromisoformat(body.as_of)
        except ValueError:
            raise problem(422, "INVALID_DATE", "Enter a valid date.")
    async with Session.begin() as db:
        row = await db.scalar(
            select(Conversation)
            .where(Conversation.id == conversation_id, Conversation.session_id == owner.id)
            .with_for_update()
        )
        if not row:
            raise problem(404, "NOT_FOUND", "Conversation not found.")
        if row.context_version != body.expected_version:
            raise problem(409, "CONTEXT_CHANGED", "The scope was changed elsewhere. Reload the conversation.")
        row.patient_id, row.as_of = body.patient_id, body.as_of
        row.context_version += 1
        conversation_service.touch(row)
        active = (
            await db.scalars(
                select(Run).where(
                    Run.conversation_id == conversation_id, Run.status.in_(["queued", "running"])
                )
            )
        ).all()
        for run in active:
            run.cancel_requested = True
    if hasattr(app.state, "close_conversation_voice"):
        await app.state.close_conversation_voice(conversation_id)
    return row


@app.post("/api/v1/conversations/{conversation_id}/runs", response_model=RunView, status_code=202)
async def submit(conversation_id: str, body: RunCreate, owner=SESSION_DEPENDENCY):
    return await runs.create_run(owner.id, conversation_id, body)


async def owned_run(run_id: str, owner_id: str) -> Run:
    async with Session() as db:
        run = await db.get(Run, run_id)
        if not run or run.session_id != owner_id:
            raise problem(404, "NOT_FOUND", "Answer not found.")
        return run


@app.get("/api/v1/runs/{run_id}")
async def get_run(run_id: str, owner=SESSION_DEPENDENCY):
    run = await owned_run(run_id, owner.id)
    async with Session() as db:
        events = (
            await db.scalars(select(RunEvent).where(RunEvent.run_id == run_id).order_by(RunEvent.sequence))
        ).all()
        attempts = (
            await db.scalars(
                select(ProviderAttempt)
                .where(ProviderAttempt.run_id == run_id)
                .order_by(ProviderAttempt.created_at)
            )
        ).all()
    return {
        **RunView.model_validate(run).model_dump(),
        "events": [
            {"sequence": e.sequence, "type": e.type, "data": e.data, "created_at": e.created_at}
            for e in events
        ],
        "provider_attempts": [
            {
                "id": a.id,
                "operation": a.operation,
                "model": a.model,
                "status": a.status,
                "usage": a.usage,
                "error_code": a.error_code,
                "created_at": a.created_at,
                "completed_at": a.completed_at,
            }
            for a in attempts
        ],
    }


@app.get("/api/v1/runs/{run_id}/events")
async def events(run_id: str, request: Request, after: int = 0, owner=SESSION_DEPENDENCY):
    await owned_run(run_id, owner.id)
    try:
        cursor = max(after, int(request.headers.get("last-event-id", "0")))
    except ValueError:
        cursor = 0

    async def stream():
        nonlocal cursor
        for _ in range(60):
            if await request.is_disconnected():
                return
            async with Session() as db:
                session = await db.get(BrowserSession, owner.id)
                if session.revoked or session.expires_at <= utcnow():
                    return
                items = (
                    await db.scalars(
                        select(RunEvent)
                        .where(RunEvent.run_id == run_id, RunEvent.sequence > cursor)
                        .order_by(RunEvent.sequence)
                    )
                ).all()
                run = await db.get(Run, run_id)
            for event in items:
                cursor = event.sequence
                yield f"id: {cursor}\nevent: run\ndata: {json.dumps({'sequence': cursor, 'type': event.type, 'data': event.data})}\n\n"
            if run is None or run.status in runs.TERMINAL:
                return
            yield ": heartbeat\n\n"
            await asyncio.sleep(1)

    return StreamingResponse(stream(), media_type="text/event-stream", headers={"X-Accel-Buffering": "no"})


@app.post("/api/v1/runs/{run_id}/cancel", response_model=RunView)
async def cancel(run_id: str, owner=SESSION_DEPENDENCY):
    await owned_run(run_id, owner.id)
    async with Session.begin() as db:
        row = await db.scalar(select(Run).where(Run.id == run_id).with_for_update())
        if row.status not in runs.TERMINAL:
            row.cancel_requested = True
            await runs.add_event(db, row, "cancellation_requested")
    return row


@app.post("/api/v1/drafts", response_model=DraftView, status_code=201)
async def create_draft(body: DraftCreate, owner=SESSION_DEPENDENCY):
    run = await owned_run(body.run_id, owner.id)
    if run.status != "completed" or not run.answer or run.answer.get("status") == "clarification":
        raise problem(409, "ANSWER_REQUIRED", "Create a draft from a completed answer.")
    sections = ["Internal draft — synthetic data", ""]
    for statement in run.answer["statements"]:
        sections.append(
            statement["text"]
            + " ["
            + ", ".join(dict.fromkeys(c["doc_id"] for c in statement["citations"]))
            + "]"
        )
    if run.answer["gaps"]:
        sections += ["", "Information not supplied:"] + [g["text"] for g in run.answer["gaps"]]
    async with Session.begin() as db:
        await conversation_service.owned(db, run.conversation_id, owner.id, lock=True)
        await db.scalar(select(Run).where(Run.id == run.id).with_for_update())
        existing = await db.scalar(select(Draft).where(Draft.run_id == run.id, Draft.session_id == owner.id))
        if existing:
            return existing
        row = Draft(session_id=owner.id, run_id=run.id, index_id=run.index_id, text="\n\n".join(sections))
        db.add(row)
        await db.flush()
    return row


@app.get("/api/v1/drafts")
async def drafts(before: str | None = None, run_id: str | None = None, owner=SESSION_DEPENDENCY):
    from sqlalchemy import tuple_

    async with Session() as db:
        statement = select(Draft).where(Draft.session_id == owner.id)
        if run_id:
            statement = statement.where(Draft.run_id == run_id)
        if before:
            cursor = await db.get(Draft, before)
            if not cursor or cursor.session_id != owner.id or (run_id and cursor.run_id != run_id):
                raise problem(404, "NOT_FOUND", "Earlier drafts could not be found. Reload the draft list.")
            statement = statement.where(tuple_(Draft.created_at, Draft.id) < (cursor.created_at, cursor.id))
        rows = (
            await db.scalars(statement.order_by(Draft.created_at.desc(), Draft.id.desc()).limit(51))
        ).all()
        page = rows[:50]
        return {
            "items": [DraftView.model_validate(row) for row in page],
            "next_cursor": page[-1].id if len(rows) > 50 else None,
        }


@app.get("/api/v1/drafts/{draft_id}", response_model=DraftView)
async def get_draft(draft_id: str, owner=SESSION_DEPENDENCY):
    async with Session() as db:
        row = await db.get(Draft, draft_id)
        if not row or row.session_id != owner.id:
            raise problem(404, "NOT_FOUND", "Draft not found.")
        return row


@app.patch("/api/v1/drafts/{draft_id}", response_model=DraftView)
async def update_draft(draft_id: str, body: DraftUpdate, owner=SESSION_DEPENDENCY):
    async with Session.begin() as db:
        row = await db.scalar(
            select(Draft).where(Draft.id == draft_id, Draft.session_id == owner.id).with_for_update()
        )
        if not row:
            raise problem(404, "NOT_FOUND", "Draft not found.")
        if row.version != body.expected_version:
            raise problem(409, "DRAFT_CHANGED", "This draft has a newer saved version. Reload before saving.")
        row.text, row.version, row.check = body.text, row.version + 1, None
    return row


@app.post("/api/v1/drafts/{draft_id}/recheck", response_model=DraftView)
async def recheck(draft_id: str, owner=SESSION_DEPENDENCY):
    from emer.services.drafts import check_draft

    return await check_draft(draft_id, owner.id)


@app.get("/api/v1/diagnostics")
async def diagnostics(owner=SESSION_DEPENDENCY):
    async with Session() as db:
        rows = (
            await db.scalars(
                select(Run).where(Run.session_id == owner.id).order_by(Run.created_at.desc()).limit(30)
            )
        ).all()
    return {
        "items": [
            {"id": r.id, "status": r.status, "index_id": r.index_id, "metrics": r.metrics} for r in rows
        ],
        "generation_model": settings.openrouter_model,
        "live_model": settings.openai_live_model,
    }


@app.get("/api/v1/evaluations")
async def evaluations():
    report = Path("config/evaluation-summary.json")
    if report.is_file():
        from emer.services.answering import ANSWER_PROMPT_ID, CHECKER_PROMPT_ID

        return {
            "status": "historical_evidence",
            "message": "Saved evaluation results apply only to their recorded source/configuration, not automatically to the running system.",
            "runtime": {
                "model": settings.openrouter_model,
                "answer_prompt": ANSWER_PROMPT_ID,
                "checker_prompt": CHECKER_PROMPT_ID,
                "retrieval_mode_requested": settings.retrieval_mode,
                "retrieval_top_k": settings.retrieval_top_k,
        "context_token_budget": settings.context_token_budget,
        "reranking_enabled": settings.reranking_enabled,
        "generator_model": settings.openrouter_model,
        "verifier_model": settings.verifier_model,
            },
            "historical_report": json.loads(report.read_text()),
        }
    return {
        "status": "pending",
        "message": "Measured provider comparisons have not been completed.",
        "reports": [],
    }


@app.get("/api/v1/health/live")
async def health_live():
    return {"status": "ok"}


@app.get("/api/v1/health/ready")
async def health_ready():
    async with Session() as db:
        await db.execute(text("SELECT 1"))
    try:
        bundle = await load_bundle()
    except RuntimeError:
        return JSONResponse({"status": "no_active_corpus"}, status_code=503)
    try:
        mode, note = runs.effective_retrieval_mode(bundle)
        if settings.reranking_enabled:
            from emer.providers.reranker import get_reranker
            await asyncio.to_thread(get_reranker, settings.retrieval_model_cache)
    except (ValueError, RuntimeError, OSError) as exc:
        return JSONResponse({"status": "retrieval_not_ready", "reason": str(exc),
                             "index_id": bundle.id}, status_code=503)
    return {
        "status": "ready",
        "index_id": bundle.id,
        "documents": len(bundle.documents),
        "generation_configured": bool(settings.openrouter_api_key),
        "live_configured": bool(settings.openai_api_key),
        "retrieval_mode": mode,
        "retrieval_mode_requested": settings.retrieval_mode,
        "retrieval_top_k": settings.retrieval_top_k,
        "chunks": len(bundle.chunks),
        "context_token_budget": settings.context_token_budget,
        "reranking_enabled": settings.reranking_enabled,
        "generator_model": settings.openrouter_model,
        "verifier_model": settings.verifier_model,
        "embedding_model": (bundle.config.get("embedding") or {}).get("model"),
        "retrieval_note": note,
    }


web_dist = Path("apps/web/dist")
if (web_dist / "assets").exists():
    app.mount("/assets", StaticFiles(directory=web_dist / "assets"), name="assets")


@app.get("/favicon.svg")
async def favicon():
    if (web_dist / "favicon.svg").exists():
        return FileResponse(web_dist / "favicon.svg", media_type="image/svg+xml")
    return Response(status_code=404)


@app.get("/")
async def index():
    if (web_dist / "index.html").exists():
        return FileResponse(web_dist / "index.html")
    return JSONResponse(
        {"status": "frontend_build_required", "command": "npm --prefix apps/web run build"}, status_code=503
    )


@app.get("/share/{token}", include_in_schema=False)
async def shared_entrypoint(token: str):
    return await index()
