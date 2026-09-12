"""A verified discovery can supply identity to the next lookup, not new factual evidence."""

from copy import deepcopy
from pathlib import Path

import pytest
from emer.contracts.answer import AnswerGap, Citation, ProviderUsage, PublishedAnswer, PublishedStatement
from emer.domain.conversation_context import discovered_patient
from emer.providers.openrouter import StructuredResult
from emer.services.ingestion import ingest
from emer.services.retrieval import RetrievalService
from emer.services.text_intent import ResolvedTextIntent, resolve_text_intent

QUESTION = "Which patient had their light-based procedure discussion postponed because of sun exposure?"


@pytest.fixture
def discovery():
    packet = RetrievalService(ingest(Path(__file__).resolve().parents[3] / "config/corpus.json")).evidence(QUESTION, mode="full")
    source = next(item for item in packet.sources if item.doc_id == "PT-003")
    citation = Citation(
        doc_id=source.doc_id, title=source.title, version=source.version,
        effective_date=source.effective_date, authority=source.authority,
        source_sha256=source.sha256, index_id=packet.index_id,
        quote=source.text, start=0, end=len(source.text),
    )
    answer = PublishedAnswer(
        status="answered", statements=[PublishedStatement(
            part_id=packet.scopes[0].part_id,
            text="PT-003 had the light-based procedure discussion postponed because of recent sun exposure.",
            citations=[citation],
        )], gaps=[], next_steps=[], scopes=packet.scopes,
        index_id=packet.index_id, index_checksum=packet.index_checksum, usage=[], diagnostics={},
    )
    return answer, packet


def test_single_cited_discovery_uses_published_entity_not_all_retrieved_patients(discovery):
    answer, packet = discovery
    assert len(packet.scopes[0].patient_ids) == 8
    assert discovered_patient(answer, packet) == "PT-003"


@pytest.mark.parametrize("change", ["multiple", "gap", "next_step", "uncited", "quote", "hash", "index", "unsupported", "all", "not_discovery"])
def test_unconfirmed_or_ambiguous_entities_are_not_inherited(discovery, change):
    answer, packet = deepcopy(discovery)
    if change == "multiple":
        answer.statements[0].text += " PT-004 also has a different follow-up."
    elif change == "gap":
        answer.gaps.append(AnswerGap(part_id="part-1", text="PT-004 has no recorded appointment date."))
    elif change == "next_step":
        answer.next_steps.append(PublishedStatement(part_id="part-1", text="Review PT-004's record.", citations=[]))
    elif change == "uncited":
        answer.statements[0].citations.clear()
    elif change == "quote":
        answer.statements[0].citations[0].quote = "invented passage"
    elif change == "hash":
        answer.statements[0].citations[0].source_sha256 = "changed"
    elif change == "index":
        answer.index_id = "different-index"
    elif change == "unsupported":
        answer.status = "unsupported"
    elif change == "all":
        answer.scopes[0].all_patients = True
    else:
        answer.scopes[0].patient_discovery = False
    assert discovered_patient(answer, packet) is None


async def test_discovery_pronoun_resolves_but_explicit_patient_still_wins(discovery):
    answer, packet = discovery
    class Provider:
        async def structured(self, instructions, payload, schema, **options):
            current = "PT-004" if "PT-004" in payload["question"] else "PT-003"
            assert payload["active_patient_ids"] == [current]
            return StructuredResult(
                value=ResolvedTextIntent(question=f"What is {current}'s follow-up?", clarification=None),
                usage=ProviderUsage(operation="text_intent_resolution", model="test-double", latency_ms=0),
            )
    confirmed = discovered_patient(answer, packet)
    for question, expected in [("What is their follow-up?", "PT-003"), ("Same question for PT-004", "PT-004")]:
        result = await resolve_text_intent(Provider(), question, [QUESTION], confirmed, None)
        assert result.clarification is None and expected in result.question
