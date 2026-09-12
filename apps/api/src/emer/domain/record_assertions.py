"""Conservative clarity checks for record assertions, not general entailment.

These checks reject a few recognizable ambiguous grammatical forms for normal
repair. They never infer a patient fact or approve an answer as grounded. Source
identity/quote validation and the semantic checker remain required. Callers pass
the statement's validated cited sources, not unrelated retrieved documents.
"""

import re
from collections.abc import Sequence

from emer.contracts.answer import DraftStatement, SourceDocument

_SENTENCE = re.compile(r"[^.!?;\n]+[.!?;]?")
_HISTORY = re.compile(r"\b(?:history|prior\s+(?:procedures?|treatments?|surgery))\b", re.IGNORECASE)
_UNCERTAIN = re.compile(r"\b(?:undocumented|unknown|uncertain|not\s+(?:documented|recorded|known))\b", re.IGNORECASE)
_NEGATIVE_HISTORY = re.compile(
    r"(?P<head>\b(?:no|without)\s+"
    r"(?:(?!is\b|are\b|was\b|were\b|has\b|have\b|had\b|and\b|but\b)[\w'’-]+\s+){0,8}?"
    r"(?:history|prior\s+(?:procedures?|treatments?|surgery))\b)"
    r"[^.!?;\n]*", re.IGNORECASE,
)
_DOCUMENTATION = re.compile(r"\b(?:documented|recorded|reported|known|on\s+file)\b", re.IGNORECASE)
_NEGATION = re.compile(r"\b(?:no|not|never|without|undocumented|unknown|uncertain|pending)\b", re.IGNORECASE)
_DENIES_INFERENCE = re.compile(r"\b(?:not|cannot)\s+(?:mean|establish|confirm|prove)\b[^,;.!?]*$", re.IGNORECASE)
_PROSPECTIVE = re.compile(r"\b(?:must|should|may|can|could|would|will|needs?\s+to|to\s+be)\b", re.IGNORECASE)
_GENERAL_ATTRIBUTION = re.compile(
    r"^\s*(?:according\s+to\s+)?(?:the\s+)?(?:general\s+(?:overview|guidance|workflow|procedure)|"
    r"procedure\s+overview|practice\s+(?:workflow|guidance))\b", re.IGNORECASE,
)
_EVENT = r"(?:documented|recorded|completed|performed|administered)"
_SOURCE_EVENT = re.compile(rf"\b{_EVENT}\b", re.IGNORECASE)
_COMPLETED = re.compile(
    rf"\b(?P<subject>[^,;:.!?]+?)\s+"
    rf"(?:is|are|was|were|has\s+been|have\s+been|had\s+been)\s+"
    rf"(?P<event>{_EVENT})\b", re.IGNORECASE,
)
_WITH_COMPLETED = re.compile(
    rf"\bwith\s+(?P<subject>[^,;:.!?]+?)\s+(?P<event>{_EVENT})\b", re.IGNORECASE,
)
_WORDS = re.compile(r"[a-z]+")
_FUNCTION_WORDS = frozenset(
    ["a", "an", "and", "are", "as", "at", "be", "been", "being", "by", "for", "from", "had", "has", "have",
     "in", "is", "it", "of", "on", "or", "patient", "patients", "record", "records", "relevant", "that", "the",
     "their", "this", "to", "was", "were", "with", "current", "status", "already", "also", "now", "yet", "so", "far"]
)


def _sentences(text: str) -> list[str]:
    return [match.group().strip() for match in _SENTENCE.finditer(text)]


def _unknown_history(text: str) -> bool:
    return any(
        _HISTORY.search(sentence) and (
            _UNCERTAIN.search(sentence)
            or (_NEGATIVE_HISTORY.search(sentence) and _DOCUMENTATION.search(sentence))
        ) for sentence in _sentences(text)
    )


def _subject_words(text: str, patients: Sequence[SourceDocument]) -> set[str]:
    # Record IDs are metadata, never fixed patient names or domain branches.
    for patient in patients:
        text = re.sub(re.escape(patient.doc_id), "", text, flags=re.IGNORECASE)
    return set(_WORDS.findall(text.casefold())) - _FUNCTION_WORDS


def _explicit_negative_history(head: str, patient: SourceDocument) -> bool:
    words = _subject_words(head, [patient]) - {"no", "without", "any"}
    for sentence in _sentences(patient.text):
        for negative in _NEGATIVE_HISTORY.finditer(sentence):
            if _DOCUMENTATION.search(negative.group()) or _UNCERTAIN.search(negative.group()):
                continue
            if words <= _subject_words(negative.group("head"), [patient]):
                return True
    return False


def _matching_patients(text: str, patients: Sequence[SourceDocument]) -> list[SourceDocument]:
    named = [patient for patient in patients if re.search(
        rf"(?<!\w){re.escape(patient.doc_id)}(?!\w)", text, re.IGNORECASE,
    )]
    return named or list(patients)


def _patient_event_support(subject: str, event: str, patients: Sequence[SourceDocument]) -> bool:
    words = _subject_words(subject, patients)
    if not words:
        return False
    for patient in patients:
        for sentence in _sentences(patient.text):
            if (_NEGATION.search(sentence) or _PROSPECTIVE.search(sentence)):
                continue
            if (event.casefold() in {"documented", "recorded"} or _SOURCE_EVENT.search(sentence)) and (
                words <= _subject_words(sentence, patients)
            ):
                return True
    return False


def record_assertion_problems(
    statement: DraftStatement, sources: Sequence[SourceDocument],
) -> list[str]:
    """Return repair instructions for recognizable record-language ambiguity.

    This deliberately covers explicit negative-history phrases and passive
    completed-event phrases. It is not a classifier for arbitrary paraphrases.
    Ambiguous mixed citations must be rewritten with explicit source attribution;
    a general overview cannot prove that a patient's procedure or documentation
    occurred. Prospective instructions remain the semantic checker's concern.
    """
    cited_ids = {ref.doc_id for ref in statement.citations}
    cited = [source for source in sources if source.doc_id in cited_ids]
    patients = [source for source in cited if source.category == "synthetic_patient"]
    if not patients:
        return []
    problems: list[str] = []
    for sentence in _sentences(statement.text):
        local_patients = _matching_patients(sentence, patients)
        for negative in _NEGATIVE_HISTORY.finditer(sentence):
            # A leading "the record documents X and no history" does not bind
            # the missingness qualification clearly to the negative conjunct.
            if _DOCUMENTATION.search(negative.group()) or _UNCERTAIN.search(negative.group()):
                continue
            if _DENIES_INFERENCE.search(sentence[:negative.start()]):
                continue
            if any(_unknown_history(patient.text) and not _explicit_negative_history(negative.group("head"), patient)
                   for patient in local_patients):
                problems.append(
                    "Negative history is ambiguous: the cited patient record marks history as "
                    "undocumented or uncertain. State that qualification beside the history claim; "
                    "do not turn missing documentation into confirmed absence."
                )
                break
        if len(cited) == len(patients) or _GENERAL_ATTRIBUTION.search(sentence):
            continue
        # Split comma clauses so an unrelated earlier modal/negation cannot
        # license a trailing completed-event assertion.
        for clause in re.split(r"[,:]", sentence):
            if _GENERAL_ATTRIBUTION.search(clause):
                continue
            for event in [*_COMPLETED.finditer(clause), *_WITH_COMPLETED.finditer(clause)]:
                if _NEGATION.search(event.group()) or _PROSPECTIVE.search(event.group()):
                    continue
                subject = event.group("subject")
                if not _subject_words(subject, local_patients):
                    continue
                if not _patient_event_support(subject, event.group("event"), local_patients):
                    problems.append(
                        "Mixed patient and general-source citations leave a completed-event assertion "
                        "unattributed. Cite affirmative patient-record evidence for that event, or "
                        "separate it as explicitly general guidance with prospective wording. "
                        "General workflow does not establish completed patient work."
                    )
                    break
    return list(dict.fromkeys(problems))
