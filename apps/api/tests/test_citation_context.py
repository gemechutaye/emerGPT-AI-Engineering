"""Citation context tests use exact source strings, not model or retrieval doubles."""

import pytest
from emer.domain.citation_context import citation_context_span


def locate(text: str, quote: str) -> tuple[int, int]:
    start = text.index(quote)
    return start, start + len(quote)


def test_previous_recipient_and_following_qualification_remain_exact():
    text = (
        "Questions go to the licensed team.\n\n"
        "Its role is to support escalation.\n\n"
        "It cannot diagnose a condition.\n\n"
        "This more distant paragraph should remain outside the excerpt."
    )
    start, end = locate(text, "Its role is to support escalation.")
    left, right = citation_context_span(text, start, end)
    assert text[left:right] == text[: text.index("\n\nThis more distant")]
    assert left <= start < end <= right


@pytest.mark.parametrize("separator", ["\n\n", "\r\n\r\n", "\r\n \t\r\n\r\n"])
def test_unicode_offsets_and_blank_line_bytes_are_unchanged(separator):
    text = separator.join(["Équipe 👩🏾‍⚕️", "Use café guidance.", "No normalization."])
    start, end = locate(text, "café")
    assert citation_context_span(text, start, end) == (0, len(text))
    left, right = citation_context_span(text, start, end)
    assert text[left:right][start - left : end - left] == "café"


@pytest.mark.parametrize("quote,expected", [("First", "First.\n\nSecond."), ("Third", "Second.\n\nThird.")])
def test_beginning_and_end_include_only_available_adjacent_paragraph(quote, expected):
    text = "First.\n\nSecond.\n\nThird."
    left, right = citation_context_span(text, *locate(text, quote))
    assert text[left:right] == expected


def test_quote_spanning_paragraphs_keeps_every_original_character():
    text = "Prior.\n\nFirst cited.\n\nSecond cited.\n\nFollowing."
    start, end = locate(text, "cited.\n\nSecond")
    assert citation_context_span(text, start, end) == (0, len(text))


def test_single_newline_is_not_a_paragraph_boundary():
    text = "Age: 38\nGoal: texture\nStatus: deciding later"
    assert citation_context_span(text, *locate(text, "texture")) == (0, len(text))


def test_adjacent_context_counts_separators_and_respects_exact_cap():
    text = "Prior.\n\nCited.\n\nFollowing."
    start, end = locate(text, "Cited")
    left, right = citation_context_span(text, start, end, max_chars=len("Prior.\n\nCited."))
    assert text[left:right] == "Prior.\n\nCited."
    assert right - left == len("Prior.\n\nCited.")


def test_oversized_previous_paragraph_does_not_block_fitting_next_paragraph():
    text = "x" * 1601 + "\n\nCited.\n\nFollowing."
    left, right = citation_context_span(text, *locate(text, "Cited"))
    assert text[left:right] == "Cited.\n\nFollowing."


def test_huge_containing_paragraph_preserves_original_quote_without_expansion():
    text = "x" * 4000 + "known quote" + "y" * 4000
    original = locate(text, "known quote")
    assert citation_context_span(text, *original) == original


def test_original_quote_can_exceed_budget_without_truncation():
    text = "Before.\n\n" + "x" * 1700 + "\n\nAfter."
    original = locate(text, "x" * 1700)
    assert citation_context_span(text, *original) == original


def test_separator_only_quote_is_preserved_without_inventing_a_paragraph():
    text = "First.\n\nSecond."
    original = locate(text, "\n\n")
    assert citation_context_span(text, *original) == original


@pytest.mark.parametrize(
    "start,end", [(None, 1), (0, None), (-1, 2), (0, 10), (1, 1), (2, 1), (True, 2), (0, 1.5)]
)
def test_rejects_absent_or_invalid_offsets(start, end):
    with pytest.raises(ValueError, match="Citation offsets"):
        citation_context_span("text", start, end)


@pytest.mark.parametrize("text", ["", None, 42])
def test_rejects_missing_source_text(text):
    with pytest.raises(ValueError, match="Citation offsets"):
        citation_context_span(text, 0, 1)


@pytest.mark.parametrize("limit", [0, -1, None, 2.5, True])
def test_rejects_invalid_budget(limit):
    with pytest.raises(ValueError, match="context limit"):
        citation_context_span("text", 0, 1, max_chars=limit)
