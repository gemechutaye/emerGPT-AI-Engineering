"""An explicit source-to-answer audit prevents an incomplete inventory defining its own rubric."""

import re
from hashlib import sha256

from emer.providers.openrouter import ProviderError


def source_audit_units(packet, draft):
    if packet.retrieval.get("unit") != "source_chunk":
        return []
    cited = {(item.part_id, citation.doc_id) for item in [*draft.statements, *draft.next_steps]
             for citation in item.citations}
    units = {}
    for passage in packet.passages:
        for part_id in passage.scope_ids:
            if (part_id, passage.doc_id) not in cited:
                continue
            # Newlines preserve structured fields; sentence boundaries expose conditions
            # and record context independently of the earlier model's chosen fact list.
            for match in re.finditer(r"\S(?:[^\n]*?\S)?(?=(?:[.!?](?:\s|$))|\n|$)[.!?]?", passage.text):
                text = match.group().strip()
                if not text:
                    continue
                start, end = passage.start + match.start(), passage.start + match.end()
                key = sha256(f"{part_id}:{passage.doc_id}:{start}:{end}".encode()).hexdigest()[:16]
                units[key] = {"unit_id": key, "part_id": part_id, "doc_id": passage.doc_id,
                              "start": start, "end": end, "text": text}
    if len(units) > 160:
        raise ProviderError("source_audit_budget", "The cited source audit exceeds its bounded review size.")
    return list(units.values())


def source_coverage_problems(packet, draft, review):
    units = {unit["unit_id"]: unit for unit in source_audit_units(packet, draft)}
    if not units:
        return []
    if {item.unit_id for item in review.source_review} != set(units) or len(review.source_review) != len(units):
        raise ProviderError("source_audit_incomplete", "The verifier did not account for every cited-source unit exactly once.")
    return [f"coverage {units[item.unit_id]['part_id']}: Missing source context or qualification: "
            f"{units[item.unit_id]['text']} ({item.reason})" for item in review.source_review
            if item.disposition == "required_omission"]
