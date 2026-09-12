import asyncio
from pathlib import Path
from uuid import uuid4

from emer.services.ingestion import (
    Bundle,
    IngestionError,
    embedding_inputs,
    ingest,
    read_embeddings,
    with_embeddings,
)
from emer.settings import settings
from emer.storage.database import Session
from emer.storage.models import ActiveIndex, CorpusIndex
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

_cache: dict[str, Bundle] = {}
CACHE_MAX_ENTRIES = 4
CACHE_MAX_BYTES = 64 * 1024 * 1024
EMBEDDING_BATCH_INPUTS = 64
EMBEDDING_BATCH_BYTES = 180_000
EMBEDDING_INPUT_BYTES = 20_000


def _remember(bundle: Bundle) -> None:
    # dict insertion order supplies LRU semantics without another dependency.
    _cache.pop(bundle.id, None)
    _cache[bundle.id] = bundle
    while _cache and (
        len(_cache) > CACHE_MAX_ENTRIES or sum(len(item.data) for item in _cache.values()) > CACHE_MAX_BYTES
    ):
        del _cache[next(iter(_cache))]


async def build_embeddings(
    bundle: Bundle,
    model: str,
    provider,
    *,
    previous: Bundle | None = None,
    receipts: list[dict] | None = None,
) -> Bundle:
    """Reuse only identical immutable inputs; bound every provider batch independently."""
    inputs = embedding_inputs(bundle)
    vectors = {}
    old_meta = previous.config.get("embedding", {}) if previous else {}
    if (
        previous
        and old_meta
        and old_meta.get("model") == model
        and old_meta.get("input") in {"title-section-path-chunk-v1", "title-newline-text-v1"}
    ):
        previous_inputs = dict(embedding_inputs(previous))
        previous_vectors = await asyncio.to_thread(read_embeddings, previous)
        # Exact input equality allows short unchanged historical documents to be reused
        # under their new chunk IDs. Source identity alone would miss changed titles.
        vectors_by_input = {
            text: previous_vectors[key] for key, text in previous_inputs.items() if key in previous_vectors
        }
        vectors = {key: vectors_by_input[text] for key, text in inputs if text in vectors_by_input}
    pending = [(key, text) for key, text in inputs if key not in vectors]
    if any(not text or len(text.encode()) > EMBEDDING_INPUT_BYTES for _, text in pending):
        raise IngestionError("Embedding title/section/source input exceeds the per-input byte bound")
    batches = []
    batch = []
    byte_count = 0
    for item in pending:
        size = len(item[1].encode())
        if batch and (len(batch) >= EMBEDDING_BATCH_INPUTS or byte_count + size > EMBEDDING_BATCH_BYTES):
            batches.append(batch)
            batch, byte_count = [], 0
        batch.append(item)
        byte_count += size
    if batch:
        batches.append(batch)
    for batch in batches:
        result = await provider.embed([text for _, text in batch], model)
        if receipts is not None:
            receipts.append(
                {
                    "inputs": len(batch),
                    "input_bytes": sum(len(text.encode()) for _, text in batch),
                    "usage": result.usage.model_dump(),
                    "rate_limit_retries": result.rate_limit_retries,
                }
            )
        if result.model != model or len(result.vectors) != len(batch):
            raise IngestionError("Embedding provider changed model space or omitted batch rows")
        vectors.update({key: vector for (key, _), vector in zip(batch, result.vectors, strict=True)})
    return await asyncio.to_thread(with_embeddings, bundle, model, vectors)


async def store_and_activate(bundle: Bundle) -> Bundle:
    async with Session.begin() as db:
        await db.execute(
            insert(CorpusIndex)
            .values(
                id=bundle.id,
                checksum=bundle.checksum,
                corpus_checksum=bundle.corpus_checksum,
                bundle=bundle.data,
                manifest=bundle.config,
            )
            .on_conflict_do_nothing(index_elements=["id"])
        )
        stored = await db.get(CorpusIndex, bundle.id)
        if stored is None or (
            stored.checksum != bundle.checksum
            or stored.corpus_checksum != bundle.corpus_checksum
            or stored.manifest != bundle.config
            or stored.bundle != bundle.data
        ):
            raise IngestionError("Stored index identity already refers to a different retrieval bundle")
        await db.execute(
            insert(ActiveIndex)
            .values(id=1, index_id=bundle.id)
            .on_conflict_do_update(index_elements=["id"], set_={"index_id": bundle.id})
        )
    _remember(bundle)
    return bundle


async def activate(
    manifest: str, embedding_model: str | None = None, provider=None, *, receipts: list[dict] | None = None
) -> Bundle:
    """Build all verified chunk/vector bytes before atomically replacing the active pointer."""
    bundle = await asyncio.to_thread(ingest, manifest)
    if embedding_model:
        if provider is None:
            raise ValueError("An embedding-enabled index needs a configured provider")
        try:
            previous = await load_bundle()
        except IngestionError as exc:
            if str(exc) != "No active corpus; run ingestion first":
                raise
            previous = None
        bundle = await build_embeddings(
            bundle, embedding_model, provider, previous=previous, receipts=receipts
        )
    return await store_and_activate(bundle)


async def load_bundle(index_id: str | None = None) -> Bundle:
    async with Session() as db:
        if not index_id:
            index_id = await db.scalar(select(ActiveIndex.index_id).where(ActiveIndex.id == 1))
        if not index_id:
            raise IngestionError("No active corpus; run ingestion first")
        if index_id in _cache:
            bundle = _cache.pop(index_id)
            _cache[index_id] = bundle
            return bundle
        row = await db.get(CorpusIndex, index_id)
        if row is None:
            raise LookupError("Source index not found")
        bundle = await asyncio.to_thread(Bundle.from_bytes, row.bundle, row.checksum)
        if (
            bundle.id != index_id
            or bundle.corpus_checksum != row.corpus_checksum
            or bundle.config != row.manifest
        ):
            raise IngestionError("Stored retrieval metadata differs from its immutable bundle")
    target = Path(settings.cache_dir)
    target.mkdir(parents=True, exist_ok=True)
    path = target / (bundle.checksum + ".sqlite")
    temporary = path.with_suffix(f".{uuid4().hex}.tmp")
    try:
        temporary.write_bytes(bundle.data)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    _remember(bundle)
    return bundle
