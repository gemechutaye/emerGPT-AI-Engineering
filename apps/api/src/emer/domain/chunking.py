"""Deterministic retrieval spans; source text is never rewritten or normalized."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from hashlib import sha256

import tiktoken
from emer.contracts.answer import SourceDocument
from emer.contracts.knowledge import SourceChunk

CHUNKER_CONFIG = {
    "version": "source-spans-v1",
    "tokenizer": "cl100k_base",
    "max_tokens": 320,
    "overlap_tokens": 40,
    "boundaries": "markdown-sections-paragraphs-list-items-table-rows-sentences",
}


@lru_cache(maxsize=1)
def tokenizer():
    return tiktoken.get_encoding(CHUNKER_CONFIG["tokenizer"])


def count_tokens(text: str) -> int:
    # Retrieved text can contain strings resembling a tokenizer's special tokens.
    return len(tokenizer().encode(text, disallowed_special=()))


def chunk_identity(doc: SourceDocument, start: int, end: int, config: dict) -> str:
    material = {
        "doc_id": doc.doc_id,
        "source_sha256": doc.sha256,
        "start": start,
        "end": end,
        "chunker": config,
    }
    digest = sha256(json.dumps(material, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return "chunk-" + digest[:32]


@dataclass(frozen=True)
class _Unit:
    start: int
    end: int
    section: tuple[str, ...]


def _bounded_units(text: str, start: int, end: int, section: tuple[str, ...], cap: int):
    """Prefer sentence ends, falling back to exact Unicode-safe character boundaries."""
    boundaries = [start + m.end() for m in re.finditer(r"[.!?](?:[\"')\]]*)(?:\s+|$)", text[start:end])]
    if count_tokens(text[start:end]) > cap and len(boundaries) > 1:
        for stop in sorted({*boundaries, end}):
            if stop > start:
                yield from _bounded_units(text, start, stop, section, cap)
                start = stop
        return
    while start < end:
        # Bound tokenization work to the next chunk, even for a megabyte-long
        # paragraph with no sentence separators. Avoid rescanning the whole suffix.
        high = min(end, start + cap * 8)
        while high < end and count_tokens(text[start:high]) <= cap:
            high = min(end, start + (high - start) * 2)
        if high == end and count_tokens(text[start:end]) <= cap:
            yield _Unit(start, end, section)
            break
        low = start + 1
        while low < high:
            middle = (low + high + 1) // 2
            if count_tokens(text[start:middle]) <= cap:
                low = middle
            else:
                high = middle - 1
        stop = low
        # Keep normal sentences intact and keep the final fallback near a word boundary.
        sentence_end = next((b for b in reversed(boundaries) if start < b <= stop), None)
        if sentence_end is not None and count_tokens(text[start:sentence_end]) >= cap // 3:
            stop = sentence_end
        else:
            spaces = list(re.finditer(r"\s+", text[start:stop]))
            if spaces and spaces[-1].end() >= (stop - start) // 2:
                stop = start + spaces[-1].end()
        # BPE prefix lengths need not be strictly monotonic; enforce the final bound directly.
        while count_tokens(text[start:stop]) > cap:
            stop -= 1
        if stop <= start:
            raise ValueError("Chunk token limit cannot represent one Unicode character")
        yield _Unit(start, stop, section)
        start = stop


def _units(text: str, cap: int):
    section: list[str] = []
    # A paragraph includes its separators, retaining exact original offsets and complete coverage.
    for paragraph in re.finditer(r"\S[\s\S]*?(?:\n[ \t]*\n+|\Z)|\s+", text):
        start, end = paragraph.span()
        body = text[start:end]
        lines = list(re.finditer(r"[^\n]*(?:\n|$)", body))
        special = any(re.match(r"\s*(?:#{1,6}\s|[-*+]\s|\d+[.)]\s|\|)", line.group()) for line in lines)
        spans = (
            [(start + line.start(), start + line.end()) for line in lines if line.group()]
            if special
            else [(start, end)]
        )
        for left, right in spans:
            heading = re.match(r"[ \t]*(#{1,6})[ \t]+(.+?)[ \t]*\n?$", text[left:right])
            if heading:
                depth = len(heading[1])
                section = section[: depth - 1] + [heading[2].rstrip("# ")]
            yield from _bounded_units(text, left, right, tuple(section), cap)


def chunk_document(doc: SourceDocument, *, config: dict | None = None) -> list[SourceChunk]:
    config = dict(CHUNKER_CONFIG if config is None else config)
    cap, overlap = int(config["max_tokens"]), int(config["overlap_tokens"])
    if cap < 8 or not 0 <= overlap < cap or config["tokenizer"] != CHUNKER_CONFIG["tokenizer"]:
        raise ValueError("Invalid source chunking configuration")
    units = [_Unit(0, len(doc.text), ())] if count_tokens(doc.text) <= cap else list(_units(doc.text, cap))
    chunks: list[SourceChunk] = []
    current: list[_Unit] = []

    def emit():
        start, end = current[0].start, current[-1].end
        chunks.append(
            SourceChunk(
                chunk_id=chunk_identity(doc, start, end, config),
                doc_id=doc.doc_id,
                source_sha256=doc.sha256,
                ordinal=len(chunks),
                start=start,
                end=end,
                text=doc.text[start:end],
                section_path=list(current[0].section),
                token_count=count_tokens(doc.text[start:end]),
            )
        )

    for unit in units:
        section_changed = bool(current and unit.section != current[-1].section)
        too_long = bool(current and count_tokens(doc.text[current[0].start : unit.end]) > cap)
        if section_changed or too_long:
            emit()
            retained: list[_Unit] = []
            if not section_changed:
                for previous in reversed(current):
                    if count_tokens(doc.text[previous.start : current[-1].end]) > overlap:
                        break
                    retained.insert(0, previous)
            current = retained
            while current and count_tokens(doc.text[current[0].start : unit.end]) > cap:
                current.pop(0)
        current.append(unit)
    if current:
        emit()
    return chunks
