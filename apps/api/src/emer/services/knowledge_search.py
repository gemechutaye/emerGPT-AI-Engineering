"""Per-request hybrid chunk search and deterministic, token-bounded evidence assembly."""

from __future__ import annotations

import json
import sqlite3
import threading
from collections import OrderedDict
from dataclasses import dataclass
from time import perf_counter

import numpy as np
import tiktoken
from emer.contracts.answer import EvidencePacket, EvidencePassage
from emer.contracts.knowledge import QuestionPlan
from emer.domain.policy import apply_policies
from emer.providers.reranker import get_reranker
from emer.services.ingestion import IngestionError, read_embeddings
from emer.services.retrieval import exact_title_source_ids, explicit_source_ids, reciprocal_rank_fusion

ENCODING = tiktoken.get_encoding("cl100k_base")
_indexes: OrderedDict[str, ChunkIndex] = OrderedDict()
_cache_lock = threading.Lock()
_MAX_INDEX_CACHE_BYTES = 128 * 1024 * 1024


def tokens(value) -> int:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return len(ENCODING.encode(text, disallowed_special=()))


class ChunkIndex:
    def __init__(self, bundle):
        if not bundle.chunks:
            raise IngestionError("This index has no source chunks; rebuild with the current ingest command.")
        self.bundle = bundle
        self.documents = {doc.doc_id: doc for doc in bundle.documents}
        self.chunks = {chunk.chunk_id: chunk for chunk in bundle.chunks}
        self.doc_chunks = {doc_id: [] for doc_id in self.documents}
        for chunk in bundle.chunks:
            self.doc_chunks[chunk.doc_id].append(chunk)
        self.family_docs = {}
        rows = (bundle.config.get("source_catalog") or {}).get("sources", bundle.config.get("policies", []))
        for row in rows:
            self.family_docs.setdefault(row["family"], set()).add(row["doc_id"])
        self.ids = sorted(self.chunks)
        vectors = read_embeddings(bundle, compact=True) if bundle.config.get("embedding") else {}
        self.matrix = None
        if vectors:
            if bundle.config["embedding"].get("unit") != "chunk" or set(vectors) != set(self.ids):
                raise IngestionError("Current retrieval requires one vector per source chunk.")
            matrix = np.asarray([vectors[key] for key in self.ids], dtype=np.float32)
            matrix /= np.linalg.norm(matrix, axis=1)[:, None]
            self.matrix = matrix
            self.matrix.flags.writeable = False
        self.connection = sqlite3.connect(":memory:", check_same_thread=False)
        self.connection.deserialize(bundle.data)
        self.connection.execute("PRAGMA query_only=ON")
        self.lock = threading.Lock()
        self.size = len(bundle.data) + (self.matrix.nbytes if self.matrix is not None else 0)

    def lexical(self, query: str) -> list[tuple[str, float]]:
        import re

        from emer.domain.scope import canonicalize_patient_mentions
        from emer.services.retrieval import _STOPWORDS

        words = list(dict.fromkeys(
            word.lower() for word in re.findall(r"\w+", canonicalize_patient_mentions(query))
            if word.lower() not in _STOPWORDS and (len(word) > 1 or word.isdigit())
        ))[:100]
        if not words:
            return []
        expression = " OR ".join('"' + word.replace('"', '""') + '"' for word in words)
        with self.lock:
            return [(key, -score) for key, score in self.connection.execute(
                "SELECT chunk_id,bm25(chunk_search,0,0,4,2,1) AS score "
                "FROM chunk_search WHERE chunk_search MATCH ? ORDER BY score,chunk_id", (expression,),
            )]

    def semantic(self, vector: list[float], model: str) -> list[tuple[str, float]]:
        metadata = self.bundle.config.get("embedding") or {}
        if self.matrix is None or model != metadata.get("model"):
            raise IngestionError("Query and chunk index must use the same embedding space.")
        query = np.asarray(vector, dtype=np.float32)
        if query.shape != (self.matrix.shape[1],) or not np.isfinite(query).all() or not np.linalg.norm(query):
            raise IngestionError("Invalid query embedding dimensions or values.")
        scores = self.matrix @ (query / np.linalg.norm(query))
        return sorted(zip(self.ids, map(float, scores), strict=True), key=lambda row: (-row[1], row[0]))


def get_chunk_index(bundle) -> ChunkIndex:
    with _cache_lock:
        if bundle.checksum in _indexes:
            _indexes.move_to_end(bundle.checksum)
            return _indexes[bundle.checksum]
        index = ChunkIndex(bundle)
        # Eviction releases cache ownership only; in-flight requests retain their index.
        while _indexes and (len(_indexes) >= 2 or sum(item.size for item in _indexes.values()) + index.size > _MAX_INDEX_CACHE_BYTES):
            _indexes.popitem(last=False)
        if index.size <= _MAX_INDEX_CACHE_BYTES:
            _indexes[bundle.checksum] = index
        return index


@dataclass
class PartCandidates:
    scope: object
    ranked: list[str]
    mandatory: list[str]
    diagnostics: dict


class KnowledgeSearch:
    def __init__(self, bundle, *, reranker=None):
        self.index = get_chunk_index(bundle)
        self.bundle = bundle
        self.reranker = reranker

    def candidates(self, part, vector, model, mode, candidate_k, top_k) -> PartCandidates:
        scope = part.scope
        policies = self.bundle.config.get("policies", [])
        catalog = self.bundle.config.get("source_catalog")
        if catalog:
            from emer.domain.policy import apply_source_metadata
            scope = apply_source_metadata(scope, catalog)
        else:
            scope = apply_policies(scope, policies)
        permitted = {
            doc.doc_id for doc in self.bundle.documents
            if doc.category != "synthetic_patient" or doc.doc_id in scope.patient_ids
        }
        lexical = self.index.lexical(part.query)
        semantic = self.index.semantic(vector, model) if mode in {"hybrid", "semantic"} else []
        ranked = reciprocal_rank_fusion(lexical, semantic) if mode == "hybrid" else semantic if mode == "semantic" else lexical
        ranked = [(key, value) for key, value in ranked if self.index.chunks[key].doc_id in permitted]
        direct = explicit_source_ids(part.query, self.index.documents) + exact_title_source_ids(part.query, self.index.documents.values())
        mandatory_docs = set(direct) & permitted
        if not scope.patient_discovery:
            mandatory_docs.update(scope.patient_ids)
        keys = [key for key, _ in ranked[:candidate_k]]
        rank_positions = {key: position for position, (key, _) in enumerate(ranked)}
        direct_candidates = [chunk.chunk_id for doc_id in sorted(mandatory_docs) for chunk in sorted(
            self.index.doc_chunks[doc_id], key=lambda item: (rank_positions.get(item.chunk_id, len(ranked)), item.ordinal)
        )[:8]]
        mandatory = direct_candidates
        selected_docs = {self.index.chunks[key].doc_id for key in keys + mandatory}
        if catalog:
            rows = catalog["sources"]
            families = {row["family"] for row in rows if row["doc_id"] in selected_docs}
            family_docs = {doc_id for family in families if len(self.index.family_docs[family]) > 1
                           for doc_id in self.index.family_docs[family]} & permitted
        else:
            families = {row["family"] for row in policies if row["doc_id"] in selected_docs}
            family_docs = {row["doc_id"] for row in policies if row["family"] in families}
        # Resolve applicability before publication. Retain source inspection alternatives,
        # while excluded revisions are recorded in metadata without using their terms.
        inactive = set(scope.inapplicable_source_ids or scope.inapplicable_policy_ids) - set(scope.source_reference_ids)
        allowed = permitted - inactive
        scope.allowed_source_ids = sorted(allowed)
        family_keys = [min(self.index.doc_chunks[doc_id], key=lambda chunk: (
            rank_positions.get(chunk.chunk_id, len(ranked)), chunk.ordinal
        )).chunk_id for doc_id in sorted(family_docs & allowed)]
        keys = list(dict.fromkeys(mandatory + family_keys + keys))
        keys = [key for key in keys if self.index.chunks[key].doc_id in allowed]
        # A wide explicit request cannot silently blow through the bounded reranker.
        rerank_keys = keys[:96]
        logits = {}
        if self.reranker is not None and rerank_keys:
            texts = [self.index.documents[self.index.chunks[key].doc_id].title + "\n" + self.index.chunks[key].text for key in rerank_keys]
            values = self.reranker.score(part.query, texts)
            logits = dict(zip(rerank_keys, values, strict=True))
            order = sorted(rerank_keys, key=lambda key: (-logits[key], keys.index(key)))
        else:
            order = rerank_keys
        # Exact addressing guarantees source representation, not leading-page priority.
        # Select the best reranked passage per required source; remaining passages
        # compete normally. This cannot let a long document consume the entire packet.
        required_docs = (mandatory_docs | family_docs) & allowed
        mandatory = [next(key for key in order if self.index.chunks[key].doc_id == doc_id)
                     for doc_id in sorted(required_docs) if any(self.index.chunks[key].doc_id == doc_id for key in order)]
        chosen = list(dict.fromkeys(mandatory + order[:top_k]))
        relevant_docs = selected_docs | family_docs | mandatory_docs
        scope.source_windows = [row for row in scope.source_windows if row["doc_id"] in relevant_docs]
        scope.applicable_source_ids = [key for key in scope.applicable_source_ids if key in relevant_docs]
        scope.inapplicable_source_ids = [key for key in scope.inapplicable_source_ids if key in relevant_docs]
        scope.conflicting_source_ids = [key for key in scope.conflicting_source_ids if key in relevant_docs]
        if not scope.conflicting_source_ids:
            scope.warnings = [warning for warning in scope.warnings if not warning.startswith("Competing source revisions")]
        return PartCandidates(scope, chosen, mandatory, {
            "part_id": part.part_id, "query": part.query, "requested_information": part.requested_information,
            "raw_ranked_chunk_ids": [key for key, _ in ranked[:candidate_k]],
            "raw_ranked_ids": list(dict.fromkeys(self.index.chunks[key].doc_id for key, _ in ranked[:candidate_k])),
            "lexical_ranked_chunk_ids": [key for key, _ in lexical[:candidate_k]],
            "semantic_ranked_chunk_ids": [key for key, _ in semantic[:candidate_k]],
            "reranked_chunk_ids": order, "rerank_scores": logits,
            "mandatory_chunk_ids": mandatory, "candidate_count": len(rerank_keys),
            "candidate_overflow": len(keys) > 96,
        })

    def search(self, plan: QuestionPlan, *, vectors=None, model=None, mode="hybrid", candidate_k=32, top_k=8, token_budget=6000, protected_passages=()) -> EvidencePacket:
        if mode not in {"lexical", "semantic", "hybrid"}:
            raise ValueError("Production search requires an explicit ranked retrieval mode.")
        started = perf_counter()
        vectors = vectors or {}
        candidates = [self.candidates(part, vectors.get(part.part_id), model, mode, candidate_k, top_k) for part in plan.parts]
        chosen: dict[str, EvidencePassage] = {}
        scopes = [part.scope.model_copy(deep=True) for part in candidates]

        def payload_cost(selected):
            # Exactly count serialized source metadata and evidence passages; source bodies
            # are never sent in addition to these excerpts.
            doc_ids = {item.doc_id for item in selected.values()}
            return tokens({
                "sources": [self.index.documents[key].model_dump(exclude={"text"}) for key in sorted(doc_ids)],
                "passages": [item.model_dump(mode="json") for item in selected.values()],
            })

        part_permissions = {part.scope.part_id: set(part.scope.allowed_source_ids) for part in candidates}
        for passage in protected_passages:
            original = self.index.chunks.get(passage.chunk_id)
            if original is None or original.model_dump() != passage.model_dump(exclude={"scope_ids"}):
                raise IngestionError("Protected evidence does not match the pinned chunk index.")
            allowed_scopes = [part_id for part_id in passage.scope_ids
                              if passage.doc_id in part_permissions.get(part_id, set())]
            if allowed_scopes:
                chosen[passage.chunk_id] = passage.model_copy(update={"scope_ids": allowed_scopes})
        if payload_cost(chosen) > token_budget:
            raise IngestionError("Previously supported evidence exceeds the context budget.")

        # Round-robin allocation prevents one compound part consuming the whole budget.
        for position in range(max((len(part.ranked) for part in candidates), default=0)):
            for part in candidates:
                if position >= len(part.ranked):
                    continue
                key = part.ranked[position]
                previous = chosen.get(key)
                ids = list(dict.fromkeys([*(previous.scope_ids if previous else []), part.scope.part_id]))
                passage = EvidencePassage(**self.index.chunks[key].model_dump(), scope_ids=ids)
                proposed = {**chosen, key: passage}
                if payload_cost(proposed) <= token_budget:
                    chosen = proposed
        for scope, part in zip(scopes, candidates, strict=True):
            included = [item for item in chosen.values() if scope.part_id in item.scope_ids]
            scope.allowed_source_ids = sorted({item.doc_id for item in included})
            # Search candidates are diagnostic. Only selected sources and their related
            # revisions contribute metadata to the model's evidence packet.
            logical_ids = {row["logical_id"] for row in scope.source_windows
                           if row["doc_id"] in scope.allowed_source_ids}
            scope.source_windows = [row for row in scope.source_windows if row["logical_id"] in logical_ids]
            relevant_ids = {row["doc_id"] for row in scope.source_windows}
            for field in ("applicable_source_ids", "inapplicable_source_ids", "conflicting_source_ids"):
                setattr(scope, field, [key for key in getattr(scope, field) if key in relevant_ids])
            if scope.patient_discovery:
                scope.patient_ids = [key for key in scope.patient_ids if key in scope.allowed_source_ids]
            omitted = [key for key in part.ranked if key not in chosen or scope.part_id not in chosen[key].scope_ids]
            part.diagnostics.update(
                selected_chunk_ids=[item.chunk_id for item in included], selected_ids=scope.allowed_source_ids,
                allowed_source_ids=scope.allowed_source_ids, budget_omitted_chunk_ids=omitted,
                complete_source_ids=[key for key in scope.allowed_source_ids if all(
                    chunk.chunk_id in {item.chunk_id for item in included} for chunk in self.index.doc_chunks[key]
                )],
            )
            if omitted:
                scope.warnings.append("The evidence budget omitted candidate passages; source absence is not established.")
        sources = [self.index.documents[key] for key in sorted({item.doc_id for item in chosen.values()})]
        first = candidates[0].diagnostics
        return EvidencePacket(
            index_id=self.bundle.id, index_checksum=self.bundle.checksum, corpus_checksum=self.bundle.corpus_checksum,
            sources=sources, scopes=scopes, passages=list(chosen.values()),
            retrieval={
                "mode": mode, "mode_requested": mode, "unit": "source_chunk", "top_k": top_k,
                "candidate_k": candidate_k, "selected_count": len(sources), "selected_chunk_count": len(chosen),
                "corpus_count": len(self.bundle.documents), "corpus_chunk_count": len(self.bundle.chunks),
                "raw_ranked_ids": first["raw_ranked_ids"], "raw_top_k_ids": first["raw_ranked_ids"][:top_k],
                "scoped_selection": [item.diagnostics for item in candidates],
                "context_tokens": payload_cost(chosen), "context_token_budget": token_budget,
                "token_encoding": "cl100k_base", "reranker": self.reranker.identity if self.reranker else None,
                "embedding_model": model, "latency_ms": round((perf_counter() - started) * 1000, 3),
                "score_meaning": "Cross-encoder relevance logits and rank signals; not answer probability",
            },
        )


async def retrieve_plan(bundle, provider, plan, settings, *, expanded=False, protected_passages=(), embedding_cache=None):
    import asyncio

    mode = settings.retrieval_mode
    metadata = bundle.config.get("embedding")
    vectors, model = {}, None
    embedding_cache = {} if embedding_cache is None else embedding_cache
    if mode in {"hybrid", "semantic"}:
        if not metadata or metadata.get("unit") != "chunk":
            raise IngestionError("Ranked chunk embeddings are not initialized. Rebuild the knowledge index.")
        model = metadata["model"]
        missing = list(dict.fromkeys(part.query for part in plan.parts if (model, part.query) not in embedding_cache))
        if missing:
            response = await provider.embed(missing, model)
            if response.model != model or len(response.vectors) != len(missing):
                raise IngestionError("Query embedding response changed model or query count.")
            embedding_cache.update({(model, query): vector for query, vector in zip(missing, response.vectors, strict=True)})
        vectors = {part.part_id: embedding_cache[(model, part.query)] for part in plan.parts}
    reranker = await asyncio.to_thread(get_reranker, settings.retrieval_model_cache) if settings.reranking_enabled else None
    search = await asyncio.to_thread(KnowledgeSearch, bundle, reranker=reranker)
    return await asyncio.to_thread(
        search.search, plan, vectors=vectors, model=model, mode=mode,
        candidate_k=min(96, settings.retrieval_candidate_k * (2 if expanded else 1)),
        top_k=min(24, settings.retrieval_top_k * (2 if expanded else 1)),
        token_budget=settings.context_token_budget,
        protected_passages=protected_passages,
    )
