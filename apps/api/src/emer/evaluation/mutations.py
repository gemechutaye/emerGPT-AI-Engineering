"""In-memory evaluation-only source perturbations. Never publish an active corpus bundle."""

from hashlib import sha256

from emer.contracts.answer import EvidencePacket
from emer.services.ingestion import canonical


def mutate_packet(packet: EvidencePacket, fixture: dict) -> EvidencePacket:
    changed = packet.model_copy(deep=True)
    targets = [doc for doc in changed.sources if doc.doc_id == fixture["doc_id"]]
    if len(targets) != 1 or targets[0].text.count(fixture["old"]) != 1:
        raise ValueError("Mutation does not uniquely match its declared original passage")
    source = targets[0]
    source.text = source.text.replace(fixture["old"], fixture["new"], 1)
    source.sha256 = sha256(source.text.encode()).hexdigest()
    identity = sha256(
        canonical(
            {
                "parent_index": packet.index_id,
                "fixture": fixture,
                "sources": [doc.model_dump() for doc in changed.sources],
            }
        )
    ).hexdigest()
    changed.index_id = "eval-" + identity[:24]
    changed.index_checksum = identity
    changed.retrieval.update(
        {
            "evaluation_only": True,
            "mutation_id": fixture["id"],
            "parent_index_id": packet.index_id,
            "never_activate": True,
        }
    )
    return changed
