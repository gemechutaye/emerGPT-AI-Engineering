"""Bounded, verbatim paragraph context around an already validated citation."""

import re

_PARAGRAPH_BREAK = re.compile(r"\r?\n[ \t]*\r?\n(?:[ \t]*\r?\n)*")


def citation_context_span(text: str, start: int, end: int, *, max_chars: int = 1600) -> tuple[int, int]:
    """Return source offsets containing the quote and nearby whole paragraphs.

    This is presentation context, not evidence validation: callers must establish
    that the original quote supports its claim before using these offsets.
    Paragraph boundaries are blank lines; no text or whitespace is normalized.
    If the containing paragraph(s) fit, include the immediate predecessor first,
    then the immediate successor when each still fits. Never skip a paragraph to
    include a more distant one. Oversized original quotes remain intact; an
    oversized containing paragraph does not cause partial or unbounded expansion.
    """
    if (
        not isinstance(text, str)
        or type(start) is not int
        or type(end) is not int
        or not 0 <= start < end <= len(text)
    ):
        raise ValueError("Citation offsets must identify a nonempty span in the source text.")
    if type(max_chars) is not int or max_chars < 1:
        raise ValueError("Citation context limit must be a positive integer.")
    if end - start >= max_chars:
        return start, end

    paragraphs: list[tuple[int, int]] = []
    cursor = 0
    for boundary in _PARAGRAPH_BREAK.finditer(text):
        if cursor < boundary.start():
            paragraphs.append((cursor, boundary.start()))
        cursor = boundary.end()
    if cursor < len(text):
        paragraphs.append((cursor, len(text)))
    intersected = [i for i, (left, right) in enumerate(paragraphs) if left < end and right > start]
    if not intersected:
        return start, end

    first, last = intersected[0], intersected[-1]
    left, right = min(start, paragraphs[first][0]), max(end, paragraphs[last][1])
    if right - left > max_chars:
        return start, end
    if first > 0 and right - paragraphs[first - 1][0] <= max_chars:
        left = paragraphs[first - 1][0]
    if last + 1 < len(paragraphs) and paragraphs[last + 1][1] - left <= max_chars:
        right = paragraphs[last + 1][1]
    return left, right
