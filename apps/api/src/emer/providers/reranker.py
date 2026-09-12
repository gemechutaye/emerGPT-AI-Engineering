"""Pinned local cross-encoder inference. Scores are relevance logits, not confidence."""

from __future__ import annotations

import threading
from functools import lru_cache
from pathlib import Path

import numpy as np

MODEL_ID = "cross-encoder/ms-marco-MiniLM-L6-v2"
MODEL_REVISION = "233902d25c440f23af6f7d6e94d2946bac0bee0a"
MODEL_FILE = "onnx/model.onnx"


def prepare_reranker(cache_dir: str) -> dict[str, str]:
    """Explicit setup operation; request-time inference never downloads weights."""
    from huggingface_hub import hf_hub_download

    return {
        name: hf_hub_download(MODEL_ID, name, revision=MODEL_REVISION, cache_dir=cache_dir)
        for name in (MODEL_FILE, "tokenizer.json")
    }


class CrossEncoderReranker:
    def __init__(self, cache_dir: str):
        import onnxruntime as ort
        from huggingface_hub import hf_hub_download
        from tokenizers import Tokenizer

        files = {
            name: hf_hub_download(
                MODEL_ID, name, revision=MODEL_REVISION, cache_dir=cache_dir, local_files_only=True
            ) for name in (MODEL_FILE, "tokenizer.json")
        }
        options = ort.SessionOptions()
        options.intra_op_num_threads = 2
        options.inter_op_num_threads = 1
        self.session = ort.InferenceSession(files[MODEL_FILE], options, providers=["CPUExecutionProvider"])
        self.tokenizer = Tokenizer.from_file(files["tokenizer.json"])
        self.tokenizer.enable_padding()
        self.lock = threading.Lock()
        self.identity = {"model": MODEL_ID, "revision": MODEL_REVISION, "backend": "onnx-cpu"}

    def score(self, query: str, passages: list[str]) -> list[float]:
        """Score every passage, splitting long pairs into overlapping model windows.

        Tokenizer offsets preserve source coverage; no tail is silently truncated.
        Batch and concurrency bounds prevent model inference from exhausting the host.
        """
        if len(passages) > 96:
            raise ValueError("Reranker candidate budget exceeded")
        if not passages:
            return []
        with self.lock:
            query_tokens = self.tokenizer.encode(query, add_special_tokens=False)
            if len(query_tokens.ids) > 192:
                raise ValueError("Search query exceeds the reranker's 192-token budget")
            remaining = 512 - len(query_tokens.ids) - 3
            pairs, owners = [], []
            for owner, passage in enumerate(passages):
                encoded = self.tokenizer.encode(passage, add_special_tokens=False)
                for left in range(0, max(1, len(encoded.ids)), max(1, remaining - 32)):
                    offsets = encoded.offsets[left:left + remaining]
                    text = passage[offsets[0][0]:offsets[-1][1]] if offsets else passage
                    pairs.append((query, text))
                    owners.append(owner)
                    if left + remaining >= len(encoded.ids):
                        break
            scores = np.full(len(passages), -np.inf)
            for start in range(0, len(pairs), 16):
                batch = self.tokenizer.encode_batch(pairs[start:start + 16])
                arrays = {
                    "input_ids": np.asarray([item.ids for item in batch], dtype=np.int64),
                    "attention_mask": np.asarray([item.attention_mask for item in batch], dtype=np.int64),
                    "token_type_ids": np.asarray([item.type_ids for item in batch], dtype=np.int64),
                }
                inputs = {item.name: arrays[item.name] for item in self.session.get_inputs()}
                values = self.session.run(None, inputs)[0].reshape(-1)
                if len(values) != len(batch) or not np.isfinite(values).all():
                    raise ValueError("Reranker returned invalid scores")
                for owner, value in zip(owners[start:start + 16], values, strict=True):
                    scores[owner] = max(scores[owner], float(value))
            return scores.tolist()


@lru_cache(maxsize=1)
def get_reranker(cache_dir: str) -> CrossEncoderReranker:
    return CrossEncoderReranker(str(Path(cache_dir).resolve()))
