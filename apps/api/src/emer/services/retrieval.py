"""Whole-document retrieval with independently resolved patient/date permissions."""

from __future__ import annotations

import re
import sqlite3
import time
from hashlib import sha256

import numpy as np
from emer.contracts.answer import EvidencePacket
from emer.domain.policy import apply_policies
from emer.domain.scope import canonicalize_patient_mentions, resolve_scopes
from emer.services.ingestion import Bundle, IngestionError, canonical, read_embeddings


def explicit_source_ids(question: str, identifiers) -> list[str]:
    """Direct source addresses match catalog identifiers, never inferred answer content."""
    normalized = canonicalize_patient_mentions(question)
    return [identifier for identifier in identifiers
            if re.search(r"(?<![\w-])" + re.escape(identifier) + r"(?![\w-])", normalized, re.IGNORECASE)]


def exact_title_source_ids(question: str, documents) -> list[str]:
    normalized = " " + " ".join(re.findall(r"\w+", question.lower())) + " "
    return [doc.doc_id for doc in documents
            if " " + " ".join(re.findall(r"\w+", doc.title.lower())) + " " in normalized]


# Grammatical words add BM25 noise but no topic evidence. Preserve negation, quantities,
# dates, patient identifiers and domain terms; ranking is never answerability confidence.
_STOPWORDS = frozenset(
    [
        "a",
        "an",
        "the",
        "and",
        "or",
        "of",
        "to",
        "for",
        "in",
        "on",
        "at",
        "by",
        "from",
        "with",
        "as",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "being",
        "do",
        "does",
        "did",
        "have",
        "has",
        "had",
        "it",
        "its",
        "this",
        "that",
        "these",
        "those",
        "i",
        "me",
        "my",
        "we",
        "us",
        "our",
        "you",
        "your",
        "he",
        "him",
        "his",
        "she",
        "her",
        "they",
        "them",
        "their",
        "what",
        "which",
        "who",
        "whom",
        "whose",
        "where",
        "when",
        "why",
        "how",
        "can",
        "could",
        "would",
        "should",
        "will",
        "shall",
        "may",
        "might",
        "please",
        "tell",
        "about",
    ]
)

_PROCEDURE_NOUN = r"(?:procedures?|treatments?|therap(?:y|ies)|modalit(?:y|ies))"
_PROCEDURE_REFERENCE = re.compile(
    rf"\b(?:that|this|their|his|her|its|same)\s+(?:\w+\s+)?{_PROCEDURE_NOUN}\b"
    rf"|\b{_PROCEDURE_NOUN}\b.{{0,100}}\b(?:discussed|reviewed|mentioned|documented|recorded)\b"
    rf"|\b(?:discussed|reviewed|mentioned|documented|recorded)\s+(?:\w+\s+)?{_PROCEDURE_NOUN}\b",
    re.IGNORECASE,
)
_TOPIC_RESET = re.compile(
    r"\b(?:separate\s+(?:issue|question)|new\s+topic|unrelated\s+question|in\s+general|generally)\b",
    re.IGNORECASE,
)
_GENERIC_TITLE_WORDS = _STOPWORDS | {
    "general",
    "overview",
    "guide",
    "reference",
    "education",
    "information",
    "procedure",
    "treatment",
    "therapy",
    "service",
    "consultation",
    "program",
    "option",
    "skin",
    "body",
    "injectable",
    "laser",
    "filler",
}

# Semantic similarity is less sensitive to repeated wording in compound questions.
# Keep lexical evidence in the fusion and exact source addresses above ranking.
_RRF_RANK_CONSTANT = 60
_RRF_LEXICAL_WEIGHT = 1
_RRF_SEMANTIC_WEIGHT = 3


def reciprocal_rank_fusion(
    lexical: list[tuple[str, float]], semantic: list[tuple[str, float]]
) -> list[tuple[str, float]]:
    """Fuse incomparable BM25/cosine scores by rank, with a measured semantic preference."""
    scores: dict[str, float] = {}
    for weight, ranking in ((_RRF_LEXICAL_WEIGHT, lexical), (_RRF_SEMANTIC_WEIGHT, semantic)):
        for rank, (identifier, _) in enumerate(ranking, 1):
            scores[identifier] = scores.get(identifier, 0) + weight / (_RRF_RANK_CONSTANT + rank)
    return sorted(scores.items(), key=lambda row: (-row[1], row[0]))


def procedure_title_aliases(title: str) -> list[tuple[str, re.Pattern]]:
    """Use only names actually present in a title, never inferred clinical synonyms."""
    core = re.split(
        r"\s+[-–—]\s+(?:general\s+)?(?:overview|guide|reference|education)\b",
        title,
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0]
    alternatives = re.findall(r"\(([^()]+)\)", core)
    alternatives.append(re.sub(r"\([^()]*\)", "", core))
    aliases = {}
    for alternative in alternatives:
        for name in alternative.split("/"):
            words = re.findall(r"[^\W_]+", name, re.UNICODE)
            if not words or not any(word.lower() not in _GENERIC_TITLE_WORDS for word in words):
                continue
            # Short single words require an explicit title acronym (e.g. IPL), not a guess.
            if len(words) == 1 and not (
                len(words[0]) >= 6 or (words[0].isupper() and 3 <= len(words[0]) <= 10)
            ):
                continue
            alias = " ".join(words).lower()
            aliases[alias] = re.compile(
                r"(?<!\w)" + r"[\W_]+".join(map(re.escape, words)) + r"(?!\w)", re.IGNORECASE
            )
    return sorted(aliases.items())


class RetrievalService:
    def __init__(self, bundle: Bundle):
        self.bundle = bundle
        self.documents = {doc.doc_id: doc for doc in bundle.documents}
        self.procedure_aliases = [
            (doc.doc_id, alias, pattern)
            for doc in bundle.documents
            if doc.category == "procedure"
            for alias, pattern in procedure_title_aliases(doc.title)
        ]
        self.vector_ids: list[str] = []
        self.matrix = None
        if bundle.config.get("embedding"):
            vectors = read_embeddings(bundle)
            metadata = bundle.config["embedding"]
            chunk_space = metadata.get("unit") == "chunk"
            if chunk_space:
                # Compatibility document-ranking view for historical evaluation commands.
                # Production searches actual chunk vectors in KnowledgeSearch.
                vectors = {doc.doc_id: np.mean([
                    vectors[chunk.chunk_id] for chunk in bundle.chunks if chunk.doc_id == doc.doc_id
                ], axis=0).tolist() for doc in bundle.documents}
            if (
                set(vectors) != set(self.documents)
                or (not chunk_space and metadata.get("format") != "float32-le"
                    and sha256(canonical(vectors)).hexdigest() != metadata["vectors_sha256"])
            ):
                raise IngestionError("Embedding cache integrity failed")
            self.vector_ids = sorted(vectors)
            matrix = np.array([vectors[key] for key in self.vector_ids], dtype=np.float64)
            if matrix.shape != (len(self.documents), metadata["dimensions"]) or not np.isfinite(matrix).all():
                raise IngestionError("Embedding cache dimensions or numeric values are invalid")
            norms = np.linalg.norm(matrix, axis=1)
            if not np.all(norms):
                raise IngestionError("Embedding cache contains zero vectors")
            self.matrix = matrix / norms[:, None]
            self.matrix.flags.writeable = False

    def semantic(self, vector: list[float], model: str) -> list[tuple[str, float]]:
        metadata = self.bundle.config.get("embedding")
        if self.matrix is None or not metadata:
            raise ValueError("Semantic retrieval has no built index")
        if model != metadata["model"]:
            raise ValueError("Query and corpus embeddings belong to different model spaces")
        query = np.asarray(vector, dtype=np.float64)
        if (
            query.shape != (metadata["dimensions"],)
            or not np.isfinite(query).all()
            or not np.linalg.norm(query)
        ):
            raise ValueError("Query embedding dimensions or numeric values are invalid")
        scores = self.matrix @ (query / np.linalg.norm(query))
        return sorted(
            zip(self.vector_ids, map(float, scores), strict=True), key=lambda row: (-row[1], row[0])
        )

    def explicit_source_ids(self, question: str) -> list[str]:
        return explicit_source_ids(question, self.documents)

    def exact_title_source_ids(self, question: str) -> list[str]:
        return exact_title_source_ids(question, self.documents.values())

    def lexical(self, question: str, limit: int = 12) -> list[tuple[str, float]]:
        # Spoken identifiers ("patient seven") must match the record's PT-007 tokens.
        tokens = list(
            dict.fromkeys(
                token.lower()
                for token in re.findall(r"\w+", canonicalize_patient_mentions(question), re.UNICODE)
                if (len(token) > 1 or token.isdigit())
                and (token.lower() not in _STOPWORDS or token.isupper())
            )
        )[:80]
        explicit = list(
            dict.fromkeys(self.explicit_source_ids(question) + self.exact_title_source_ids(question))
        )
        tokens.extend(identifier for identifier in explicit if identifier not in tokens)
        if not tokens:
            return []
        # Every token is quoted; user FTS operators/quotes never become query syntax.
        # Bare document-code prefixes are not topic evidence (e.g. CARE in CARE-201).
        # Exact addresses use the metadata lookup above; general words search source content.
        expression = (
            "{title text} : (" + " OR ".join('"' + token.replace('"', '""') + '"' for token in tokens) + ")"
        )
        db = sqlite3.connect(":memory:")
        try:
            db.deserialize(self.bundle.data)
            db.execute("PRAGMA query_only=ON")
            ranked = [
                (row[0], -row[1])
                for row in db.execute(
                    "SELECT doc_id, bm25(search, 0, 4, 1) AS rank FROM search WHERE search MATCH ? ORDER BY rank, doc_id",
                    (expression,),
                )
            ]
            # IDs are looked up directly so legacy v1 bundles with UNINDEXED doc_id also work.
            highest = max((score for _, score in ranked), default=0)
            return (
                [(identifier, highest + 1) for identifier in explicit]
                + [(identifier, score) for identifier, score in ranked if identifier not in explicit]
            )[: min(max(limit, 1), len(self.documents))]
        finally:
            db.close()

    def procedure_references(self, question: str, patient_id: str) -> list[dict]:
        """Link a scoped source mention to educational sources, not a treatment decision."""
        if not _PROCEDURE_REFERENCE.search(question) or _TOPIC_RESET.search(question):
            return []
        source = self.documents[patient_id]
        if source.category != "synthetic_patient":
            return []
        matches = []
        for identifier, alias, pattern in self.procedure_aliases:
            match = pattern.search(source.text)
            if match:
                matches.append(
                    {
                        "doc_id": identifier,
                        "patient_doc_id": patient_id,
                        "alias": alias,
                        "start": match.start(),
                        "end": match.end(),
                        "quote": match.group(),
                    }
                )
        return matches

    def evidence(
        self,
        question: str,
        patient_id: str | None = None,
        as_of: str | None = None,
        mode: str = "lexical",
        top_k: int = 8,
        query_vector: list[float] | None = None,
        query_model: str | None = None,
        requested_mode: str | None = None,
    ) -> EvidencePacket:
        if mode not in {"full", "lexical", "semantic", "hybrid"}:
            raise ValueError("Retrieval mode has no built index: " + mode)
        if not isinstance(top_k, int) or isinstance(top_k, bool) or top_k < 1:
            raise ValueError("top_k must be a positive integer")
        started = time.perf_counter()
        lexical = self.lexical(question, len(self.documents))
        ranked = lexical
        semantic = []
        if mode in {"semantic", "hybrid"}:
            if self.matrix is None:
                raise ValueError("Semantic retrieval has no built index")
            if query_vector is None or query_model is None:
                raise ValueError("Semantic retrieval requires the query vector and its model identity")
            semantic = self.semantic(query_vector, query_model)
            if mode == "semantic":
                ranked = semantic
            else:
                ranked = reciprocal_rank_fusion(lexical, semantic)
        raw_ranked = ranked
        explicit = self.explicit_source_ids(question)
        title_matches = self.exact_title_source_ids(question)
        direct_matches = list(dict.fromkeys(explicit + title_matches))
        # A direct source address is metadata retrieval, not an embedding similarity guess.
        if direct_matches:
            highest = max((score for _, score in ranked), default=0)
            ranked = [(identifier, highest + 1) for identifier in direct_matches] + [
                (identifier, score) for identifier, score in ranked if identifier not in direct_matches
            ]
        scopes = resolve_scopes(question, self.bundle.documents, patient_id, as_of)
        policies = self.bundle.config["policies"]
        family_ids = {p["doc_id"] for p in policies}
        families = {p["doc_id"]: p["family"] for p in policies}
        selected: set[str] = set()
        resolved_scopes = []
        scoped_selection = []
        expanded_families: set[str] = set()
        for raw_scope in scopes:
            scope = apply_policies(raw_scope, policies)
            permitted = {
                doc.doc_id
                for doc in self.bundle.documents
                if doc.category != "synthetic_patient" or doc.doc_id in scope.patient_ids
            }
            permitted_ranked = [identifier for identifier, _ in ranked if identifier in permitted]
            ranked_selected = permitted_ranked[:top_k]
            patient_expansion: set[str] = set()
            policy_expansion: set[str] = set()
            procedure_expansion: set[str] = set()
            procedure_candidates: list[dict] = []
            procedure_limit_exceeded = False
            if mode != "full":
                # Keep mandatory patient records and complete policy families after measuring raw rank.
                chosen = set(ranked_selected)
                if scope.patient_discovery:
                    # Discovery lets ranking find the matching records instead of enrolling every patient.
                    scope = scope.model_copy(
                        update={"patient_ids": [p for p in scope.patient_ids if p in chosen]}
                    )
                else:
                    patient_expansion = set(scope.patient_ids) - chosen
                    chosen.update(scope.patient_ids)
                if (
                    not scope.patient_discovery
                    and not scope.all_patients
                    and not scope.unknown_patient_ids
                    and len(scope.patient_ids) == 1
                    and scope.patient_ids[0] in chosen
                ):
                    procedure_candidates = self.procedure_references(scope.question, scope.patient_ids[0])
                    referenced = {candidate["doc_id"] for candidate in procedure_candidates}
                    procedure_limit_exceeded = len(referenced) > 3
                    if not procedure_limit_exceeded:
                        procedure_expansion = referenced - chosen
                        chosen.update(referenced)
                    if referenced:
                        scope.warnings.append(
                            "Procedure documents linked from this record are educational context only; "
                            "a mention does not establish selection, completion, eligibility, or a recommendation."
                        )
                    if len(referenced) > 1:
                        scope.warnings.append(
                            "The record mentions multiple procedure topics; do not silently choose one "
                            "as the requested treatment. Clarify if the reference is ambiguous."
                        )
                    if procedure_limit_exceeded:
                        scope.warnings.append(
                            "More than three procedure sources match this record; related-source expansion "
                            "was withheld. Clarify the procedure rather than selecting an arbitrary subset."
                        )
                relevant_families = {families[identifier] for identifier in chosen & family_ids}
                relevant_versions = {p["doc_id"] for p in policies if p["family"] in relevant_families}
                policy_expansion = relevant_versions - chosen
                expanded_families.update(relevant_versions)
                chosen.update(relevant_versions)
            else:
                chosen = permitted
            selected.update(chosen)
            scope.allowed_source_ids = sorted(
                (chosen - family_ids)
                | (chosen & (set(scope.applicable_policy_ids) | set(scope.source_reference_ids)))
            )
            resolved_scopes.append(scope)
            scoped_selection.append(
                {
                    "part_id": scope.part_id,
                    "permitted_ranked_ids": permitted_ranked,
                    "ranked_selected_ids": ranked_selected if mode != "full" else [],
                    "patient_expansion_ids": sorted(patient_expansion),
                    "policy_expansion_ids": sorted(policy_expansion),
                    "procedure_reference_candidates": procedure_candidates,
                    "procedure_expansion_ids": sorted(procedure_expansion),
                    "procedure_expansion_limit_exceeded": procedure_limit_exceeded,
                    "selected_ids": sorted(chosen),
                    "allowed_source_ids": scope.allowed_source_ids,
                }
            )
        rank_order = {identifier: rank for rank, (identifier, _) in enumerate(ranked)}
        ordered = sorted(
            selected, key=lambda identifier: (rank_order.get(identifier, len(ranked)), identifier)
        )
        return EvidencePacket(
            index_id=self.bundle.id,
            index_checksum=self.bundle.checksum,
            corpus_checksum=self.bundle.corpus_checksum,
            sources=[self.documents[identifier] for identifier in ordered],
            scopes=resolved_scopes,
            retrieval={
                "mode": mode,
                "mode_requested": requested_mode or mode,
                "raw_ranked_ids": [identifier for identifier, _ in raw_ranked],
                "selection_ranked_ids": [identifier for identifier, _ in ranked],
                "lexical_ranked_ids": [identifier for identifier, _ in lexical],
                "semantic_ranked_ids": [identifier for identifier, _ in semantic],
                "fusion": {
                    "algorithm": "weighted_reciprocal_rank_fusion",
                    "rank_constant": _RRF_RANK_CONSTANT,
                    "lexical_weight": _RRF_LEXICAL_WEIGHT,
                    "semantic_weight": _RRF_SEMANTIC_WEIGHT,
                }
                if mode == "hybrid"
                else None,
                "scores": {identifier: score for identifier, score in raw_ranked if identifier in selected},
                "selected_count": len(ordered),
                "corpus_count": len(self.documents),
                "policy_family_expansion": sorted(expanded_families),
                "explicit_source_ids": explicit,
                "exact_title_source_ids": title_matches,
                "scoped_selection": scoped_selection,
                "latency_ms": round((time.perf_counter() - started) * 1000, 3),
                "score_meaning": "Retrieval relevance with exact identifier priority, not answer confidence",
                "embedding_model": (self.bundle.config.get("embedding") or {}).get("model"),
                "raw_top_k_ids": [identifier for identifier, _ in raw_ranked[:top_k]],
                "top_k": top_k,
            },
        )
