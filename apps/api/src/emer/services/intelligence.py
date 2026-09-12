"""Bounded knowledge workflow shared by HTTP/Live and architecture evaluations.

The backend owns stages, permissions, budgets and retries. Models propose plans,
inventory source-supported facts, draft an answer and independently assess it.
"""

from emer.contracts.answer import EvidencePassage
from emer.providers.openrouter import ProviderError
from emer.services.answering import AnsweringService, locate_quote
from emer.services.evidence_assessment import assess_evidence
from emer.services.knowledge_search import retrieve_plan
from emer.services.question_planning import plan_question


def supported_passages(packet, assessment) -> list[EvidencePassage]:
    """Keep evidence for established facts across recovery, scoped to its original part."""
    selected = {}
    for part in assessment.parts:
        for fact in part.facts:
            for citation in fact.citations:
                matching = [passage for passage in packet.passages
                            if passage.doc_id == citation.doc_id and part.part_id in passage.scope_ids
                            and locate_quote(passage.text, citation.quote) is not None]
                # Inventory validation can allow a quote spanning contiguous chunks. Preserve
                # that source's selected intervals if no individual chunk contains the quote.
                if not matching:
                    matching = [passage for passage in packet.passages
                                if passage.doc_id == citation.doc_id and part.part_id in passage.scope_ids]
                for passage in matching:
                    existing = selected.get(passage.chunk_id)
                    selected[passage.chunk_id] = passage.model_copy(update={"scope_ids": list(dict.fromkeys(
                        [*(existing.scope_ids if existing else []), part.part_id]
                    ))})
    return list(selected.values())


class IntelligenceService:
    def __init__(self, generator, verifier, settings):
        if (generator is verifier or not getattr(verifier, "model", None)
                or verifier.model == getattr(generator, "model", None)):
            raise ProviderError("independent_verifier_required", "Configure a distinct semantic verifier.")
        self.generator, self.verifier, self.settings = generator, verifier, settings
        self.packet = None

    async def answer(self, bundle, question, *, patient_id=None, as_of=None, on_evidence=None):
        embedding_cache = {}
        searches = []
        plan = await plan_question(self.generator, question, bundle, patient_id, as_of)
        packet = await retrieve_plan(bundle, self.generator, plan, self.settings, embedding_cache=embedding_cache)
        self.packet = packet
        assessment = await assess_evidence(self.verifier, packet, question)
        searches.append({"reason": "initial", "plan": plan.model_dump(mode="json"),
                         "retrieval": packet.retrieval, "assessment": assessment.model_dump(mode="json")})
        # One correction for semantic plan omissions, with the original request preserved.
        if assessment.unassigned_requests:
            plan = await plan_question(self.generator, question, bundle, patient_id, as_of,
                                       missing_requests=assessment.unassigned_requests)
            packet = await retrieve_plan(bundle, self.generator, plan, self.settings, embedding_cache=embedding_cache)
            self.packet = packet
            assessment = await assess_evidence(self.verifier, packet, question)
            searches.append({"reason": "plan_coverage_repair", "plan": plan.model_dump(mode="json"),
                             "retrieval": packet.retrieval, "assessment": assessment.model_dump(mode="json")})
            if assessment.unassigned_requests:
                raise ProviderError("query_plan_incomplete", "The bounded question plan still omitted requested information.")
        recovery = {part.part_id: part.search_query for part in assessment.parts if part.search_query}
        if recovery:
            expanded_plan = plan.model_copy(update={"parts": [part.model_copy(update={
                "query": recovery.get(part.part_id) or part.query,
            }) for part in plan.parts]})
            packet = await retrieve_plan(
                bundle, self.generator, expanded_plan, self.settings, expanded=True,
                protected_passages=supported_passages(packet, assessment), embedding_cache=embedding_cache,
            )
            self.packet = packet
            assessment = await assess_evidence(self.verifier, packet, question)
            searches.append({"reason": "missing_evidence_recovery", "plan": expanded_plan.model_dump(mode="json"),
                             "retrieval": packet.retrieval, "assessment": assessment.model_dump(mode="json")})
            if assessment.unassigned_requests:
                raise ProviderError("query_plan_incomplete", "Requested information remains outside the evaluated question plan.")
        packet.assessment = assessment.model_dump(mode="json")
        packet.retrieval = {**packet.retrieval, "search_rounds": searches,
                            "recovery_attempted": bool(recovery),
                            "confidence_method": "independent_evidence_inventory; no uncalibrated score threshold",
                            "absence_scope": "retrieved_evidence_only"}
        if on_evidence:
            await on_evidence(packet)
        answer = await AnsweringService(self.generator, self.verifier).answer(packet, question)
        return answer, packet
