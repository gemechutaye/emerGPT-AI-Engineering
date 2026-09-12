"""Explicit source manifest → finalized, reproducible SQLite retrieval bytes."""

from __future__ import annotations

import json
import math
import sqlite3
from dataclasses import dataclass, field
from datetime import date
from hashlib import sha256
from pathlib import Path
from typing import Any

import numpy as np
from emer.contracts.answer import SourceDocument
from emer.contracts.knowledge import SourceChunk
from emer.domain.chunking import CHUNKER_CONFIG, chunk_document

FIELDS = {"doc_id", "title", "category", "version", "effective_date", "authority", "text"}
INDEX_VERSION = "source-chunks-fts5-v3"


class IngestionError(ValueError):
    pass


def canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


@dataclass(frozen=True)
class Bundle:
    id: str
    checksum: str
    data: bytes
    documents: list[SourceDocument]
    corpus_checksum: str
    config: dict[str, Any]
    chunks: list[SourceChunk] = field(default_factory=list)

    @property
    def bytes(self) -> bytes:
        return self.data

    @classmethod
    def from_bytes(cls, data: bytes, expected_checksum: str | None = None) -> Bundle:
        checksum = sha256(data).hexdigest()
        if expected_checksum and checksum != expected_checksum:
            raise IngestionError("Retrieval bundle checksum mismatch")
        connection = sqlite3.connect(":memory:")
        try:
            connection.deserialize(data)
            connection.execute("PRAGMA query_only=ON")
            if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise IngestionError("Retrieval bundle integrity failed")
            metadata = dict(connection.execute("SELECT key, value FROM metadata"))
            config = json.loads(metadata["config"])
            expected_id = sha256(
                canonical({"corpus_checksum": metadata["corpus_checksum"], "config": config})
            ).hexdigest()[:24]
            if metadata["id"] != expected_id:
                raise IngestionError("Retrieval bundle identity differs from its configuration")
            rows = list(connection.execute("SELECT doc_id, payload FROM documents ORDER BY doc_id"))
            docs = [SourceDocument.model_validate_json(row[1]) for row in rows]
            if not docs or any(key != doc.doc_id for (key, _), doc in zip(rows, docs, strict=True)):
                raise IngestionError("Source identifier differs from its stored key")
            for doc in docs:
                if sha256(doc.text.encode()).hexdigest() != doc.sha256:
                    raise IngestionError("Source hash differs from bundle metadata")
            chunks = []
            if config.get("chunker"):
                rows = list(
                    connection.execute("SELECT chunk_id, payload FROM chunks ORDER BY doc_id, ordinal")
                )
                chunks = [SourceChunk.model_validate_json(row[1]) for row in rows]
                expected_chunks = [
                    chunk for doc in docs for chunk in chunk_document(doc, config=config["chunker"])
                ]
                if chunks != expected_chunks or any(
                    key != chunk.chunk_id for (key, _), chunk in zip(rows, chunks, strict=True)
                ):
                    raise IngestionError("Chunk spans, identity or metadata differ from original sources")
                by_id = {doc.doc_id: doc for doc in docs}
                indexed_chunks = list(
                    connection.execute(
                        "SELECT chunk_id, doc_id, title, section_path, text FROM chunk_search ORDER BY doc_id, chunk_id"
                    )
                )
                expected_search = sorted(
                    (
                        chunk.chunk_id,
                        chunk.doc_id,
                        by_id[chunk.doc_id].title,
                        " > ".join(chunk.section_path),
                        chunk.text,
                    )
                    for chunk in chunks
                )
                if sorted(indexed_chunks) != expected_search:
                    raise IngestionError("Chunk search representation differs from original source spans")
            indexed = list(connection.execute("SELECT doc_id, title, text FROM search ORDER BY doc_id"))
            if indexed != [(doc.doc_id, doc.title, doc.text) for doc in docs]:
                raise IngestionError("Search representation differs from original source payloads")
        except (sqlite3.DatabaseError, KeyError, TypeError, ValueError) as exc:
            if isinstance(exc, IngestionError):
                raise
            raise IngestionError("Unreadable retrieval bundle") from exc
        finally:
            connection.close()
        bundle = cls(metadata["id"], checksum, data, docs, metadata["corpus_checksum"], config, chunks)
        if config.get("embedding"):
            read_embeddings(bundle, compact=True)
        return bundle


def _checked_bytes(base: Path, entry: dict[str, Any]) -> bytes:
    path = (base / entry["path"]).resolve()
    if path.suffix not in {".jsonl", ".txt"}:
        raise IngestionError("Only the explicitly declared source file is ingestible")
    raw = path.read_bytes()
    if len(raw) != entry["bytes"] or sha256(raw).hexdigest() != entry["sha256"]:
        raise IngestionError(f"Original source verification failed: {path.name}")
    return raw


def ingest(manifest_path: str | Path) -> Bundle:
    path = Path(manifest_path).resolve()
    manifest = json.loads(path.read_text())
    schema = manifest.get("schema_version")
    fields = (
        {"schema_version", "corpus", "preserve", "policies"}
        if schema == 1
        else {"schema_version", "corpus", "preserve", "source_metadata", "authority"}
    )
    if schema not in {1, 2} or set(manifest) != fields:
        raise IngestionError("Unsupported corpus manifest")
    raw = _checked_bytes(path.parent, manifest["corpus"])
    for preserved in manifest["preserve"]:
        _checked_bytes(path.parent, preserved)
    documents = []
    ids = set()
    for number, line in enumerate(raw.splitlines(), 1):
        record = json.loads(line)
        if set(record) != FIELDS or any(not isinstance(v, str) or not v for v in record.values()):
            raise IngestionError(f"Invalid source fields at record {number}")
        date.fromisoformat(record["effective_date"])
        if record["doc_id"] in ids:
            raise IngestionError("Duplicate source identifier")
        ids.add(record["doc_id"])
        documents.append(SourceDocument(**record, sha256=sha256(record["text"].encode()).hexdigest()))
    if len(documents) != manifest["corpus"]["records"]:
        raise IngestionError("Source record count mismatch")
    by_id = {doc.doc_id: doc for doc in documents}
    for policy in manifest.get("policies", []):
        doc = by_id[policy["doc_id"]]
        if policy["provenance_quote"] not in doc.text or (
            policy.get("supersession_quote") and policy["supersession_quote"] not in doc.text
        ):
            raise IngestionError("Policy annotation is not backed by its exact source passage")
        if policy["valid_from"] != doc.effective_date:
            raise IngestionError("Policy annotation effective date mismatch")
        date.fromisoformat(policy["valid_from"])
        if policy["valid_through"]:
            through = date.fromisoformat(policy["valid_through"])
            source_date = f"{through.strftime('%B')} {through.day}, {through.year}"
            if (
                source_date not in policy["provenance_quote"]
                or policy["valid_through"] < policy["valid_from"]
            ):
                raise IngestionError("Policy expiry annotation is not backed by its source passage")
        if any(previous not in by_id for previous in policy["supersedes"]):
            raise IngestionError("Unknown superseded source")
        if any(previous not in policy.get("supersession_quote", "") for previous in policy["supersedes"]):
            raise IngestionError("Policy supersession annotation is not backed by its source passage")
    catalog = None
    if schema == 2:
        from emer.domain.source_metadata import SourceMetadataError, policy_annotations, validate_catalog

        try:
            catalog = validate_catalog(documents, manifest["source_metadata"], manifest["authority"])
        except SourceMetadataError as exc:
            raise IngestionError(str(exc)) from exc
        policies = policy_annotations(catalog)
    else:
        policies = manifest["policies"]
    config = {
        "index_version": INDEX_VERSION,
        "sqlite_version": sqlite3.sqlite_version,
        "embedding": None,
        "chunker": dict(CHUNKER_CONFIG),
        "policies": policies,
    }
    if catalog is not None:
        config["source_catalog"] = catalog
    corpus_checksum = sha256(raw).hexdigest()
    index_id = sha256(canonical({"corpus_checksum": corpus_checksum, "config": config})).hexdigest()[:24]
    db = sqlite3.connect(":memory:")
    try:
        db.execute("PRAGMA page_size=4096")
        db.execute("CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        db.execute("CREATE TABLE documents (doc_id TEXT PRIMARY KEY, payload TEXT NOT NULL)")
        db.execute("CREATE VIRTUAL TABLE search USING fts5(doc_id, title, text, tokenize='porter unicode61')")
        db.execute(
            "CREATE TABLE chunks (chunk_id TEXT PRIMARY KEY, doc_id TEXT NOT NULL, ordinal INTEGER NOT NULL, payload TEXT NOT NULL, UNIQUE(doc_id, ordinal))"
        )
        db.execute(
            "CREATE VIRTUAL TABLE chunk_search USING fts5(chunk_id UNINDEXED, doc_id UNINDEXED, title, section_path, text, tokenize='porter unicode61')"
        )
        db.executemany(
            "INSERT INTO metadata VALUES (?, ?)",
            [("id", index_id), ("corpus_checksum", corpus_checksum), ("config", canonical(config).decode())],
        )
        for doc in sorted(documents, key=lambda d: d.doc_id):
            db.execute("INSERT INTO documents VALUES (?, ?)", (doc.doc_id, doc.model_dump_json()))
            db.execute("INSERT INTO search VALUES (?, ?, ?)", (doc.doc_id, doc.title, doc.text))
            for chunk in chunk_document(doc):
                db.execute(
                    "INSERT INTO chunks VALUES (?, ?, ?, ?)",
                    (chunk.chunk_id, doc.doc_id, chunk.ordinal, chunk.model_dump_json()),
                )
                db.execute(
                    "INSERT INTO chunk_search VALUES (?, ?, ?, ?, ?)",
                    (chunk.chunk_id, doc.doc_id, doc.title, " > ".join(chunk.section_path), chunk.text),
                )
        db.execute("INSERT INTO search(search) VALUES ('optimize')")
        db.execute("INSERT INTO chunk_search(chunk_search) VALUES ('optimize')")
        db.commit()
        db.execute("VACUUM")
        data = db.serialize()
    finally:
        db.close()
    return Bundle.from_bytes(data)


def embedding_inputs(bundle: Bundle) -> list[tuple[str, str]]:
    """The exact embedding input identity is shared by indexing and reuse checks."""
    if bundle.chunks:
        docs = {doc.doc_id: doc for doc in bundle.documents}
        return [
            (chunk.chunk_id, "\n".join([docs[chunk.doc_id].title, *chunk.section_path, chunk.text]))
            for chunk in bundle.chunks
        ]
    return [(doc.doc_id, doc.title + "\n" + doc.text) for doc in bundle.documents]


def _vector_checksum(rows: list[tuple[str, bytes]]) -> str:
    digest = sha256()
    for key, vector in rows:
        encoded = key.encode()
        digest.update(len(encoded).to_bytes(4, "big"))
        digest.update(encoded)
        digest.update(len(vector).to_bytes(4, "big"))
        digest.update(vector)
    return digest.hexdigest()


def read_embeddings(bundle: Bundle, *, compact: bool = False) -> dict[str, list[float] | np.ndarray]:
    """Validate compact current vectors and historical JSON document spaces alike."""
    metadata = bundle.config.get("embedding")
    if not metadata:
        return {}
    expected_ids = (
        {chunk.chunk_id for chunk in bundle.chunks}
        if metadata.get("unit") == "chunk"
        else {doc.doc_id for doc in bundle.documents}
    )
    db = sqlite3.connect(":memory:")
    try:
        db.deserialize(bundle.data)
        db.execute("PRAGMA query_only=ON")
        if metadata.get("format") == "float32-le":
            rows = list(db.execute("SELECT unit_id, vector FROM embeddings ORDER BY unit_id"))
            if any(
                not isinstance(value, bytes) or len(value) != metadata["dimensions"] * 4 for _, value in rows
            ):
                raise IngestionError("Embedding cache dimensions or storage format are invalid")
            digest = _vector_checksum(rows)
            vectors = {key: np.frombuffer(value, dtype="<f4") for key, value in rows}
            if not compact:
                vectors = {key: value.tolist() for key, value in vectors.items()}
        else:
            vectors = {
                key: json.loads(value)
                for key, value in db.execute("SELECT doc_id, vector FROM embeddings ORDER BY doc_id")
            }
            digest = sha256(canonical(vectors)).hexdigest()
        if set(vectors) != expected_ids or digest != metadata["vectors_sha256"]:
            raise IngestionError("Embedding cache integrity failed")
        if any(
            len(vector) != metadata["dimensions"]
            or not np.isfinite(vector).all()
            or not np.any(vector)
            for vector in vectors.values()
        ):
            raise IngestionError("Embedding cache dimensions or numeric values are invalid")
        return vectors
    except (sqlite3.DatabaseError, KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, IngestionError):
            raise
        raise IngestionError("Unreadable embedding cache") from exc
    finally:
        db.close()


def with_embeddings(bundle: Bundle, model: str, vectors: dict[str, list[float]]) -> Bundle:
    """Build one immutable vector space covering every indexed retrieval unit."""
    if not model or set(vectors) != {key for key, _ in embedding_inputs(bundle)}:
        raise IngestionError("An embedding space must cover every source retrieval unit exactly once")
    dimensions = {len(vector) for vector in vectors.values()}
    if len(dimensions) != 1 or not next(iter(dimensions)):
        raise IngestionError("Embedding dimensions are inconsistent or empty")
    if any(not all(math.isfinite(v) for v in vector) or not any(vector) for vector in vectors.values()):
        raise IngestionError("Embedding vectors must be finite and nonzero")
    rows = []
    for key in sorted(vectors):
        with np.errstate(over="ignore", under="ignore"):
            vector = np.asarray(vectors[key], dtype="<f4")
        if not np.isfinite(vector).all() or not np.any(vector):
            raise IngestionError("Embedding vectors cannot be represented as finite nonzero float32")
        rows.append((key, vector.tobytes()))
    config = {
        **bundle.config,
        "embedding": {
            "model": model,
            "dimensions": next(iter(dimensions)),
            "unit": "chunk" if bundle.chunks else "document",
            "format": "float32-le",
            "input": "title-section-path-chunk-v1" if bundle.chunks else "title-newline-text-v1",
            "vectors_sha256": _vector_checksum(rows),
        },
    }
    index_id = sha256(canonical({"corpus_checksum": bundle.corpus_checksum, "config": config})).hexdigest()[
        :24
    ]
    db = sqlite3.connect(":memory:")
    try:
        db.deserialize(bundle.data)
        db.execute("DROP TABLE IF EXISTS embeddings")
        db.execute("CREATE TABLE embeddings (unit_id TEXT PRIMARY KEY, vector BLOB NOT NULL)")
        db.executemany("INSERT INTO embeddings VALUES (?, ?)", rows)
        db.execute("UPDATE metadata SET value=? WHERE key='id'", (index_id,))
        db.execute("UPDATE metadata SET value=? WHERE key='config'", (canonical(config).decode(),))
        db.commit()
        db.execute("VACUUM")
        data = db.serialize()
    finally:
        db.close()
    return Bundle.from_bytes(data)
