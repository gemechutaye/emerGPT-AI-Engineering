"""Check the deployed index against source/configuration without calling a provider."""

import asyncio
import json
import sys


class IndexConfigurationError(ValueError):
    pass


def validate_index(bundle, source, retrieval_mode: str, embedding_model: str) -> None:
    if bundle.corpus_checksum != source.corpus_checksum:
        raise IndexConfigurationError("The active index belongs to a different source corpus.")
    source_configuration = {key: value for key, value in bundle.config.items() if key != "embedding"}
    if source_configuration != source.config:
        raise IndexConfigurationError(
            "The active source metadata or chunk/index configuration differs from this release. "
            "Rebuild the index explicitly before starting."
        )
    actual = {doc.doc_id: doc for doc in bundle.documents}
    expected = {doc.doc_id: doc for doc in source.documents}
    if actual != expected:
        raise IndexConfigurationError("The active index does not contain the exact supplied source records.")
    if retrieval_mode in {"semantic", "hybrid"}:
        embedding = bundle.config.get("embedding") or {}
        if embedding.get("model") != embedding_model:
            raise IndexConfigurationError(
                "The active index lacks the configured embedding space. Initialize it explicitly "
                "with `uv run emer ingest --embeddings --manifest config/corpus.json` before starting."
            )
        if getattr(source, "chunks", []) and embedding.get("unit") != "chunk":
            raise IndexConfigurationError("The active embedding space must cover the indexed source chunks.")


async def check() -> dict:
    from emer.services.ingestion import ingest
    from emer.services.retrieval import RetrievalService
    from emer.settings import settings
    from emer.storage.bundles import load_bundle
    from emer.storage.database import Session, engine
    from emer.storage.models import ActiveIndex
    from sqlalchemy import select

    try:
        source = await asyncio.to_thread(ingest, settings.corpus_manifest)
        async with Session() as db:
            index_id = await db.scalar(select(ActiveIndex.index_id).where(ActiveIndex.id == 1))
        if not index_id:
            raise IndexConfigurationError(
                "No corpus index is initialized. Run the explicit database setup in deployment/README.md."
            )
        bundle = await load_bundle(index_id)
        validate_index(bundle, source, settings.retrieval_mode, settings.embedding_model)
        # Reuse the application's checksum, vector coverage and dimension validation.
        RetrievalService(bundle)
        if settings.reranking_enabled:
            from emer.providers.reranker import get_reranker

            # local_files_only=True in the application adapter: readiness cannot download models.
            get_reranker(settings.retrieval_model_cache)
        embedding = bundle.config.get("embedding") or {}
        return {
            "status": "ready",
            "index_id": bundle.id,
            "checksum": bundle.checksum,
            "documents": len(bundle.documents),
            "retrieval_mode": settings.retrieval_mode,
            "embedding_model": embedding.get("model"),
            "embedding_dimensions": embedding.get("dimensions"),
            "provider_calls": 0,
        }
    finally:
        await engine.dispose()


if __name__ == "__main__":
    try:
        print(json.dumps(asyncio.run(check())))
    except IndexConfigurationError as error:
        print(f"EMER startup validation failed: {error}", file=sys.stderr)
        sys.exit(1)
    except Exception:
        # Database/provider URLs can contain credentials; never print their exceptions.
        print(
            "EMER startup validation failed: the database or source index could not be verified. "
            "Check database connectivity, migrations, source hashes and index integrity.",
            file=sys.stderr,
        )
        sys.exit(1)
