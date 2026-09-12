"""Bounded saved-question context shared across input channels; never retrieval evidence."""

from emer.contracts.answer import EvidencePacket, PublishedAnswer
from emer.domain.conversation_context import discovered_patient
from emer.domain.dialogue_state import build_dialogue_state
from emer.domain.scope import patient_ids, patient_reference_clarification
from emer.storage.models import Run
from sqlalchemy import select


async def recent_intent_context(db, conversation_id: str, context_version: int) -> dict:
    rows = (
        await db.scalars(
            select(Run)
            .where(
                Run.conversation_id == conversation_id,
                Run.context_version == context_version,
                Run.status == "completed",
            )
            .order_by(Run.created_at.desc())
            .limit(6)
        )
    ).all()
    rows = [row for row in rows if (row.answer or {}).get("status") != "clarification"]
    questions = [row.context.get("resolved_question", row.question) for row in reversed(rows)]
    patient = None
    if questions and not patient_reference_clarification(questions[-1]):
        identifiers = patient_ids(questions[-1])
        if len(identifiers) == 1:
            patient = identifiers[0]
        elif not identifiers and rows[0].answer and rows[0].evidence:
            patient = discovered_patient(
                PublishedAnswer.model_validate(rows[0].answer),
                EvidencePacket.model_validate(rows[0].evidence),
            )
    state = build_dialogue_state(
        list(reversed(rows)), conversation_id=conversation_id, context_version=context_version,
    )
    return {"previous_questions": questions, "patient_id": patient,
            "dialogue_state": state.model_dump(mode="json")}


def live_startup_history(context: dict) -> list[dict]:
    """Keep whole user messages within a conservative UTF-8/token bound."""
    selected = []
    remaining = 6000
    for question in reversed(context.get("previous_questions", [])):
        size = len(question.encode())
        if size > remaining:
            break
        selected.append(
            {
                "type": "message",
                "role": "user",
                "content": [
                    {"type": "input_text", "text": question},
                ],
            }
        )
        remaining -= size
    history = list(reversed(selected))
    if history:
        subject = context.get("patient_id")
        history.append(
            {
                "type": "message",
                "role": "assistant",
                "content": [
                    {
                        "type": "output_text",
                        "text": (
                            "The previous answers are saved. "
                            + (f"The latest patient reference is {subject}. " if subject else "")
                            + "New factual questions still need a fresh lookup in the supplied sources."
                        ),
                    },
                ],
            }
        )
    return history
