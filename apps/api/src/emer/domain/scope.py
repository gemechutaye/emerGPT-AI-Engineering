"""Resolve scope from explicit identifiers and each temporal part, without ID-specific logic."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime
from itertools import pairwise

from emer.contracts.answer import QuestionScope, SourceDocument

PATIENT = re.compile(r"\bPT[\s-]*(\d{1,6})\b", re.IGNORECASE)
_NUMBER_WORDS = {
    "zero": 0, "oh": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14,
    "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20,
    "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90,
}  # fmt: skip
_WORDS = "|".join([*_NUMBER_WORDS, "hundred", "thousand", "million", "billion", "trillion"])
_NUMBER_TOKEN = rf"(?:\d+|{_WORDS})\b"
# Spoken and loosely typed identifier forms: "patient six", "patient number 6", "P.T. 006",
# "PT zero zero six". A following unit ("patient 2 weeks") means a quantity, not an identifier.
SPOKEN_PATIENT = re.compile(
    rf"\b(?:P\.?\s?T\.?|(?:patient|case)(?!s\b)(?:\s+(?:number|no\.?|#|id|record))?)[\s#:.\-–—]*"
    rf"((?>{_NUMBER_TOKEN}(?:[ \t-]+(?:and[ \t-]+)?{_NUMBER_TOKEN})*))",
    re.IGNORECASE,
)
_QUANTITY_SUFFIX = re.compile(
    r"^[ \t-]*(?:and[ \t]+(?:a[ \t]+)?(?:half|quarter)[ \t]+)?"
    r"(?:(?:weeks?|days?|months?|years?|hours?|minutes?|times?|sessions?|visits?|percent|am|pm|of)\b|%)",
    re.IGNORECASE,
)
_PATIENT_NUMBER_CLARIFICATION = "Please repeat the complete patient identifier using digits, such as PT-006."
# The interrogative must select an entity, not another noun followed much later by "patient"
# ("which product ... for an individual patient" is a generic clinical question).
WHICH_PATIENT = re.compile(
    r"\b(?:which|whose|list|identify|find|summari[sz]e|show|compare|how\s+many)\s+"
    r"(?:(?:of|the|a|an|all|every|each|our|these|those|synthetic|documented|recorded|available|eight|\d+)\s+)*"
    r"(?:patients?|patient\s+records?|cases?)\b"
    r"(?!\s+(?:preparation|instructions|information|education|care|guidance|intake|communication)\b)"
    r"|\bwhat\s+patient\s+(?:has|had|is|was|wants|needs|reports|requested)\b",
    re.IGNORECASE,
)
_DESCRIBED_PATIENT = re.compile(
    r"\bthe\s+(?:patient|person|one|case)\s+(?:who|whose|with|that)\s+\S"
    r"|\bwho\s+(?:has|had|wants|wanted|reports|reported|requested|prefers|received|completed)\b",
    re.IGNORECASE,
)
# Inspect overlapping noun phrases: an earlier "the follow-up ... for the ..."
# must not consume the later descriptive phrase before its boundary check runs.
_PATIENT_DESCRIPTOR = re.compile(r"(?=\bthe\s+((?:[\w-]+\s+){1,5})(?:patient|person|case)\b)", re.IGNORECASE)
_GENERIC_DESCRIPTORS = {
    "individual",
    "specific",
    "unspecified",
    "hypothetical",
    "example",
    "new",
    "current",
    "particular",
    "typical",
    "ordinary",
    "average",
    "same",
    "next",
    "previous",
    "prospective",
    "aesthetic",
    "generic",
}
_DESCRIPTOR_BOUNDARIES = {"a", "an", "the", "of", "for", "to", "and", "or", "in", "on", "at", "from"}
MONTHS = {
    name.lower(): number
    for number, name in enumerate(
        (
            "January",
            "February",
            "March",
            "April",
            "May",
            "June",
            "July",
            "August",
            "September",
            "October",
            "November",
            "December",
        ),
        1,
    )
}
MONTH_RE = re.compile(
    r"\b(" + "|".join(MONTHS) + r")(?:\s+(\d{1,2})(?:st|nd|rd|th)?(?!\d))?(?:,?\s+(\d{4}))?\b",
    re.IGNORECASE,
)
ISO_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
_SOURCE_INSPECTION = re.compile(
    r"\b(?:compare|contrast|difference|differences|differ|changed|quote|read|show|summari[sz]e|describe)\b"
    r"|\b(?:what|how)\b[^?;]*\b(?:say|says|said|state|states|stated|wording|text|content|contain|"
    r"contains|document|documents|describe|describes|specify|specifies|require|requires|recommend|recommends)\b",
    re.IGNORECASE,
)
_OPERATIONAL_POLICY_TASK = re.compile(
    r"\b(?:should|must|can|may|do)\s+(?:I|we|staff|the\s+(?:team|practice))\b"
    r"|\b(?:enforce|follow|use)\b[^?;]*\b(?:now|today|currently|this\s+appointment)\b"
    r"|\b(?:apply|applies|applicable|valid|in\s+force)\b[^?;]*\b(?:now|today|currently|still)\b"
    r"|\b(?:now|today|currently|still)\b[^?;]*\b(?:apply|applies|applicable|valid|in\s+force|rule|policy)\b"
    r"|\b(?:which|what)\b[^?;]*\b(?:policy|rule|version)\b[^?;]*\b(?:applies|applicable|in\s+force)\b",
    re.IGNORECASE,
)
_INSPECTION_TASK_BOUNDARY = re.compile(
    r"\s+(?:and|but)\s+(?=(?:which|what|can|should|must|may|do|does|is|are|tell\s+me\s+(?:which|what))\b)",
    re.IGNORECASE,
)


class ScopeError(ValueError):
    pass


def source_reference_ids(question: str, documents: list[SourceDocument]) -> list[str]:
    """Exact record inspection is independent of which policy governs an operational date.

    Merely mentioning or quoting an old ID while asking which rule to follow never opens
    that revision for use. Applicability and the query date are left untouched.
    """
    requested_patients = set(requested_patient_ids(question))
    references = [
        doc.doc_id
        for doc in documents
        if re.search(r"(?<![\w-])" + re.escape(doc.doc_id) + r"(?![\w-])", question, re.IGNORECASE)
        and (doc.category != "synthetic_patient" or doc.doc_id in requested_patients)
    ]
    if not references or _OPERATIONAL_POLICY_TASK.search(question):
        return []
    bare_reference = question.strip(" \t\n.?!`'\"").upper() in {
        identifier.upper() for identifier in references
    }
    return references if bare_reference or _SOURCE_INSPECTION.search(question) else []


def _source_task_chunks(question: str, documents: list[SourceDocument]) -> list[str]:
    """Separate an explicit inspection from a following operational question, only when both exist."""
    if not _SOURCE_INSPECTION.search(question) or not _OPERATIONAL_POLICY_TASK.search(question):
        return [question]
    chunks = _INSPECTION_TASK_BOUNDARY.split(question)
    if len(chunks) > 1 and any(source_reference_ids(chunk, documents) for chunk in chunks):
        return chunks
    return [question]


def _spoken_number(words: str) -> int:
    tokens = re.split(r"[\s-]+", words.lower().strip())
    if len(tokens) == 1 and tokens[0].isdigit():
        if len(tokens[0]) > 6:
            raise ScopeError(_PATIENT_NUMBER_CLARIFICATION)
        return int(tokens[0])
    if all(token in _NUMBER_WORDS and _NUMBER_WORDS[token] < 10 for token in tokens):
        # Digit-by-digit speech: "zero zero six" is 006.
        if len(tokens) > 6:
            raise ScopeError(_PATIENT_NUMBER_CLARIFICATION)
        return int("".join(str(_NUMBER_WORDS[token]) for token in tokens))

    def under_thousand(part: list[str]) -> int:
        hundreds = 0
        if len(part) >= 2 and part[1] == "hundred" and part[0] in _NUMBER_WORDS:
            multiplier = _NUMBER_WORDS[part[0]]
            if not 1 <= multiplier <= 9:
                raise ScopeError(_PATIENT_NUMBER_CLARIFICATION)
            hundreds, part = multiplier * 100, part[2:]
            if not part:
                return hundreds
        if len(part) == 1 and part[0] in _NUMBER_WORDS:
            return hundreds + _NUMBER_WORDS[part[0]]
        if len(part) == 2 and all(token in _NUMBER_WORDS for token in part):
            tens, units = (_NUMBER_WORDS[token] for token in part)
            if tens >= 20 and 1 <= units <= 9:
                return hundreds + tens + units
        raise ScopeError(_PATIENT_NUMBER_CLARIFICATION)

    # "And" joins a cardinal remainder, never two separate patient identifiers.
    if any(
        token == "and" and (i == 0 or tokens[i - 1] not in {"hundred", "thousand"})
        for i, token in enumerate(tokens)
    ):
        raise ScopeError(_PATIENT_NUMBER_CLARIFICATION)
    tokens = [token for token in tokens if token != "and"]
    if tokens.count("thousand") == 1:
        boundary = tokens.index("thousand")
        thousands = under_thousand(tokens[:boundary])
        if not thousands:
            raise ScopeError(_PATIENT_NUMBER_CLARIFICATION)
        return thousands * 1000 + (under_thousand(tokens[boundary + 1 :]) if tokens[boundary + 1 :] else 0)
    return under_thousand(tokens)


def canonicalize_patient_mentions(text: str) -> str:
    """Rewrite spoken/loose patient references to the corpus identifier form (PT-006)."""

    def replace(match: re.Match[str]) -> str:
        suffix = text[match.end() :]
        # Check the complete number's suffix outside the regex, so a quantity can never
        # backtrack to a shorter identifier ("twenty one years" must not become PT-020).
        if _QUANTITY_SUFFIX.match(suffix):
            return match.group()
        if re.match(r"(?:\.\d|\s+(?:point|and\s+(?:a\s+)?(?:half|quarter))\b)", suffix, re.IGNORECASE):
            raise ScopeError(_PATIENT_NUMBER_CLARIFICATION)
        number = _spoken_number(match.group(1))
        return f"PT-{number:03d}"

    return SPOKEN_PATIENT.sub(replace, text)


def patient_reference_clarification(text: str) -> str | None:
    """Validate complete numeric references before typed or Live intent resolution."""
    try:
        canonicalize_patient_mentions(text)
    except ScopeError as exc:
        return str(exc)
    return None


def patient_ids(text: str) -> list[str]:
    canonical = canonicalize_patient_mentions(text)
    return list(dict.fromkeys(f"PT-{int(m.group(1)):03d}" for m in PATIENT.finditer(canonical)))


def dismissed_context_question(text: str) -> str | None:
    """Remove an explicit leading context dismissal, never an ordinary negative fact.

    The remainder is still an unverified question and may need clarification. A
    complete clause boundary prevents 'leave a patient's medication aside from ...'
    from being interpreted as permission to change identity.
    """
    canonical = canonicalize_patient_mentions(text)
    subject = r"(?:PT[\s-]*\d{1,6}|(?:(?:that|this|the|previous|current)\s+)+(?:patient|person|case|record|topic|subject))"
    match = re.fullmatch(
        r"\s*(?:please\s+)?(?:leave|put|set)\s+" + subject
        + r"\s+aside(?:\s+for\s+now)?(?:\s*[.;:—]\s*|\s*,?\s+and\s+|\s*,\s*)(.*)",
        canonical, re.IGNORECASE | re.DOTALL,
    )
    return match.group(1).strip() if match else None


def requested_patient_ids(text: str) -> list[str]:
    """Keep a clear replacement target while leaving affirmative comparisons intact.

    This is intent syntax, not a patient lookup: unknown identifiers are retained,
    and complete-number validation runs before an excluded identity can be dropped.
    The original question remains unchanged for storage and source attribution.
    """
    canonical = canonicalize_patient_mentions(text)
    if (remainder := dismissed_context_question(canonical)) is not None:
        canonical = remainder
    matches = list(PATIENT.finditer(canonical))
    identifiers = patient_ids(canonical)
    if len(identifiers) < 2 or re.search(
        r"\b(?:compare|contrast|comparison|differences?|differ|both|between|versus|vs)\b",
        canonical, re.IGNORECASE,
    ):
        return identifiers
    negated_prefix = re.compile(
        r"\b(?:not(?:\s+(?:use|show|retrieve|for))?|instead\s+of|rather\s+than|excluding|except)\s*$",
        re.IGNORECASE,
    )
    positive = [match for match in matches if not negated_prefix.search(canonical[:match.start()])]
    positives = list(dict.fromkeys(normalize_patient(match.group()) for match in positive))
    if len(positive) < len(matches) and len(positives) == 1:
        return positives
    # A terse self-correction must occupy the entire separator. A sentence such as
    # "PT-X has no history, PT-Y ..." is not an identity replacement.
    if len(matches) == 2 and re.fullmatch(
        r"[\s,;:—-]*(?:sorry|actually|correction|no|I\s+(?:mean|meant))[\s,;:—-]*",
        canonical[matches[0].end():matches[1].start()], re.IGNORECASE,
    ):
        return [normalize_patient(matches[-1].group())]
    return identifiers


def non_patient_numbers(text: str) -> set[int]:
    """Numbers that are not part of a patient identifier; PT5 and PT-005 are the same ID."""
    return {
        int(number) for number in re.findall(r"\d+", PATIENT.sub("", canonicalize_patient_mentions(text)))
    }


def asks_which_patient(text: str) -> bool:
    return bool(WHICH_PATIENT.search(text))


def patient_discovery_requested(text: str) -> bool:
    """Allow record search for identity cues/descriptions, never mere clinical similarity."""
    if asks_which_patient(text):
        return True
    if _DESCRIBED_PATIENT.search(text):
        return True
    for match in _PATIENT_DESCRIPTOR.finditer(text):
        words = set(match.group(1).lower().split())
        if not words & _DESCRIPTOR_BOUNDARIES and words - _GENERIC_DESCRIPTORS:
            return True
    return False


def normalize_patient(value: str) -> str:
    match = PATIENT.fullmatch(canonicalize_patient_mentions(value).strip())
    if not match:
        raise ScopeError("Use an exact patient identifier such as PT-001")
    return f"PT-{int(match.group(1)):03d}"


def _is_calendar_month(match: re.Match[str], text: str) -> bool:
    """The modal verb 'may' needs calendar context before it can set a date."""
    if match.group(1).lower() != "may" or match.group(2) or match.group(3):
        return True
    before = text[: match.start()].rstrip()
    after = text[match.start() + len(match.group(1)) :].lstrip()
    if not before and not after.strip(" ?.!,:;"):
        return True
    if re.search(
        r"\b(?:in|during|for|since|until|before|after|through|between|from|to|by|on|of|this|last|next"
        r"|prior\s+to|later\s+than|earlier\s+than)\s*$",
        before,
        re.IGNORECASE,
    ):
        return True
    # Month lists/comparisons are explicit even when only the final month carries the year.
    month_names = "|".join(MONTHS)
    if re.match(r"(?:and|or|through|to|versus|vs\.?)\s+(?:" + month_names + r")\b", after, re.IGNORECASE):
        return True
    if re.search(
        r"\b(?:" + month_names + r")(?:\s+\d{4})?\s+(?:and|or|through|to|versus|vs\.?)\s*$",
        before,
        re.IGNORECASE,
    ):
        # In 'in June, or may I ...', May is still a modal verb, not a second month.
        return not bool(re.match(r"(?:I|we|you|he|she|they|it|the|a|an|be|have|not)\b", after, re.IGNORECASE))
    return False


def date_scope_clarification(question: str) -> str | None:
    """A point-date lookup must not silently collapse a temporal condition or range."""
    matches = sorted(
        [
            *ISO_RE.finditer(question),
            *(match for match in MONTH_RE.finditer(question) if _is_calendar_month(match, question)),
        ],
        key=lambda match: match.start(),
    )
    clarification = (
        "Which exact date should I use for this lookup? "
        "Please give one reference date, or name the dates you want to compare."
    )
    if len(set(re.findall(r"\b20\d{2}\b", question))) > 1 and any(
        match.re is MONTH_RE and not match.group(3) for match in matches
    ):
        return "Please give the year for each named date; more than one year is mentioned."
    independent_points = bool(re.search(
        r"\b(?:compare|contrast|comparison)\b"
        r"|\b(?:each|either|both|respective)\s+(?:dates?|days?|versions?|occasions?|points?)\b",
        question, re.IGNORECASE,
    ))
    interval_requested = bool(re.search(
        r"\b(?:throughout|period|range|interval)\b", question, re.IGNORECASE,
    ))
    comparison_starts = {
        left.start()
        for left, right in pairwise(matches)
        if (
            question[left.end() : right.start()].strip().lower() == "to"
            and re.search(r"\bcompare\b", question[: left.start()], re.IGNORECASE)
            or re.fullmatch(r",?\s*and", question[left.end() : right.start()].strip(), re.IGNORECASE)
            and independent_points
        ) and not interval_requested
    }
    for match in matches:
        relation = re.search(
            r"\b(?:before|after|since|until|through|between|from|prior\s+to|later\s+than|earlier\s+than)\s*$",
            question[: match.start()],
            re.IGNORECASE,
        )
        point_from = bool(
            relation
            and relation.group().strip().lower() == "from"
            and (
                match.start() in comparison_starts
                or (
                    len(matches) == 1
                    and re.search(r"\beffective\s+from\s*$", question[: match.start()], re.IGNORECASE)
                )
            )
        )
        if relation and not point_from:
            return clarification
        if (
            match.re is MONTH_RE
            and match.group(2)
            and re.match(
                r"\s*(?:to|through|until|[-–—])\s*\d{1,2}(?:st|nd|rd|th)?\b",
                question[match.end() :],
                re.IGNORECASE,
            )
        ):
            return clarification
    for left, right in pairwise(matches):
        separator = question[left.end() : right.start()].strip()
        if re.fullmatch(r"to|through|until|[-–—]", separator, re.IGNORECASE):
            # An explicit comparison names independent lookup dates, not an interval.
            if left.start() in comparison_starts:
                continue
            return clarification
    return None


def _dates(text: str, default_year: int) -> list[tuple[str, bool]]:
    result = []
    for match in ISO_RE.finditer(text):
        try:
            result.append((date.fromisoformat(match.group()).isoformat(), False))
        except ValueError as exc:
            raise ScopeError("The question contains an invalid calendar date") from exc
    year_match = re.search(r"\b(20\d{2})\b", text)
    year = int(year_match.group()) if year_match else default_year
    for match in MONTH_RE.finditer(text):
        if not _is_calendar_month(match, text):
            continue
        try:
            result.append(
                (
                    date(
                        int(match.group(3) or year), MONTHS[match.group(1).lower()], int(match.group(2) or 1)
                    ).isoformat(),
                    not bool(match.group(2)),
                )
            )
        except ValueError as exc:
            raise ScopeError("The question contains an invalid calendar date") from exc
    return list(dict.fromkeys(result))


def _patient_task_chunks(question: str, current_date: date) -> list[str]:
    """A same-date comparison owns both identities; independent patient/date tasks do not."""
    chunks = re.split(r"\s+(?:and|versus|vs\.?)\s+(?=PT[\s-]*\d)", question, flags=re.IGNORECASE)
    identifiers = list(PATIENT.finditer(question))
    if (
        len(chunks) < 2
        or len(set(patient_ids(question))) < 2
        or not re.search(
            r"\b(?:compare|contrast|comparison|differences?|differ|versus|vs\.?)\b", question, re.IGNORECASE
        )
    ):
        return chunks
    dates = {value for value, _ in _dates(question, current_date.year)}
    if len(dates) > 1:
        return chunks
    if (
        dates
        and dates != {current_date.isoformat()}
        and re.search(r"\b(?:today|currently|now)\b", question, re.IGNORECASE)
    ):
        return chunks
    # A date between patient IDs usually belongs to the preceding patient, whereas a
    # date before/after the whole comparison scopes the combined request. Preserve
    # explicit patient/date pairs unless every clause resolves to the same date.
    between_ids = question[identifiers[0].end() : identifiers[-1].start()]
    if _dates(between_ids, current_date.year):
        clause_dates = [
            {value for value, _ in _dates(chunk, current_date.year)} or {current_date.isoformat()}
            for chunk in chunks
        ]
        if any(values != dates for values in clause_dates):
            return chunks
    return [question]


def resolve_scopes(
    question: str,
    documents: list[SourceDocument],
    patient_id: str | None = None,
    as_of: str | date | None = None,
    today: date | None = None,
) -> list[QuestionScope]:
    if (remainder := dismissed_context_question(question)) is not None:
        question, patient_id = remainder, None
    if clarification := date_scope_clarification(question):
        raise ScopeError(clarification)
    today = today or datetime.now(UTC).date()
    try:
        context_date = date.fromisoformat(as_of) if isinstance(as_of, str) else as_of
    except ValueError as exc:
        raise ScopeError("Use a valid ISO date for date scope") from exc
    current_date = context_date or today
    known = {d.doc_id for d in documents if d.category == "synthetic_patient"}
    contextual_patient = normalize_patient(patient_id) if patient_id else None
    question = canonicalize_patient_mentions(question)
    explicit_years = set(re.findall(r"\b20\d{2}\b", question))
    # A single supplied year also qualifies date lists split into patient-specific
    # clauses. Multiple explicit years never supply an arbitrary missing year.
    question_year = int(next(iter(explicit_years))) if len(explicit_years) == 1 else current_date.year
    entire_ids = requested_patient_ids(question)
    # Preserve comparison groups while keeping independent sentences and paired dates separate.
    chunks = [
        part.strip()
        for sentence in re.split(r"[;\n]+|(?<=[?])\s+", question)
        for source_task in _source_task_chunks(sentence, documents)
        for part in _patient_task_chunks(source_task, current_date)
        if part.strip()
    ]
    scopes = []
    for chunk in chunks:
        ids = requested_patient_ids(chunk)
        # Explicit IDs define a subset even when the question uses aggregate wording.
        # Only an explicit universal request can broaden a selected patient context.
        universal = bool(
            re.search(
                r"\b(all|every|each|how\s+many)\s+(?:eight\s+|synthetic\s+)?patients?\b",
                chunk,
                re.IGNORECASE,
            )
        )
        # Enumerate actual records when requested; a general rule "for all patients" is not
        # permission to attach each fictional patient to an otherwise general clinical answer.
        record_enumeration = bool(
            re.search(
                r"\b(?:roll[- ]?up|status|statuses|goals|records?|synthetic|overdue|how\s+many)\b",
                chunk,
                re.IGNORECASE,
            )
        ) or asks_which_patient(chunk)
        all_patients = not ids and (
            (universal and record_enumeration)
            or (
                not contextual_patient
                and asks_which_patient(chunk)
                and bool(re.search(r"\bpatients\b", chunk, re.IGNORECASE))
            )
        )
        if not ids and not entire_ids and not contextual_patient:
            # An aggregate follow-up query searches records; it does not invent an event date.
            all_patients = all_patients or bool(
                re.search(r"\bfollow[- ]?ups?\b", chunk, re.IGNORECASE)
                and re.search(r"\b(?:overdue|due|which|list|compare)\b", chunk, re.IGNORECASE)
            )
        discovery = False
        if all_patients:
            ids = sorted(known)
        elif not ids and patient_discovery_requested(chunk):
            # "Which patient …" searches records by description; it is not bound to a selected patient.
            discovery, ids = True, sorted(known)
        elif not ids and not entire_ids and contextual_patient:
            ids = [contextual_patient]
        elif not ids and len(entire_ids) == 1:
            ids = entire_ids
        # No default enrollment: a general question or an unidentified "the patient" cannot
        # acquire an identity merely because a neighboring patient record ranks highly.
        resolved_dates = _dates(chunk, question_year)
        for resolved, month_only in resolved_dates or [(current_date.isoformat(), False)]:
            warnings = []
            if month_only:
                warnings.append(
                    "The named month is evaluated from its first day; no encounter date is inferred."
                )
            unknown = [identifier for identifier in ids if identifier not in known]
            if unknown:
                warnings.append(
                    "The requested patient record is absent; another patient must not be substituted."
                )
            if discovery:
                warnings.append(
                    "No patient was named; patient records were searched by description and a patient is named only when its record supports it."
                )
            elif ids:
                warnings.append(
                    "Source effective dates are publication dates, not patient encounters or absolute follow-up deadlines."
                )
            elif not unknown:
                warnings.append(
                    "No patient record was requested by identifier or identifying description. "
                    "Use general knowledge only; ask for identity if a particular patient's facts are needed."
                )
            scopes.append(
                QuestionScope(
                    part_id=f"part-{len(scopes) + 1}",
                    question=chunk,
                    patient_ids=[identifier for identifier in ids if identifier in known],
                    unknown_patient_ids=unknown,
                    as_of=resolved,
                    date_origin="question" if resolved_dates else "context" if context_date else "today",
                    all_patients=all_patients,
                    patient_discovery=discovery,
                    warnings=warnings,
                    source_reference_ids=source_reference_ids(chunk, documents),
                )
            )
    return scopes
