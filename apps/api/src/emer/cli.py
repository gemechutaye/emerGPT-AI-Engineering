import asyncio
import json
from pathlib import Path

import typer

from emer.settings import settings

app = typer.Typer(no_args_is_help=True, pretty_exceptions_show_locals=False)


@app.command()
def prepare_retrieval():
    """Download pinned local reranking assets before starting the application."""
    from emer.providers.reranker import MODEL_ID, MODEL_REVISION, prepare_reranker

    prepare_reranker(settings.retrieval_model_cache)
    typer.echo(json.dumps({"model": MODEL_ID, "revision": MODEL_REVISION, "ready": True}))


@app.command()
def ingest(manifest: str = settings.corpus_manifest, if_missing: bool = False, embeddings: bool = False):
    """Verify explicit source manifest and atomically activate its durable index.

    --embeddings builds the configured chunk space with bounded batches and exact-input reuse.
    """
    from sqlalchemy import select

    from emer.services.ingestion import ingest as build_index
    from emer.storage.bundles import activate, load_bundle
    from emer.storage.database import Session
    from emer.storage.models import ActiveIndex

    receipts = []

    async def prepare():
        if if_missing:
            async with Session() as db:
                current = await db.scalar(select(ActiveIndex.index_id).where(ActiveIndex.id == 1))
            if current:
                existing = await load_bundle(current)
                desired = await asyncio.to_thread(build_index, manifest)
                comparable = lambda config: {key: value for key, value in config.items() if key != "embedding"}
                vector_ready = not embeddings or (
                    (existing.config.get("embedding") or {}).get("unit") == "chunk"
                    and existing.config["embedding"]["model"] == settings.embedding_model
                )
                if (existing.corpus_checksum == desired.corpus_checksum
                    and comparable(existing.config) == comparable(desired.config) and vector_ready):
                    return existing
        if embeddings:
            from emer.providers.openrouter import OpenRouterClient

            provider = OpenRouterClient(settings.openrouter_api_key, settings.openrouter_model)
            return await activate(manifest, settings.embedding_model, provider, receipts=receipts)
        return await activate(manifest)

    bundle = asyncio.run(prepare())
    typer.echo(
        json.dumps(
            {
                "index_id": bundle.id,
                "checksum": bundle.checksum,
                "documents": len(bundle.documents),
                "chunks": len(bundle.chunks),
                "embedding": (bundle.config.get("embedding") or {}).get("model"),
                "embedding_batches": receipts,
            }
        )
    )


@app.command()
def openapi(output: str = "apps/web/openapi.json"):
    """Export the real HTTP contract without starting a server."""
    from emer.api.app import app as api

    Path(output).write_text(json.dumps(api.openapi(), indent=2) + "\n")
    typer.echo(output)


@app.command()
def smoke(url: str = "http://localhost:8017"):
    """Check health, corpus, session ownership and a persistent conversation without inference."""
    import httpx

    with httpx.Client(base_url=url, headers={"Origin": url}) as client:
        for path in ["/api/v1/health/ready", "/api/v1/corpus"]:
            response = client.get(path)
            response.raise_for_status()
        session = client.post("/api/v1/session")
        session.raise_for_status()
        row = client.post("/api/v1/conversations", json={"title": "Smoke check"})
        row.raise_for_status()
        cid = row.json()["id"]
        client.get("/api/v1/conversations/" + cid).raise_for_status()
        with httpx.Client(base_url=url, headers={"Origin": url}) as stranger:
            other_session = stranger.post("/api/v1/session")
            other_session.raise_for_status()
            shared = session.json()["id"] == other_session.json()["id"]
            assert stranger.get("/api/v1/conversations/" + cid).status_code == (200 if shared else 404)
        typer.echo(
            json.dumps(
                {
                    "status": "passed",
                    "checks": 5,
                    "conversation_id": cid,
                    "history_scope": "shared" if shared else "browser",
                    "classification": "local HTTP, no inference",
                }
            )
        )


@app.command()
def backup(destination: str):
    """Create a consistent private database dump below .local/."""
    from emer.storage.backup import backup as perform

    typer.echo(json.dumps(perform(destination)))


@app.command()
def prune(apply: bool = typer.Option(False, "--apply", help="Delete the reported expired work.")):
    """Preview one batch of expired anonymous work; deletion requires --apply."""
    from emer.storage.database import engine
    from emer.storage.retention import prune_expired_sessions

    async def perform():
        try:
            return await prune_expired_sessions(apply=apply)
        finally:
            await engine.dispose()

    typer.echo(json.dumps(asyncio.run(perform())))


@app.command()
def restore(source: str, database: str):
    """Restore into a new emer_restore_* database; RESTORE_DATABASE_URL can select its server."""
    from emer.storage.backup import restore as perform

    typer.echo(json.dumps(perform(source, database)))


@app.command()
def session_reset(url: str = "http://localhost:8017"):
    """Exercise anonymous session reset for a new CLI visitor; browser reset is in the UI."""
    import httpx

    with httpx.Client(base_url=url, headers={"Origin": url}) as client:
        first = client.post("/api/v1/session")
        first.raise_for_status()
        reset = client.post("/api/v1/session/reset")
        reset.raise_for_status()
        if first.json()["id"] == reset.json()["id"]:
            typer.echo("Shared workspace retained; saved history remains available in every browser.")
        else:
            typer.echo("Anonymous CLI session replaced; previous ownership revoked.")
