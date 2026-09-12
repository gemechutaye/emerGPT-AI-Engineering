"""Validate an interpreted voice request before lookup or speech publication."""

import re

from emer.domain.scope import ScopeError, canonicalize_patient_mentions, patient_ids
from emer.domain.utterance import correction_suffix
from emer.services.text_intent import (
    explicit_patients,
    resolved_detail_conflict,
    self_contained,
    unresolved_pronoun,
)

SAFE_CLARIFICATION = "Could you restate the question, including the patient or detail you mean?"
_MONTH_NAMES = [
    "january",
    "february",
    "march",
    "april",
    "may",
    "june",
    "july",
    "august",
    "september",
    "october",
    "november",
    "december",
]


def _months(text: str) -> set[int]:
    result = {
        i + 1 for i, month in enumerate(_MONTH_NAMES) if re.search(rf"\b{month}\b", text, re.IGNORECASE)
    }
    result.update(int(month) for month in re.findall(r"\b\d{4}-(\d{2})-\d{2}\b", text))
    return result


def validate_voice_resolution(
    question: str,
    clarification: str | None,
    user_text: str,
    context: dict,
    *,
    observed_user_text: str | None = None,
) -> tuple[str, str | None]:
    question = question.strip()
    suffix = correction_suffix(user_text)
    if suffix is not None:
        suffix = suffix.strip(" ,.!?:;")
    # Preserve a complete explicit correction verbatim (apart from ID spelling).
    # A model rewrite must not attach a different patient's earlier treatment to it.
    # A bare replacement ID remains contextual and still needs intent resolution.
    if suffix:
        try:
            if not explicit_patients(suffix) and unresolved_pronoun(suffix):
                return "", "Please repeat the correction, including the patient identifier."
            if explicit_patients(suffix) and self_contained(suffix):
                question = canonicalize_patient_mentions(suffix).strip(" ,.!?")
                clarification = None
        except ScopeError as exc:
            return "", str(exc)
    if clarification is not None or not question:
        # A resolver's clarification is not a route for unchecked factual speech.
        return "", SAFE_CLARIFICATION
    previous = context.get("previous_questions", [])
    observed = "\n".join(
        [
            *previous,
            observed_user_text if observed_user_text is not None else user_text,
            context.get("patient_id") or "",
            context.get("as_of") or "",
        ]
    )
    if conflict := resolved_detail_conflict(question, observed):
        return "", conflict
    if suffix is not None:
        try:
            corrected = explicit_patients(suffix)
            resolved_ids = set(patient_ids(question))
        except ScopeError as exc:
            return "", str(exc)
        if corrected and resolved_ids != corrected:
            return "", "Please repeat the question with the corrected patient identifier."
        months = _months(suffix)
        if months and _months(question) != months:
            return "", "Please repeat the question with the corrected policy date."
        dates = set(re.findall(r"\b\d{4}-\d{2}-\d{2}\b", suffix))
        if dates and set(re.findall(r"\b\d{4}-\d{2}-\d{2}\b", question)) != dates:
            return "", "Please repeat the question with the corrected policy date."
    return question, None
