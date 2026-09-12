"""Offline configuration regressions; no database or provider calls."""

import unittest
from copy import deepcopy
from types import SimpleNamespace

from check_index import IndexConfigurationError, validate_index


class CheckIndexTests(unittest.TestCase):
    def setUp(self):
        self.source = SimpleNamespace(
            corpus_checksum="source-hash",
            # Real unembedded ingestion includes this key with a null value.
            config={"index_version": "test-format", "policies": [{"family": "test"}], "embedding": None},
            documents=[SimpleNamespace(doc_id="source-one", text="Original text")],
        )
        self.bundle = deepcopy(self.source)
        self.bundle.config["embedding"] = {"model": "configured-embedding", "dimensions": 3}

    def validate(self, mode="hybrid"):
        validate_index(self.bundle, self.source, mode, "configured-embedding")

    def test_matching_index_passes(self):
        self.validate()

    def test_missing_embeddings_do_not_silently_downgrade(self):
        self.bundle.config["embedding"] = None
        for mode in ("hybrid", "semantic"):
            with self.subTest(mode=mode), self.assertRaises(IndexConfigurationError):
                self.validate(mode)

    def test_different_embedding_space_is_rejected(self):
        self.bundle.config["embedding"]["model"] = "other-model"
        with self.assertRaises(IndexConfigurationError):
            self.validate()

    def test_lexical_mode_is_explicitly_allowed_without_embeddings(self):
        self.bundle.config["embedding"] = None
        self.validate("lexical")

    def test_changed_corpus_policies_and_format_are_rejected(self):
        for field in ("corpus_checksum", "policies", "index_version"):
            with self.subTest(field=field):
                original = deepcopy(self.bundle)
                if field == "corpus_checksum":
                    self.bundle.corpus_checksum = "other-corpus"
                else:
                    self.bundle.config[field] = "changed"
                with self.assertRaises(IndexConfigurationError):
                    self.validate()
                self.bundle = original

    def test_missing_or_modified_source_is_rejected(self):
        self.bundle.documents = []
        with self.assertRaises(IndexConfigurationError):
            self.validate()
        self.bundle.documents = [SimpleNamespace(doc_id="source-one", text="Changed text")]
        with self.assertRaises(IndexConfigurationError):
            self.validate()

    def test_order_is_not_a_source_change(self):
        self.source.documents.append(SimpleNamespace(doc_id="source-two", text="Second"))
        self.bundle.documents = list(reversed(deepcopy(self.source.documents)))
        self.validate()

    def test_new_metadata_and_chunk_configuration_cannot_be_ignored(self):
        for key in ("source_catalog", "chunker"):
            with self.subTest(key=key):
                self.source.config[key] = {"version": 2}
                with self.assertRaises(IndexConfigurationError):
                    self.validate()
                self.bundle.config[key] = {"version": 2}
                self.validate()

    def test_chunked_sources_require_chunk_embeddings(self):
        self.source.chunks = ["chunk-one"]
        self.bundle.config["embedding"]["unit"] = "document"
        with self.assertRaises(IndexConfigurationError):
            self.validate()
        self.bundle.config["embedding"]["unit"] = "chunk"
        self.validate()


if __name__ == "__main__":
    unittest.main()
