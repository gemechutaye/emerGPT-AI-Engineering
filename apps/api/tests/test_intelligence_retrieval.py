"""Source-derived retrieval/integrity checks; these are deterministic regressions, not a holdout."""

import json
import sqlite3
from hashlib import sha256
from pathlib import Path

import pytest
from emer.services.ingestion import Bundle, IngestionError, ingest
from emer.services.retrieval import RetrievalService

ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture(scope="module")
def bundle():
    return ingest(ROOT / "config/corpus.json")


def _mutate(bundle, statement, params=()):
    connection = sqlite3.connect(":memory:")
    try:
        connection.deserialize(bundle.data)
        connection.execute(statement, params)
        connection.commit()
        return connection.serialize()
    finally:
        connection.close()


def test_every_original_record_and_metadata_survives_whole_document_chunking(bundle):
    records = [
        json.loads(line)
        for line in (ROOT / "EMER_AI_TakeHome_Knowledge_Corpus.jsonl").read_text().splitlines()
    ]
    documents = {doc.doc_id: doc for doc in bundle.documents}
    assert len(documents) == len(records) == 35
    assert len({doc.sha256 for doc in documents.values()}) == 35
    for record in records:
        source = documents[record["doc_id"]]
        assert source.model_dump(exclude={"sha256"}) == record
        assert source.sha256 == sha256(record["text"].encode()).hexdigest()
        for paragraph in record["text"].split("\n\n"):
            start = source.text.index(paragraph)
            assert source.text[start : start + len(paragraph)] == paragraph


def test_all_source_codes_are_searchable_at_rank_one(bundle):
    retriever = RetrievalService(bundle)
    failures = {}
    for source in bundle.documents:
        ranking = retriever.lexical(source.doc_id)
        if not ranking or ranking[0][0] != source.doc_id:
            failures[source.doc_id] = ranking[:3]
    assert failures == {}


def test_all_exact_source_titles_are_searchable_at_rank_one(bundle):
    retriever = RetrievalService(bundle)
    assert all(retriever.lexical(source.title)[0][0] == source.doc_id for source in bundle.documents)


def test_multiple_document_identifiers_are_ranked_and_traced(bundle):
    packet = RetrievalService(bundle).evidence("Compare BIZ-501 with IT-403", mode="lexical", top_k=2)
    assert set(packet.retrieval["raw_top_k_ids"]) == {"BIZ-501", "IT-403"}
    assert set(packet.retrieval["explicit_source_ids"]) == {"BIZ-501", "IT-403"}


def test_source_identifier_after_many_words_is_preserved(bundle):
    question = " ".join(f"unrelatedword{i}" for i in range(100)) + " Explain CARE-201"
    assert RetrievalService(bundle).lexical(question)[0][0] == "CARE-201"


@pytest.mark.parametrize(
    "word,expected", [("cancel", {"OPS-306-V1", "OPS-306-V2"}), ("photograph", {"PROC-105", "PROC-108"})]
)
def test_lexical_inflections_retrieve_source_words(bundle, word, expected):
    ranked = RetrievalService(bundle).lexical(word, 35)
    assert expected <= {identifier for identifier, _ in ranked}


def test_grammatical_only_query_has_no_ranked_evidence(bundle):
    packet = RetrievalService(bundle).evidence("the what where is", mode="lexical", top_k=3)
    assert packet.retrieval["raw_ranked_ids"] == []
    assert packet.sources == []


def test_possessive_fragment_does_not_change_lexical_ranking(bundle):
    retriever = RetrievalService(bundle)
    assert retriever.lexical("patient’s skincare") == retriever.lexical("patient skincare")
    assert retriever.lexical("PROC") == []


def test_orientation_question_not_displaced_by_code_prefix_or_possessive(bundle):
    # Fresh IH-001 was consumed when this index/tokenization regression was diagnosed.
    question = (
        "Give a two-sentence orientation: where is this fictional practice, "
        "and how does it keep a patient’s care connected over time?"
    )
    assert "PRACTICE-001" in {identifier for identifier, _ in RetrievalService(bundle).lexical(question, 3)}


def test_unrelated_policy_versions_not_forced_into_small_ranked_packet(bundle):
    packet = RetrievalService(bundle).evidence("Explain PROC-106", mode="lexical", top_k=1)
    assert [source.doc_id for source in packet.sources] == ["PROC-106"]
    assert packet.retrieval["policy_family_expansion"] == []


@pytest.mark.parametrize(
    "as_of,applicable", [("2025-12-31", []), ("2026-03-01", ["OPS-306-V1"]), ("2026-09-01", ["OPS-306-V2"])]
)
def test_ranked_policy_family_keeps_versions_and_applicability(bundle, as_of, applicable):
    packet = RetrievalService(bundle).evidence("Cancellation", mode="lexical", top_k=1, as_of=as_of)
    assert {source.doc_id for source in packet.sources} == {"OPS-306-V1", "OPS-306-V2"}
    assert packet.scopes[0].applicable_policy_ids == applicable
    assert packet.scopes[0].allowed_source_ids == applicable


def test_scope_filtered_ranking_and_mandatory_records_are_separate(bundle):
    packet = RetrievalService(bundle).evidence(
        "Body contouring", patient_id="PT-005", mode="lexical", top_k=1
    )
    details = packet.retrieval["scoped_selection"][0]
    assert details["ranked_selected_ids"] == ["PROC-108"]
    assert details["patient_expansion_ids"] == ["PT-005"]
    assert set(details["selected_ids"]) == {"PROC-108", "PT-005"}
    assert {doc.doc_id for doc in packet.sources if doc.category == "synthetic_patient"} == {"PT-005"}


@pytest.mark.parametrize("top_k", [0, -1, True, 1.5])
def test_invalid_top_k_rejected_before_search(bundle, top_k):
    with pytest.raises(ValueError, match="top_k"):
        RetrievalService(bundle).evidence("anything", top_k=top_k)


@pytest.mark.parametrize(
    "sql,params",
    [
        ("UPDATE search SET text=? WHERE doc_id=?", ("Unrelated source text", "PROC-105")),
        ("UPDATE search SET title=? WHERE doc_id=?", ("Wrong source title", "PROC-105")),
        ("DELETE FROM search WHERE doc_id=?", ("PROC-105",)),
        ("INSERT INTO search SELECT * FROM search WHERE doc_id=?", ("PROC-105",)),
        ("UPDATE documents SET doc_id=? WHERE doc_id=?", ("WRONG-105", "PROC-105")),
        ("UPDATE metadata SET value=? WHERE key='id'", ("wrong-index-id",)),
    ],
)
def test_restore_rejects_detached_search_or_index_identity(bundle, sql, params):
    with pytest.raises(IngestionError):
        Bundle.from_bytes(_mutate(bundle, sql, params))


def test_legacy_bundle_remains_readable_for_historical_citations():
    path = ROOT / "apps/api/tests/fixtures/recorded-retrieval/legacy-large.sqlite"
    if not path.exists():
        pytest.skip("Optional previously recorded real embedding fixture is absent")
    restored = Bundle.from_bytes(path.read_bytes())
    assert restored.id == "22f19d30c57328610866ea43"
    assert len(restored.documents) == 35
    assert RetrievalService(restored).lexical("PROC-105")[0][0] == "PROC-105"


@pytest.fixture
async def isolated_bundle_store(monkeypatch, tmp_path):
    """Use actual local Postgres, with no dependency on the serving application's database."""
    from uuid import uuid4

    import psycopg
    from emer.settings import settings
    from emer.storage import bundles
    from emer.storage.models import Base
    from psycopg import sql
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    name = "emer_retrieval_audit_" + uuid4().hex[:12]
    admin = "postgresql://localhost:55432/postgres"
    with psycopg.connect(admin, autocommit=True) as db:
        db.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    engine = create_async_engine(f"postgresql+psycopg://localhost:55432/{name}")
    try:
        async with engine.begin() as db:
            await db.run_sync(Base.metadata.create_all)
        monkeypatch.setattr(bundles, "Session", async_sessionmaker(engine, expire_on_commit=False))
        monkeypatch.setattr(bundles, "_cache", {})
        monkeypatch.setattr(settings, "cache_dir", str(tmp_path))
        yield bundles, tmp_path
    finally:
        await engine.dispose()
        with psycopg.connect(admin, autocommit=True) as db:
            db.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


async def test_real_postgres_bundle_restore_ignores_missing_or_corrupt_disk_cache(
    bundle, isolated_bundle_store
):
    store, cache_dir = isolated_bundle_store
    await store.store_and_activate(bundle)
    store._cache.clear()
    restored = await store.load_bundle()
    assert restored.data == bundle.data
    cache_path = cache_dir / (bundle.checksum + ".sqlite")
    assert cache_path.read_bytes() == bundle.data
    for corrupt in (None, b"malformed disposable cache"):
        if corrupt is None:
            cache_path.unlink()
        else:
            cache_path.write_bytes(corrupt)
        store._cache.clear()
        restored = await store.load_bundle(bundle.id)
        assert restored.documents == bundle.documents
        assert cache_path.read_bytes() == bundle.data
        assert RetrievalService(restored).lexical("PROC-105")[0][0] == "PROC-105"


async def test_real_postgres_rejects_index_collision_and_detached_metadata(bundle, isolated_bundle_store):
    from dataclasses import replace

    from emer.storage.models import ActiveIndex, CorpusIndex

    store, _ = isolated_bundle_store
    await store.store_and_activate(bundle)
    with pytest.raises(IngestionError, match="different retrieval bundle"):
        await store.store_and_activate(replace(bundle, checksum="wrong-checksum"))
    async with store.Session() as db:
        assert (await db.get(ActiveIndex, 1)).index_id == bundle.id
        assert (await db.get(CorpusIndex, bundle.id)).checksum == bundle.checksum
    async with store.Session.begin() as db:
        row = await db.get(CorpusIndex, bundle.id)
        row.manifest = {**bundle.config, "index_version": "wrong-version"}
    store._cache.clear()
    with pytest.raises(IngestionError, match="metadata differs"):
        await store.load_bundle(bundle.id)
