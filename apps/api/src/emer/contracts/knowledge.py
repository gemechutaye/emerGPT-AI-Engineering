"""Source-addressable retrieval units and bounded question plans."""

from emer.contracts.answer import Contract, EvidencePassage, QuestionScope, SourceChunk  # noqa: F401
from pydantic import Field


class SearchPart(Contract):
    part_id: str
    query: str = Field(min_length=1, max_length=2000)
    requested_information: list[str] = Field(min_length=1, max_length=12)
    scope: QuestionScope


class QuestionPlan(Contract):
    original_question: str
    parts: list[SearchPart] = Field(min_length=1, max_length=8)
    planner: str
