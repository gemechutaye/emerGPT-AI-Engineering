"""Explicit spoken correction cues; ordinary negative facts are not corrections."""

import re

CORRECTION = re.compile(
    r"\b(?:actually|correction|i mean(?:t)?|sorry(?=[,\s])|instead|change that|switch to|change to)\b"
    r"|\bno(?=\s*,)|(?:^|[.!?]\s*)no(?=\s+(?:patient\b|p[.\s-]*t\b|i mean))",
    re.IGNORECASE,
)


def correction_suffix(text: str) -> str | None:
    matches = list(CORRECTION.finditer(text))
    return text[matches[-1].end() :] if matches else None
