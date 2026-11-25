"""Building blocks every detection module shares.

The rule record, the direct-identifier categories, and the constructors for a
finding and its pseudonym. Moved out of ``privacy.py`` unchanged; that module
re-exports every name here, so callers keep importing from it.
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from dataclasses import dataclass

from legal_analyzer.turkish_names import tr_fold



DIRECT_IDENTIFIER_CATEGORIES = {
    "natural_person_name",
    "email_address",
    "phone_number",
    "turkish_national_id",
    "iban",
    "tax_number",
    "mersis_number",
    "trade_registry_number",
    "bar_registration_number",
    "case_or_investigation_number",
    "address",
    "company_name",
}


@dataclass(frozen=True)
class DetectionRule:
    category: str
    pattern: str
    risk: str
    action: str
    placeholder_prefix: str
    flags: int = re.IGNORECASE | re.UNICODE


def _finding(
    category: str,
    value: str,
    risk: str,
    action: str,
    placeholder: str,
    start: int,
    end: int,
    semantic_type: str | None = None,
) -> dict:
    finding = {
        "category": category,
        "sample": value[:180],
        "risk": risk,
        "recommended_action": action,
        "placeholder": placeholder,
        "start": start,
        "end": end,
        "fingerprint": hashlib.sha256(value.lower().encode("utf-8")).hexdigest()[:12],
    }
    # What legal_semantics classified the span as, where that is more specific
    # than its category: an institution type, an amount's or a date's role, a
    # privilege subtype. redaction_policy.semantic_type_of() falls back to the
    # category for everything else.
    if semantic_type:
        finding["semantic_type"] = semantic_type
    return finding


def _pseudonym_key(value: str) -> str:
    """Fold a matched value to the key its pseudonym is remembered under.

    str.lower() is not Turkish-aware, and the screen promises "consistent
    pseudonymization". Python lowercases the ASCII "I" of "DAVALI" to a dotted
    "i", while the same role spelled "Davalı" already carries the dotless "ı" --
    two different strings, so one party role was issued [PARTY_ROLE_2] in the
    heading and [PARTY_ROLE_3] four lines later. tr_fold is the repo's existing
    folding helper (legal_analyzer/turkish_names.py); it maps the dotted/dotless
    pairs explicitly before casefolding, and it is already what the given-name
    gazetteer matches on, so this reuses that scheme rather than adding another.

    Whitespace runs collapse too, so a name split by a DOCX run boundary or a
    wrapped line ("Şeyma  Karaduman") keeps the pseudonym it was first given.

    The dotless "ı" is then collapsed onto "i". tr_fold maps an ASCII capital
    I to "ı", which is the Turkish reading and the wrong one for an English
    name in capitals: a claim heading's "CORNELIUS FAIRBROTHER" keyed as
    "cornelıus faırbrother" and the "Cornelius Fairbrother" who signs the
    statement as "cornelius fairbrother" -- one witness, two pseudonyms. After
    the collapse "DAVALI", "Davalı" and the ASCII "Davali" are still one key,
    which is what tr_fold was brought in for.

    This is the KEY only. The finding's value, its span and the placeholder's
    visible text are untouched -- the folded form is never written anywhere.
    """
    return tr_fold(re.sub(r"\s+", " ", value)).replace("ı", "i").strip()


def _placeholder_for(prefix: str, value: str, counters: Counter[str], state: dict[tuple[str, str], str]) -> str:
    key = (prefix, _pseudonym_key(value))
    if key not in state:
        counters[prefix] += 1
        state[key] = f"[{prefix}_{counters[prefix]}]"
    return state[key]
