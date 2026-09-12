"""Source-addressable retrieval across long, structured and historical corpora."""

import json
import sqlite3
from dataclasses import replace
from hashlib import sha256
from itertools import pairwise
from pathlib import Path
from types import SimpleNamespace

import pytest
from emer.contracts.answer import SourceDocument
from emer.domain.chunking import CHUNKER_CONFIG, chunk_document, count_tokens
from emer.services.ingestion import (
    Bundle,
    IngestionError,
    canonical,
    embedding_inputs,
    ingest,
    read_embeddings,
    with_embeddings,
)
from emer.storage.bundles import build_embeddings

ROOT = Path(__file__).resolve().parents[3]


def source(text, doc_id="DOC-001", title="Source of the examples"):
    return SourceDocument(
        doc_id=doc_id,
        title=title,
        category="operations",
        version="1.0",
        effective_date="2026-01-01",
        authority="Approved Training Dataset",
        text=text,
        sha256=sha256(text.encode()).hexdigest(),
    )


def manifest_for(tmp_path, documents):
    raw = b"".join(canonical(doc.model_dump(exclude={"sha256"})) + b"\n" for doc in documents)
    corpus = tmp_path / "source.jsonl"
    corpus.write_bytes(raw)
    manifest = {
        "schema_version": 1,
        "corpus": {
            "path": str(corpus),
            "records": len(documents),
            "bytes": len(raw),
            "sha256": sha256(raw).hexdigest(),
        },
        "preserve": [],
        "policies": [],
    }
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest))
    return path


def mutate(bundle, callback):
    db = sqlite3.connect(":memory:")
    try:
        db.deserialize(bundle.data)
        callback(db)
        db.commit()
        return db.serialize()
    finally:
        db.close()


def test_short_source_remains_whole_and_immutable():
    text = "\nA patient’s status: café, 東京, 👩🏽‍⚕️.\n\nDo not infer a date.  "
    doc = source(text)
    chunks = chunk_document(doc)
    assert len(chunks) == 1
    assert (chunks[0].start, chunks[0].end, chunks[0].text) == (0, len(text), text)
    assert chunks[0].source_sha256 == doc.sha256
    assert chunks == chunk_document(doc)
    assert chunks[0].chunk_id != chunk_document(source(text + " "))[0].chunk_id
    config = {**CHUNKER_CONFIG, "max_tokens": 256}
    assert chunks[0].chunk_id != chunk_document(doc, config=config)[0].chunk_id


def test_long_unicode_document_has_complete_exact_bounded_spans():
    text = "  \n# Overview\n\n" + (
        "Every café team records uncertainty before proceeding. 東京 remains unchanged. " * 120
    )
    text += "\n\n## Steps\n\n" + "".join(
        f"- Step {i}: preserve the clinician’s exact words.\n" for i in range(70)
    )
    text += "\n## Schedule\n\n| Item | Interval |\n|---|---|\n" + "".join(
        f"| Entry {i} | {i + 1} weeks |\n" for i in range(70)
    )
    chunks = chunk_document(source(text))
    assert len(chunks) > 10
    covered = set()
    for i, chunk in enumerate(chunks):
        assert chunk.ordinal == i
        assert chunk.text == text[chunk.start : chunk.end]
        assert chunk.token_count == count_tokens(chunk.text) <= 320
        covered.update(range(chunk.start, chunk.end))
        if i and chunk.start < chunks[i - 1].end:
            assert count_tokens(text[chunk.start : chunks[i - 1].end]) <= 40
    assert covered == set(range(len(text)))
    assert any(chunk.section_path == ["Overview", "Steps"] for chunk in chunks)
    assert any(chunk.section_path == ["Overview", "Schedule"] for chunk in chunks)
    assert any(chunks[i].start < chunks[i - 1].end for i in range(1, len(chunks)))
    for row in text.splitlines(keepends=True):
        if row.startswith(("- Step", "| Entry")):
            assert any(row in chunk.text for chunk in chunks), "short list items/table rows stay whole"


def test_unbroken_unicode_and_special_token_text_is_not_corrupted():
    doc = source("漢字🧬" * 1500 + "<|endoftext|>" * 100)
    chunks = chunk_document(doc)
    assert len(chunks) > 10
    assert chunks[0].start == 0 and chunks[-1].end == len(doc.text)
    assert all(chunk.token_count <= 320 and "�" not in chunk.text for chunk in chunks)
    assert all(left.end >= right.start for left, right in pairwise(chunks))


def test_chunk_fts_finds_exact_long_source_passage(tmp_path):
    doc = source(
        "# General\n\n"
        + "Routine record. " * 900
        + "\n\n## Rare mechanism\n\nQuartz narwhal is the lookup target."
    )
    bundle = ingest(manifest_for(tmp_path, [doc]))
    assert len(bundle.chunks) > 1
    db = sqlite3.connect(":memory:")
    try:
        db.deserialize(bundle.data)
        rows = db.execute("SELECT chunk_id FROM chunk_search WHERE chunk_search MATCH 'narwhal'").fetchall()
        matches = [chunk for chunk in bundle.chunks if chunk.chunk_id in {row[0] for row in rows}]
        assert len(matches) == 1 and "Quartz narwhal" in matches[0].text
        assert matches[0].section_path == ["General", "Rare mechanism"]
    finally:
        db.close()
    assert Bundle.from_bytes(bundle.data).chunks == bundle.chunks
    assert bundle.data == ingest(manifest_for(tmp_path, [doc])).data


@pytest.mark.parametrize("target", ["span", "text", "section", "fts", "source"])
def test_chunk_and_original_representation_tamper_is_rejected(tmp_path, target):
    bundle = ingest(manifest_for(tmp_path, [source("A valid exact original passage.")]))

    def change(db):
        if target == "fts":
            db.execute("UPDATE chunk_search SET text='Injected different knowledge'")
        elif target == "source":
            doc = bundle.documents[0].model_dump()
            doc["text"] = "Replaced original source."
            db.execute("UPDATE documents SET payload=?", (json.dumps(doc),))
        else:
            chunk = bundle.chunks[0].model_dump()
            chunk.update(
                {
                    "span": {"start": 1},
                    "text": {"text": "Forged span."},
                    "section": {"section_path": ["Forged heading"]},
                }[target]
            )
            db.execute("UPDATE chunks SET payload=?", (json.dumps(chunk),))

    with pytest.raises(IngestionError):
        Bundle.from_bytes(mutate(bundle, change))


def test_compact_embedding_space_validates_coverage_precision_and_tamper(tmp_path):
    bundle = ingest(manifest_for(tmp_path, [source("A complete source.")]))
    vectors = {key: [0.1, 0.2, 0.3] for key, _ in embedding_inputs(bundle)}
    embedded = with_embeddings(bundle, "fixture/space", vectors)
    assert embedded.config["embedding"]["unit"] == "chunk"
    assert embedded.config["embedding"]["format"] == "float32-le"
    assert next(iter(read_embeddings(embedded).values())) == pytest.approx([0.1, 0.2, 0.3])
    assert embedded.data == with_embeddings(bundle, "fixture/space", vectors).data
    with pytest.raises(IngestionError, match="integrity"):
        Bundle.from_bytes(
            mutate(embedded, lambda db: db.execute("UPDATE embeddings SET vector=?", (b"\x00" * 12,)))
        )
    with pytest.raises(IngestionError, match="every source"):
        with_embeddings(bundle, "fixture/space", {"DOC-001": [1.0]})
    with pytest.raises(IngestionError, match="float32"):
        with_embeddings(bundle, "fixture/space", {key: [1e100] for key in vectors})


@pytest.mark.asyncio
async def test_historical_whole_document_json_vector_bundle_remains_readable(tmp_path):
    bundle = ingest(manifest_for(tmp_path, [source("A historical source.")]))
    vectors = {"DOC-001": [1.0, 2.0, 3.0]}
    config = {key: value for key, value in bundle.config.items() if key != "chunker"}
    config["index_version"] = "whole-document-fts5-v2"
    config["embedding"] = {
        "model": "historic/space",
        "dimensions": 3,
        "input": "title-newline-text-v1",
        "vectors_sha256": sha256(canonical(vectors)).hexdigest(),
    }
    identity = sha256(canonical({"corpus_checksum": bundle.corpus_checksum, "config": config})).hexdigest()[
        :24
    ]

    def legacy(db):
        db.execute("DROP TABLE chunks")
        db.execute("DROP TABLE chunk_search")
        db.execute("CREATE TABLE embeddings(doc_id TEXT PRIMARY KEY,vector TEXT NOT NULL)")
        db.execute("INSERT INTO embeddings VALUES (?,?)", ("DOC-001", json.dumps(vectors["DOC-001"])))
        db.execute("UPDATE metadata SET value=? WHERE key='id'", (identity,))
        db.execute("UPDATE metadata SET value=? WHERE key='config'", (canonical(config).decode(),))

    restored = Bundle.from_bytes(mutate(bundle, legacy))
    assert restored.chunks == []
    assert read_embeddings(restored) == vectors
    assert embedding_inputs(restored) == [("DOC-001", "Source of the examples\nA historical source.")]
    provider = Embeddings()
    migrated = await build_embeddings(bundle, "historic/space", provider, previous=restored)
    assert not provider.calls
    assert read_embeddings(migrated)[bundle.chunks[0].chunk_id] == [1.0, 2.0, 3.0]


class Embeddings:
    def __init__(self):
        self.calls = []

    async def embed(self, texts, model):
        self.calls.append(texts)
        return SimpleNamespace(
            model=model,
            vectors=[[1.0, float(len(text))] for text in texts],
            usage=SimpleNamespace(model_dump=lambda: {"total_tokens": 12}),
            rate_limit_retries=0,
        )


@pytest.mark.asyncio
async def test_multi_batch_corpus_reuses_only_identical_inputs(tmp_path):
    documents = [
        source(f"Reference {i} preserves a source-specific observation.", f"DOC-{i:03}") for i in range(137)
    ]
    bundle = ingest(manifest_for(tmp_path, documents))
    provider = Embeddings()
    receipts = []
    first = await build_embeddings(bundle, "fixture/space", provider, receipts=receipts)
    assert [len(batch) for batch in provider.calls] == [64, 64, 9]
    assert len(receipts) == 3
    reused_provider = Embeddings()
    second = await build_embeddings(bundle, "fixture/space", reused_provider, previous=first)
    assert second.data == first.data and not reused_provider.calls
    # A title change changes the embedding input even though source span identity stays the same.
    documents[0] = documents[0].model_copy(update={"title": "Revised title"})
    changed = ingest(manifest_for(tmp_path, documents))
    await build_embeddings(changed, "fixture/space", reused_provider, previous=first)
    assert list(map(len, reused_provider.calls)) == [1]
    other_provider = Embeddings()
    await build_embeddings(changed, "different/space", other_provider, previous=first)
    assert sum(map(len, other_provider.calls)) == 137


@pytest.mark.asyncio
async def test_embedding_batches_honor_bytes_and_fail_before_activation(tmp_path):
    documents = [source("A brief span.", f"DOC-{i:03}", title="漢字" * 1700) for i in range(30)]
    bundle = ingest(manifest_for(tmp_path, documents))
    provider = Embeddings()
    await build_embeddings(bundle, "fixture/space", provider)
    assert len(provider.calls) == 2
    assert all(sum(len(text.encode()) for text in batch) <= 180_000 for batch in provider.calls)

    class Broken(Embeddings):
        async def embed(self, texts, model):
            result = await super().embed(texts, model)
            result.model = "unexpected/space"
            return result

    with pytest.raises(IngestionError, match="changed model"):
        await build_embeddings(bundle, "fixture/space", Broken())


def test_bundle_memory_cache_is_bounded(monkeypatch, tmp_path):
    from emer.storage import bundles

    bundle = ingest(manifest_for(tmp_path, [source("A source.")]))
    monkeypatch.setattr(bundles, "_cache", {})
    monkeypatch.setattr(bundles, "CACHE_MAX_ENTRIES", 2)
    for i in range(4):
        bundles._remember(replace(bundle, id=str(i)))
    assert list(bundles._cache) == ["2", "3"]
    monkeypatch.setattr(bundles, "CACHE_MAX_BYTES", 1)
    bundles._remember(bundle)
    assert not bundles._cache


def test_schema_two_catalog_is_validated_and_included_in_index_identity(tmp_path):
    documents = [source("An approved record.")]
    path = manifest_for(tmp_path, documents)
    manifest = json.loads(path.read_text())
    manifest.pop("policies")
    manifest.update(schema_version=2, source_metadata=[], authority={})
    path.write_text(json.dumps(manifest))
    bundle = ingest(path)
    assert bundle.config["source_catalog"]["sources"][0]["logical_id"] == "DOC-001"
    assert bundle.config["source_catalog"]["sources"][0]["date_semantics"] == "publication"
    manifest["source_metadata"] = [{"doc_id": "NONEXISTENT"}]
    path.write_text(json.dumps(manifest))
    with pytest.raises(IngestionError, match="unknown source"):
        ingest(path)
