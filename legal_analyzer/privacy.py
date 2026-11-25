"""Cautious privacy and confidentiality risk mapping for legal documents."""

from __future__ import annotations

import re
from collections import Counter

from .taxonomy import OUTPUT_POSITIONING, TERMINOLOGY

from legal_analyzer import legal_semantics, redaction_policy
from .classifier import TURKISH, UNKNOWN_LANGUAGE, detect_language
from legal_analyzer.english_names import detect_english_person_names
from legal_analyzer.turkish_names import COMPANY_SUFFIX_RE, detect_person_names, is_party_role_term, tr_fold

# The detection rules and their helpers live in the privacy_* modules, split by
# concern. This module re-exports every name they define, private ones
# included, so `from legal_analyzer.privacy import X` and `privacy.X` keep
# working for every existing caller and test. What stays here: the release-gate
# vocabulary and policy, the reviewer explanations, analyze_privacy, and the
# three helpers that read DETECTION_RULES or detect_english_person_names as
# globals OF THIS MODULE (_named_court_spans, _person_spans, _venue_matches).
# tests/test_english_detection.py patches those two names here and expects
# analyze_privacy to see the patch; moving a reader out would silently stop it.

from .privacy_common import (
    DetectionRule,
    DIRECT_IDENTIFIER_CATEGORIES,
    _finding,
    _placeholder_for,
    _pseudonym_key,
)
from .privacy_context import (
    _ALLEGATION_MARKER_RE,
    classify_document_context,
    _CONTEXT_SWEEP_ABBREVIATIONS,
    CONTEXT_SWEEP_CATEGORIES,
    _CONTEXT_SWEEP_CHAR,
    _context_sweep_pieces,
    _CONTEXT_TRIGGER_RE,
    _CONTEXT_TRIGGER_RES,
    _CONTEXT_TRIGGERS,
    _DOCKET_AFTER_RE,
    _DOCKET_CASE_NUMBER,
    _DOCKET_NUMBER_BEFORE_RE,
    _DOCKET_NUMBER_BEFORE_WINDOW,
    _i_forms,
    IDENTIFIER_LABEL_ABBREVIATIONS,
    _INVESTIGATION_WORD,
    _INVESTIGATION_WORD_RE,
    _is_docket_investigation,
    language_evidence_spans,
    _RETIRED_CONTEXT_SWEEPS,
    _segment_is_reportable,
    _SEGMENT_LEADING_NOISE_RE,
    _SEGMENT_WORD_RE,
    _SweepPiece,
)
from .privacy_identifiers import (
    _address_start,
    _AMOUNT,
    _AMOUNT_MULTIPLIER,
    _assign_typed_value_placeholders,
    _bare_case_number_repeats,
    _carries_bare_repeats,
    _CASE_NUMBER_CORE_RE,
    _case_number_key,
    _EN_ADDRESS_TAIL,
    _EN_BUILDING_TYPE,
    _EN_CAP_WORD,
    _EN_COUNTRY,
    _EN_DIRECTIONAL,
    _EN_HOUSE_NUMBER,
    _EN_LOCALITY,
    _EN_MONTH,
    _EN_ORDINAL_DAY,
    _EN_SEGMENT,
    _EN_STREET_NAME,
    _EN_STREET_TYPE,
    _EN_SUBUNIT,
    ENGLISH_ACCOUNT_RULE,
    ENGLISH_ADDRESS_RULE,
    ENGLISH_CASE_NUMBER_RULE,
    ENGLISH_DATE_RULE,
    ENGLISH_PHONE_RULE,
    finding_needles,
    _follows_trade_registry_label,
    _identifier_key,
    _INLINE_SPACE,
    _KEYED_ON_DIGITS,
    _LABELLED_IDENTIFIER_GROUPS,
    MONEY_RULE,
    _NANP_PHONE,
    _PASSPORT_CORE_RE,
    _PREFIX_CURRENCY_CODES,
    _PREFIX_CURRENCY_SYMBOLS,
    _SUFFIX_CURRENCY,
    _title_and_caps,
    _TRADE_REGISTRY_LABEL_END,
    TURKISH_PHONE_RULE,
    _UK_PHONE,
    _UK_POSTCODE,
    _US_ZIP,
    valid_turkish_national_id,
    valid_turkish_tax_number,
    _WRAPPED_PREFIX_CURRENCY,
)
from .privacy_courts import (
    _assign_grouped_placeholders,
    _AUTHORITY_CASE_END,
    _bar_identity,
    _COURT_PLACE_PREFIX,
    _court_start,
    _DIRECTORATE_LEVEL,
    DIRECTORATE_RULE,
    _DIRECTORATE_SUBJECT,
    _EN_COURT_CHAMBER,
    _EN_COURT_CONTINUATION,
    _EN_COURT_DIVISIONS,
    _EN_COURT_LEVEL,
    _EN_COURT_NOUN,
    _EN_COURT_PARENT,
    _EN_COURT_QUALIFIERS,
    _EN_COURT_SITTING,
    _EN_COURT_STOP,
    _EN_COURT_UNIT,
    _enclosing_span_end,
    ENGLISH_COURT_RULE,
    _follows_tax_office_label,
    _generic_court_matches,
    GENERIC_COURT_SUFFIX_RULE,
    _HIGH_COURT_CHAMBER,
    HIGH_COURT_RULE,
    _inside_any,
    _is_unplaced_tax_office,
    _MUDURLUGU,
    NUMBERED_COURT_RULE,
    PROSECUTOR_OFFICE_RULE,
    _tax_office_field_identity,
    _TAX_OFFICE_FIELD_NEXT_WORD,
    TAX_OFFICE_FIELD_RULE,
    _TAX_OFFICE_FIELD_STOP,
    _TAX_OFFICE_FIELD_WORD,
    _TAX_OFFICE_LABEL_RE,
    _venue_identity,
    _venue_prefix_start,
)
from .privacy_names import (
    _bearer_index,
    _Bearers,
    _BUROSU,
    _COMPANY_ABBREVIATION_LETTERS,
    _company_alias_findings,
    _COMPANY_CONNECTORS,
    _COMPANY_FIRM_WORDS,
    _COMPANY_NAME_PREPOSITIONS,
    _COMPANY_PAIR_OPENERS,
    _COMPANY_STOP_WORDS,
    COMPANY_RULE,
    _company_start,
    _COMPANY_SUFFIXES,
    _company_token_case,
    _court_claimed_spans,
    _ENGLISH_LITIGATION_ROLES,
    _covered_counts,
    _established_surnames,
    _full_name_repeat,
    _GIVEN_NAME_KEYS,
    _given_name_keys,
    _given_names_before_re,
    _in_party_role_term,
    _is_guard_word,
    _is_party_role_word,
    _LAW_FIRM_FORMS,
    _LAW_FIRM_NAME_JOIN,
    LAW_FIRM_RULE,
    _mention_key,
    _MENTION_WORD_RE,
    _NAME_SEP,
    _NAME_SUFFIXES,
    _name_words,
    _nearest_bearer,
    _NEXT_WORD_RE,
    _NOTARY_ENDORSEMENT_WORDS,
    PARTY_ROLE_RULE,
    _PARTY_ROLE_WORD_RE,
    _PERSON_ADDRESS_WORD_RE,
    PERSON_CONTEXT_RE,
    _PLACE_NOUN_STEMS,
    _PREVIOUS_WORD_RE,
    _propagated_surname_mentions,
    _REPEAT_WINDOW,
    ROLE_PERSON_RE,
    _S_FORMS,
    _SENTENCE_OPENING_RE,
    _SIRKETI,
    _starts_a_proper_noun_or_place,
    _STI,
    _surname_mention_finding,
    _surname_of,
    _TRAILING_HONORIFICS,
    _U_FORMS,
    _UPPER_I_FORMS,
    _venue_surname_mentions,
    _words_used_as_given_names,
)
from .privacy_rules import (
    _address_trim_triggers,
    _CASE_NUMBER_RULE,
    _CONTEXT_PLACEHOLDER_PREFIXES,
    _context_segment_findings,
    _criminal_pieces,
    CRIMINAL_RULE,
    _CRIMINAL_SWEEP_RE,
    _criminal_trigger_reports,
    _cut_identifiers_from_context_findings,
    DETECTION_RULES,
    _FILE_NUMBER_RE,
    _FILE_REGISTER_RE,
    _PARCEL_LABEL,
    _privilege_pieces,
    PRIVILEGE_RULE,
    _PRIVILEGE_SWEEP_RE,
    _privilege_trigger_evidence,
    _prosecutor_file_numbers,
    _PROSECUTOR_TAIL_RE,
    _reaches_into,
    _span_reach,
    _WEAK_PRIVILEGE_MARKER_RE,
)


RISK_ORDER = {"LOW": 1, "MEDIUM": 2, "HIGH": 3, "CRITICAL": 4}
EXTERNAL_LLM_BLOCKED = "Blocked for external LLM use until redaction is completed and reviewed."
EXTERNAL_LLM_ALLOWED = "Allowed for external LLM use after completed redaction and approval."
# Every refusal this gate can reach starts with "Blocked": the dashboard's
# Blocked chip selects on that prefix (in Python, over the DERIVED readiness,
# case-insensitively -- see blueprints/documents.py), and benchmark.py and
# classify.py report on it the same way. A decision phrased any other way is
# refused by the gate and then invisible to the reviewer looking for refused
# documents. The SQL `LIKE 'Blocked%'` KPI that used to be the reason here was
# deleted: it read the cached column and nothing rendered it.
EXTERNAL_LLM_LANGUAGE_BLOCKED = (
    "Blocked for external LLM use: the detection rules are written for Turkish, "
    "so this document cannot be cleared automatically."
)

# The one language the detection rules are built for. DETECTION_RULES are
# Turkish-tuned regexes, Turkish check-digit algorithms and a Turkish name
# gazetteer; measured on a held-out synthetic corpus when this condition was
# added, Turkish entity recall was 0.934 and English 0.246, with English person
# names 0/76 and addresses 0/26. On such a document the gate is not wrong -- it
# has nothing to gate on, and a plausible English witness statement full of
# personal data cleared all seven conditions with residual risk "Low". So the
# language is a gate condition of its own: outside Turkish the product states
# the limit instead of clearing. The English rules (privacy_identifiers,
# privacy_courts) raised English recall to 0.831 and leave this unchanged:
# English person names are still 38 of 76, and nothing about an English
# document is measured for release.
SUPPORTED_DETECTION_LANGUAGE = TURKISH

_LANGUAGE_LABELS = {
    "TR": "Turkish",
    "EN": "English",
    UNKNOWN_LANGUAGE: "not identified",
}

LANGUAGE_COVERAGE_CONDITION = (
    "The detection rules are written for Turkish; coverage outside Turkish is "
    "materially lower, so this document cannot be cleared automatically."
)

_LANGUAGE_LIMIT = (
    "The detection rules are written for Turkish: Turkish-tuned patterns, Turkish check-digit "
    "algorithms and a Turkish name gazetteer. Coverage outside Turkish is materially lower, so "
    "an automated pass over this document cannot support a residual-risk level or a release "
    "decision. Few findings here do not mean low risk."
)


def language_coverage(language: str | None) -> dict:
    """State whether the detection rules cover this document's language.

    `language` is the value produced by classifier.detect_language() from the
    extracted TEXT. Anything that is not Turkish -- English, a mixed document,
    or a language that could not be identified at all -- is uncovered, and
    uncovered is treated exactly like English: refusal, not clearance. A missing
    or empty value is silence, and silence is uncovered too.
    """
    code = (language or "").strip() or UNKNOWN_LANGUAGE
    supported = code == SUPPORTED_DETECTION_LANGUAGE
    label = _LANGUAGE_LABELS.get(code, code)
    if supported:
        summary = "The detection rules are written for Turkish and this document reads as Turkish."
    elif code == UNKNOWN_LANGUAGE:
        # Reached by ordinary Turkish documents too -- a party roster, a
        # signature page, a short power of attorney -- so it says what the
        # reviewer should do rather than implying the document is at fault.
        summary = (
            "The document's language could not be identified from the extracted text, so it "
            "goes to you rather than being cleared automatically. " + _LANGUAGE_LIMIT
        )
    else:
        summary = f"This document reads as {label}, not Turkish. " + _LANGUAGE_LIMIT
    return {"language": code, "label": label, "supported": supported, "summary": summary}

# Canonical set of `privacy_findings.review_status` values. Anything else —
# including a value that merely looks plausible — must be treated as
# unresolved by the release gate: see review_gate_counts() in app.py.
#
# 'rejected' is deliberately absent. It used to carry two incompatible meanings
# the system could not tell apart, and it was split by migration 005 into:
#   'dismissed' — a false positive; the text is not sensitive and stays.
#   'retained'  — a real identifier the reviewer chose to leave unredacted.
# Nothing writes 'rejected' any more, so a row still carrying it is an
# unrecognised value, which the fail-closed logic below treats as unresolved and
# therefore blocking. That is the safe direction and it is intentional.
FINDING_REVIEW_STATUSES = {"pending", "approved", "dismissed", "retained", "added_by_reviewer"}

# The only review statuses whose sensitive text is actually removed from the
# exported document — redaction_targets_for_document() in app.py builds its
# target list from exactly these two. Every other status leaves the text in the
# output: 'dismissed'/'retained' are terminal for review purposes but the text
# stays either way, and 'pending'/NULL/an unrecognized value was never acted on
# at all.
REDACTED_REVIEW_STATUSES = {"approved", "added_by_reviewer"}

# Statuses that mean "this finding no longer counts against release", either
# because its text is gone from the export (approved/added_by_reviewer) or
# because the reviewer judged it not sensitive in the first place (dismissed).
# 'retained' is absent on purpose: the reviewer resolved it, but a real
# identifier is still sitting in the exported document. Both release-side
# measurements in review_gate_counts() (app.py) take the complement of this set,
# so they fail closed on NULL, 'pending', and any unrecognised value:
#   * the remaining-findings set feeding the residual-risk recompute, i.e.
#     unresolved ∪ retained;
#   * direct_identifiers_remaining, i.e. pending ∪ retained — a retained direct
#     identifier literally remains, which is what that gate condition asks.
RELEASE_CLEARED_REVIEW_STATUSES = {"approved", "added_by_reviewer", "dismissed"}

# Statuses that count as "the reviewer has made a decision about this finding",
# used by the workflow gates that ask whether a document is fully reviewed
# (mark_redacted in blueprints/review.py, get_exportable_docx in app.py).
# 'retained' belongs here — it is a decision, not an omission — and it is caught
# on the release side by RELEASE_CLEARED_REVIEW_STATUSES instead.
RESOLVED_REVIEW_STATUSES = {"approved", "dismissed", "retained", "added_by_reviewer"}


# Per-category explanations shown to reviewers so a flag is never a bare label.
# "basis" describes HOW it was detected; "concern" why it carries re-identification
# or legal-sensitivity risk. Checksum-validated categories say so explicitly.
CATEGORY_EXPLANATIONS: dict[str, dict[str, str]] = {
    "natural_person_name": {
        "label": "Person name",
        "basis": "Matched a name after a title (Av., Dr., Sayın, Mr, Mrs) or a party-role word (davacı, şüpheli, kiracı), a known Turkish or English given name, or a name on a labelled line, in a list or in a signature block, or the bare surname or repeat of a full name found elsewhere in the document.",
        "concern": "Names are direct identifiers and the primary re-identification vector in legal documents.",
    },
    "email_address": {
        "label": "Email address",
        "basis": "Matched an email address pattern.",
        "concern": "A direct identifier that ties the document to a specific individual or organization.",
    },
    "phone_number": {
        "label": "Phone number",
        "basis": "Matched a Turkish, UK or North American phone number pattern.",
        "concern": "A direct contact identifier for a specific person.",
    },
    "turkish_national_id": {
        "label": "Turkish national ID (TCKN)",
        "basis": "Matched an 11-digit number that PASSED the official TCKN check-digit validation.",
        "concern": "A government identifier that uniquely identifies a citizen; among the most sensitive fields.",
    },
    "iban": {
        "label": "IBAN / bank account",
        "basis": "Matched a Turkish IBAN pattern (TR + 24 digits).",
        "concern": "A financial direct identifier linking the document to a specific account holder.",
    },
    "tax_number": {
        "label": "Tax number (VKN)",
        "basis": "Matched a VKN keyword followed by a 10-digit number that PASSED VKN check-digit validation.",
        "concern": "Uniquely identifies a taxpayer or company.",
    },
    "mersis_number": {
        "label": "MERSIS number",
        "basis": "Matched a 16-digit central registry (MERSIS) number.",
        "concern": "Uniquely identifies a registered legal entity.",
    },
    "trade_registry_number": {
        "label": "Trade registry number",
        "basis": "Matched a ticaret sicil (trade registry) keyword followed by the registration number.",
        "concern": "Uniquely identifies a registered company, like its MERSIS number.",
    },
    "bar_registration_number": {
        "label": "Bar registration",
        "basis": "Matched a bar/registry number keyword followed by digits.",
        "concern": "Identifies a specific attorney of record.",
    },
    "passport_number": {
        "label": "Passport number",
        "basis": "Matched a pasaport/passport keyword followed by a letter-and-digit passport number.",
        "concern": "A government-issued identifier that uniquely identifies a person.",
    },
    "account_number": {
        "label": "Bank account number",
        "basis": "Matched a hesap no keyword, or an English account number, a/c no. or sort code label, followed by the number.",
        "concern": "A financial identifier linking the document to a specific account holder.",
    },
    "social_media_handle": {
        "label": "Social media handle",
        "basis": "Matched an @handle pattern.",
        "concern": "A public, searchable identifier that ties the document to a specific individual.",
    },
    "vehicle_plate": {
        "label": "Vehicle plate",
        "basis": "Matched a plate-number shape adjacent to a plaka/plakalı/plakası keyword.",
        "concern": "Identifies a specific registered vehicle and, through it, its owner.",
    },
    "land_registry_parcel": {
        "label": "Land registry parcel",
        "basis": "Matched a land-registry block and parcel pair (ada / parsel) with their numbers.",
        "concern": "Identifies a specific property and, through the land registry, its owner.",
    },
    "case_or_investigation_number": {
        "label": "Case / file number",
        "basis": "Matched a case-file keyword (Esas, Karar, Soruşturma, Takip, Yevmiye, UYAP) followed by a YYYY/NNN number, a notary's journal (yevmiye) number, or a labelled English claim or case reference (Claim No., Case Number, Our Ref).",
        "concern": "A contextual identifier that can re-identify the matter and its parties even without names.",
    },
    "court_or_authority": {
        "label": "Court / authority",
        "basis": "Matched a court, chamber, prosecutor's office, judgeship, notary, enforcement office, tax office or other authority name; the placeholder names which kind it is.",
        "concern": "Narrows the matter to a specific venue, aiding re-identification in combination with dates and roles.",
    },
    "party_role": {
        "label": "Party role",
        "basis": "Matched a procedural role word (davacı, davalı, şüpheli, tanık, …).",
        "concern": "Role context combined with other fields can re-identify individuals; review before releasing.",
    },
    "address": {
        "label": "Address",
        "basis": "Matched an address fragment (mahalle, cadde, sokak, apartman, ilçe) with its leading street name, or an English street line or UK postcode with the town and postcode beside it.",
        "concern": "Location data is a strong quasi-identifier.",
    },
    "company_name": {
        "label": "Company name",
        "basis": "Matched a company name ending in a legal-form suffix (A.Ş., LİMİTED ŞİRKETİ, Ltd. Şti.), a law firm name (… Hukuk Bürosu, … Avukatlık Ortaklığı), or a short name the document defines for a company.",
        "concern": "Identifies a specific legal entity that is a party to the matter.",
    },
    "date": {
        "label": "Date",
        "basis": "Matched a calendar date.",
        "concern": "Dates are quasi-identifiers; generalize unless legally necessary to keep the exact date.",
    },
    "money_amount": {
        "label": "Financial amount",
        "basis": "Matched a currency amount (TL, USD, EUR, £, …).",
        "concern": "Distinctive amounts can help re-identify a matter; keep only if legally necessary.",
    },
    "health_data": {
        "label": "Health data",
        "basis": "Matched health/medical context keywords (sağlık, hasta, tedavi, teşhis, …).",
        "concern": "Special-category personal data under KVKK; flagged for mandatory human legal review.",
    },
    "criminal_allegation": {
        "label": "Criminal allegation",
        "basis": "Matched criminal-context keywords (suç, şüpheli, sanık, soruşturma, …). The contract-law term \"cezai şart\" is a contractual penalty, not a trigger.",
        "concern": "Special-category data about alleged offences; flagged for mandatory human legal review.",
    },
    "privileged_or_confidential": {
        "label": "Attorney-client privilege",
        "basis": "Matched privilege evidence (müvekkil, attorney-client, privileged, work product, dava stratejisi), or a lawyer named (vekil, Av., avukat) with advice, an opinion or a communication in the same clause. Contractual confidentiality (gizli bilgi, ticari sır, an NDA) is not privilege and is not flagged.",
        "concern": "May be attorney-client communication, legal advice, work product or litigation strategy; flagged for mandatory human legal review.",
    },
    "manual_sensitive_text": {
        "label": "Manual sensitive text",
        "basis": "Added by a reviewer, not by an automated rule.",
        "concern": "Reviewer judged this text sensitive enough to redact.",
    },
}

RISK_MEANINGS: dict[str, str] = {
    "CRITICAL": "Direct identifier or special-category data — must be removed or replaced before any external use, and blocks the external-LLM gate until resolved.",
    "HIGH": "Strong identifier or quasi-identifier — should be pseudonymized or generalized before release.",
    "MEDIUM": "Contextual quasi-identifier — generalize unless it is legally necessary to keep the exact value.",
    "LOW": "Low individual re-identification risk on its own, but still reviewed rather than assumed safe.",
}


def explain_category(category: str) -> dict[str, str]:
    """Return {label, basis, concern} for a finding category, with a safe default."""
    return CATEGORY_EXPLANATIONS.get(
        category,
        {
            "label": category.replace("_", " ").title() if category else "Finding",
            "basis": "Detected by the privacy scanner.",
            "concern": "Review whether this value could re-identify a person or matter.",
        },
    )


def finding_explanations() -> dict[str, object]:
    """Bundle the explanation maps for the review UI (sent once per page)."""
    return {
        "categories": CATEGORY_EXPLANATIONS,
        "risks": RISK_MEANINGS,
        "direct_identifiers": sorted(DIRECT_IDENTIFIER_CATEGORIES),
    }


def _named_court_spans(text: str, claimed: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Spans matched by the court rules that begin at the venue's own name.

    Every court_or_authority rule except GENERIC_COURT_SUFFIX_RULE *starts* on
    the venue name -- its leading place word where it has one ("Ankara 11. Aile
    Mahkemesi", "Beyoğlu 24. Noterliği"), otherwise the institution itself
    ("Yargıtay"), and for TAX_OFFICE_FIELD_RULE the field's value rather than
    its label -- which is what makes it safe for the generic suffix rule to
    yield to them. The generic rule now carries a place prefix of its own, but
    that changes nothing here: _generic_court_matches compares the institution
    anchor against these spans, never the widened start. They do not all END there: the Yargıtay/Danıştay rule carries
    a `[^.\\n]{0,60}` sweep, so its span can run 60 characters past the name and
    over a following institution. That only widens the window the generic rule
    skips -- it never suppresses another name-anchored rule -- and the skipped
    text stays covered by the Yargıtay finding itself.

    The span the FINDING claims, not the raw match start. An earlier round used
    the raw start, on the premise that the two differ only over text
    _court_start() gave back because some other finding already claims it -- so
    a generic match inside that text was covered either way and the choice could
    be made on a cosmetic duplicate. The place list broke that premise: a
    captured word the list REJECTS is given back to nobody, and a raw-start
    window then hid it. Measured on "Şişli Tapu Müdürlüğü ve İstanbul Valiliği
    5. Asliye Hukuk Mahkemesi kayıtlarına işlendi", the prefix captured
    "Valiliği", the list rejected it, and the generic rule's 'Valiliği 5' was
    skipped as the named court's tail -- leaving a governorship inside no
    finding at all, which a reviewer cannot approve and the exporter therefore
    cannot remove. The trade reverses: an authority that ships unredactable
    costs more than the nested duplicate finding the trimmed start reintroduces
    ("[PERSON_1][COURT_AUTHORITY_1][COURT_AUTHORITY_2]" for one authority), and
    resolve_redaction_regions clips nested targets so the duplicate cannot
    corrupt the deliverable.
    """
    spans = []
    for rule in DETECTION_RULES:
        if rule.category != "court_or_authority" or rule is GENERIC_COURT_SUFFIX_RULE:
            continue
        for match in re.finditer(rule.pattern, text, rule.flags):
            if rule is TAX_OFFICE_FIELD_RULE:
                start = match.start("tax_office_value")
            else:
                start = _court_start(match, text, claimed) if "court_place" in match.re.groupindex else match.start()
            if _is_unplaced_tax_office(match, start):
                continue
            spans.append((start, match.end()))
    return spans


def _person_spans(text: str) -> list[tuple[str, int, int]]:
    """Every person-name span that WILL become a natural_person_name finding.

    Two paths: a name behind a professional title or a party role
    (PERSON_CONTEXT_RE / ROLE_PERSON_RE), and bare names with no prefix at all
    -- parties in running text, attendee and witness lists, signature blocks,
    labelled lines -- which the two regexes cannot see and which are the
    highest-value PII in a legal document. See legal_analyzer/turkish_names.py.

    Lifted out of analyze_privacy because _court_start() needs these spans
    BEFORE the detection-rule loop runs, and they depend on nothing but `text`.

    The address and legal-form filters are applied HERE, not at the call site,
    and that is the load-bearing part. This function used to return candidates
    and let analyze_privacy filter them afterwards, on the reasoning that "a
    span rejected there is still a span the court place prefix must not
    swallow". That reasoning was wrong, in exactly the way _named_court_spans'
    docstring already records for the place-list path: a captured word given
    back to nobody is a leak. _court_claimed_spans hands the venue's place word
    back to a person span, so when that span is then discarded the word is
    inside NO finding of any category -- unapprovable by a reviewer and
    therefore unredactable on export. Measured on the signature-block layout
    "VEKİLİ: Av." then a line break then "İstanbul Barosu Sicil No: 45678":
    PERSON_CONTEXT_RE's gap is `\\s+`, so it crossed the break and captured
    "İstanbul Barosu Sicil No",
    the bar finding was clamped to "Barosu Sicil No: 45678", and then the person
    span was dropped for containing "No" -- delivering "İstanbul
    [BAR_REGISTRATION_1]".

    One list, produced once and consumed by both the claim and the finding loop,
    is what makes "claimed but never a finding" unrepresentable. Filtering at two
    call sites would let the next filter added to one of them reopen this.
    """
    spans: list[tuple[str, int, int]] = []
    for person_re in (PERSON_CONTEXT_RE, ROLE_PERSON_RE):
        for match in person_re.finditer(text):
            value = match.group(1).strip()
            # A notarial endorsement in title case ("Noteri Tarafından
            # Onaylanmıştır") is wording, not a name. Only those endorsement
            # words reject the capture: the full NON_PERSON_TOKENS list holds
            # day names, and "Cuma" is also a given name, so testing against it
            # dropped "Noteri Cuma Pusulacı" with no finding at all.
            if text.startswith("Noteri", match.start()) and any(
                tr_fold(token) in _NOTARY_ENDORSEMENT_WORDS for token in value.split()
            ):
                continue
            # "Kiraya Veren ile İş Sahibi arasında": a role word, an optional
            # lowercase word, then a capitalised candidate that is itself a
            # defined party role. The bare-name detectors apply the same
            # whole-candidate guard where they accept a candidate.
            if is_party_role_term(value):
                continue
            spans.append((value, match.start(1), match.end(1)))
    spans.extend(detect_person_names(text))
    # The English paths add only what the Turkish ones did not already cover.
    # An English span inside an existing one is the same person read twice and
    # would key a second pseudonym; one that reaches past it is kept, because
    # the characters only it covers would otherwise ship.
    spans.extend([
        (value, start, end)
        for value, start, end in detect_english_person_names(text)
        if not any(other_start <= start and end <= other_end for _v, other_start, other_end in spans)
    ])
    return [
        (value, start, end)
        for value, start, end in spans
        if value and not _PERSON_ADDRESS_WORD_RE.search(value) and not COMPANY_SUFFIX_RE.match(text, end)
    ]


def _venue_matches(text: str) -> list[re.Match]:
    """Every match of a rule that carries the court place prefix."""
    return [
        match
        for rule in DETECTION_RULES
        if "court_place" in re.compile(rule.pattern, rule.flags).groupindex
        for match in re.finditer(rule.pattern, text, rule.flags)
    ]


def analyze_privacy(
    filename: str,
    text: str,
    extraction_warning: str | None = None,
    redaction_completed: bool = False,
    human_review_approved: bool = False,
    ocr_status: str = "not_required",
    profile: redaction_policy.Profile | None = None,
    language_text: str | None = None,
) -> dict:
    """Create a structured risk map and an anonymization-assisted preview.

    Detection, classification and policy run in that order and stay apart:
    the rules below find spans; legal_semantics types them (a penalty amount,
    a tax office, a privilege subtype) and finds the legally operative clauses
    around them; redaction_policy then attaches each finding's treatment and
    replacement for `profile` (default: the configured one). A provision is
    never a finding -- "legal_provisions" counts them by type, so the profile
    records which legal substance the document carries without storing any of
    its text.

    `language_text` is the text the language is read from, offset for offset
    with `text` (`extraction.extract_document` returns it); omitted, it is
    `text` itself.
    """
    profile = profile or redaction_policy.build_profile()
    findings = []
    placeholder_state: dict[tuple[str, str], str] = {}
    counters: Counter[str] = Counter()
    person_spans = _person_spans(text)
    venues = _venue_matches(text)
    surname_mentions = _venue_surname_mentions(text, person_spans, venues)
    claimed_spans = _court_claimed_spans(
        text, person_spans + [(word, start, end) for word, start, end, _name in surname_mentions]
    )
    named_court_spans = _named_court_spans(text, claimed_spans)
    clauses = legal_semantics.ClauseIndex(text)
    privilege_index = legal_semantics.PrivilegeIndex(text)
    context_triggers = _address_trim_triggers(text, clauses, privilege_index, person_spans)
    grouped_identities: dict[str, dict[int, tuple[str, str]]] = {}
    provisions = legal_semantics.classify_provisions(text, clauses)
    provision_index = legal_semantics.ProvisionIndex(provisions)
    typed_values: dict[tuple[str, str], list[tuple[int, str]]] = {}
    english_phone_spans = [match.span() for match in re.finditer(ENGLISH_PHONE_RULE.pattern, text, ENGLISH_PHONE_RULE.flags)]
    english_case_ids: dict[str, str] = {}
    high_court_chambers = [
        match.span("chamber")
        for match in re.finditer(HIGH_COURT_RULE.pattern, text, HIGH_COURT_RULE.flags)
        if match.group("chamber")
    ]

    for rule in DETECTION_RULES:
        if rule is GENERIC_COURT_SUFFIX_RULE:
            matches = ((match, None) for match in _generic_court_matches(text, named_court_spans))
        elif rule is PRIVILEGE_RULE:
            matches = _privilege_pieces(text, clauses, privilege_index)
        elif rule is CRIMINAL_RULE:
            matches = _criminal_pieces(text, clauses, person_spans)
        else:
            matches = ((match, None) for match in re.finditer(rule.pattern, text, rule.flags))
        for match, semantic_type in matches:
            place_end = None
            # The national-id rule matches an optional label ("T.C. Kimlik No:")
            # in front of the digits and reports only the digits as its sample,
            # so its SPAN has to be the digits as well. The sample is the
            # redaction target (redaction_targets_for_document in app.py), the
            # key the PDF exporter maps to coordinate boxes, and the needle
            # export QA searches the produced file for; build_redacted_preview
            # replaces the SPAN. A span wider than the sample therefore makes the
            # preview blank the whole labelled phrase while the export removes
            # only the digits, so the two disagree about what was removed. The
            # label "T.C. Kimlik No:" is not sensitive and the exporter already
            # leaves it, so narrowing the span is the side that makes them agree.
            if rule.category == "turkish_national_id" and match.lastindex:
                value, start, end = match.group(1).strip(), match.start(1), match.end(1)
            elif rule is ENGLISH_CASE_NUMBER_RULE:
                # The label and the identifier; see the rule's note.
                value, start, end = text[match.start():match.end("case_id")], match.start(), match.end("case_id")
            elif rule.category == "address" and "address_prefix" in match.re.groupindex:
                start, end = _address_start(match, text, context_triggers), match.end()
                value = text[start:end].strip()
            elif rule is TAX_OFFICE_FIELD_RULE:
                value, start, end = match.group("tax_office_value"), match.start("tax_office_value"), match.end("tax_office_value")
            elif "court_place" in match.re.groupindex:
                # Every court_or_authority rule but TAX_OFFICE_FIELD_RULE, and
                # the bar-registration rule, capture up to two leading place words; _court_start keeps the
                # ones turkish_places recognises and gives back the rest, plus
                # anything a person-name or party-role finding already claims.
                # Keyed on the group, not the category, so a rule that grows the
                # prefix is routed here by construction.
                start, end = _court_start(match, text, claimed_spans), match.end()
                if _is_unplaced_tax_office(match, start):
                    continue
                value = text[start:end].strip()
                place_end = match.end("court_place")
            elif rule is COMPANY_RULE:
                start, end = _company_start(match, text, person_spans), match.end()
                value = text[start:end].strip()
            elif rule.category == "trade_registry_number" and "\n" in match.group("trade_gap"):
                start, end = match.start("trade_sicil"), match.end()
                value = text[start:end].strip()
            else:
                value, start, end = match.group(0).strip(), match.start(), match.end()
            if not value:
                continue
            if rule.category == "address" and len(value) < 6:
                continue
            if rule is NUMBERED_COURT_RULE and _inside_any(place_end, end, high_court_chambers):
                # The chamber of a high court; HIGH_COURT_RULE reports the court
                # with its chamber, so this is the same text on a second card.
                continue
            if rule is TURKISH_PHONE_RULE and _inside_any(end - len(text[start:end].lstrip()), end, english_phone_spans):
                # The Turkish shape also reads a North American number
                # ("212-555-0147"), with its span starting on the space in front
                # of it. The English rule reports that number; a second finding
                # over the same digits is one number on two review cards.
                continue
            if rule.category == "date" and any(
                finding["category"] == "account_number" and finding["start"] <= start and end <= finding["end"]
                for finding in findings
            ):
                # "sort code 30-99-47" also reads as a dd-dd-dd date.
                continue
            if rule is ENGLISH_COURT_RULE and any(
                finding["category"] == "address" and finding["start"] <= start and end <= finding["end"]
                for finding in findings
            ):
                # "7 Linnet Court, Stockport SK4 2BW": a building the address
                # rule (which runs first) already reported, not a venue.
                continue
            if rule is ENGLISH_PHONE_RULE and any(
                finding["category"] == "phone_number" and finding["start"] <= start and end <= finding["end"]
                for finding in findings
            ):
                continue
            if rule.category == "turkish_national_id" and not valid_turkish_national_id(value):
                continue
            if rule.category == "tax_number" and match.lastindex and not valid_turkish_tax_number(match.group(1)):
                continue
            if rule.category == "court_or_authority":
                # The placeholder prefix is the institution's TYPE, so a tax
                # office reads [TAX_AUTHORITY_1] and a court [COURT_1] after the
                # name is gone. Typed on the raw match up to the institution
                # word -- the leading words included, since "Rekabet Kurumu"
                # and "Asliye Ticaret Mahkemesi" are typed by them -- and never
                # on a trailing sweep, which can carry another institution.
                # The rules' own COURT_AUTHORITY prefix is not used here.
                institution_end = match.end("institution") if "institution" in match.re.groupindex else end
                semantic_type = legal_semantics.institution_type(text[match.start():institution_end])
                grouped_identities.setdefault(semantic_type, {})[len(findings)] = (
                    _tax_office_field_identity(match)
                    if rule is TAX_OFFICE_FIELD_RULE
                    else _venue_identity(text, value, start, place_end)
                )
                placeholder = ""
            elif rule.category == "bar_registration_number":
                if _follows_trade_registry_label(text, match):
                    continue
                grouped_identities.setdefault(rule.placeholder_prefix, {})[len(findings)] = _bar_identity(
                    text, match.group("bar_number"), start, place_end
                )
                placeholder = ""
            elif rule.category == "money_amount":
                semantic_type = legal_semantics.amount_role(provision_index, start, end)
                typed_values.setdefault((rule.placeholder_prefix, _pseudonym_key(value)), []).append(
                    (len(findings), semantic_type)
                )
                placeholder = ""
            elif rule.category == "date":
                semantic_type = legal_semantics.date_role(text, provision_index, start, end)
                prefix = legal_semantics.BIRTH_DATE if semantic_type == legal_semantics.BIRTH_DATE else rule.placeholder_prefix
                typed_values.setdefault((rule.placeholder_prefix, _pseudonym_key(value)), []).append((len(findings), prefix))
                placeholder = ""
            elif rule is ENGLISH_CASE_NUMBER_RULE:
                case_id = match.group("case_id")
                placeholder = _placeholder_for(rule.placeholder_prefix, _case_number_key(match, case_id), counters, placeholder_state)
                english_case_ids.setdefault(case_id, placeholder)
            elif rule.category == "case_or_investigation_number":
                placeholder = _placeholder_for(rule.placeholder_prefix, _case_number_key(match, value), counters, placeholder_state)
            else:
                if rule is LAW_FIRM_RULE:
                    semantic_type = "LAW_FIRM"
                placeholder = _placeholder_for(rule.placeholder_prefix, _identifier_key(rule.category, value), counters, placeholder_state)
            findings.append(
                _finding(rule.category, value, rule.risk, rule.action, placeholder, start, end, semantic_type=semantic_type)
            )

    findings.extend(_bare_case_number_repeats(text, english_case_ids, findings))
    findings.extend(_prosecutor_file_numbers(text, findings, counters, placeholder_state))

    for placeholder_prefix, identities in grouped_identities.items():
        _assign_grouped_placeholders(placeholder_prefix, findings, identities, counters, placeholder_state)
    _assign_typed_value_placeholders(findings, typed_values, counters, placeholder_state)

    # Computed again inside _propagated_surname_mentions, with identical output.
    established = _established_surnames(text, person_spans, venues)
    for value, start, end in person_spans:
        people = established.get(tr_fold(re.split(r"['’]", value)[0])) if len(value.split()) == 1 else None
        if people:
            findings.append(_surname_mention_finding(value, start, end, _nearest_bearer(people, start), counters, placeholder_state))
            continue
        placeholder = _placeholder_for("PERSON", value, counters, placeholder_state)
        findings.append(_finding("natural_person_name", value, "HIGH", "Replace with consistent pseudonym", placeholder, start, end))
    for word, start, end, bearer in surname_mentions:
        findings.append(_surname_mention_finding(word, start, end, bearer, counters, placeholder_state))

    findings.extend(_company_alias_findings(text, findings))
    for word, start, end, bearer in _propagated_surname_mentions(text, person_spans, findings, venues):
        findings.append(_surname_mention_finding(word, start, end, bearer, counters, placeholder_state))

    findings = _cut_identifiers_from_context_findings(findings, text, counters, placeholder_state)
    findings = _dedupe_findings(findings)
    findings = redaction_policy.apply(findings, profile)
    redacted_preview = build_redacted_preview(text, findings)
    extraction_status = extraction_status_for(text, extraction_warning)
    # From the extracted TEXT, never the filename: this is the signal the
    # release gate refuses on, and a filename is not derived from the document.
    # The findings go with it so Turkish words inside an entity name -- a
    # company's legal form, a court, an address -- are not read as the
    # document's language.
    language_text = text if language_text is None else language_text
    coverage = language_coverage(detect_language(language_text, language_evidence_spans(language_text, findings)))
    residual_risk = residual_risk_assessment(findings, text, extraction_status, coverage)
    context = classify_document_context(filename, text)
    gate = external_llm_gate_policy(
        residual_risk,
        findings,
        extraction_status,
        redaction_completed=redaction_completed,
        human_review_approved=human_review_approved,
        ocr_status=ocr_status,
        coverage=coverage,
    )
    review_required = human_review_required(gate, residual_risk, findings, extraction_status, context, coverage)

    return {
        "terminology": TERMINOLOGY,
        "positioning": OUTPUT_POSITIONING,
        "extraction_status": extraction_status,
        "language_coverage": coverage,
        "document_context": context,
        "risk_map": findings,
        "legal_provisions": legal_semantics.provision_summary(provisions),
        "redaction_policy": profile.summary(),
        "recommended_strategy": recommended_strategy(findings, context),
        "redacted_preview": redacted_preview,
        "residual_risk": residual_risk,
        "external_llm_gate": gate,
        "external_llm_readiness": gate["decision"],
        "redaction_status": redaction_status(findings, extraction_status, redaction_completed, ocr_status),
        "human_review_required": review_required,
        "release_controls": {
            "redaction_completed": redaction_completed,
            "human_review_approved": human_review_approved,
        },
        "llm_ingestion": llm_ingestion_recommendation(gate, residual_risk, findings, extraction_status, ocr_status),
        "human_review_checklist": human_review_checklist(context, findings, extraction_status, gate, ocr_status, coverage),
    }


def _dedupe_findings(findings: list[dict]) -> list[dict]:
    seen = set()
    unique = []
    for item in sorted(findings, key=lambda f: (f["start"], -(f["end"] - f["start"]))):
        key = (item["category"], item["start"], item["end"], item["fingerprint"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return unique


_MAX_RISK_ORDER = max(RISK_ORDER.values()) + 1


def build_redacted_preview(text: str, findings: list[dict], max_chars: int = 12_000) -> str:
    """Build a pseudonymized/redacted preview from disjoint finding regions.

    At a given start offset, prefer the highest-risk finding, then the widest
    span. Preferring width first let a wide low-risk span (e.g. the old,
    now-removed wide party_role span) outrank a CRITICAL identifier nested
    inside it, so the CRITICAL finding never reached the preview. An
    unrecognised risk value sorts as more severe than CRITICAL, consistent
    with the fail-closed handling of unresolved review statuses elsewhere.

    A finding that PARTIALLY overlaps one already chosen is CLIPPED, not
    dropped -- the same rule, for the same reason, as collect_replacements in
    legal_analyzer/docx_redactor.py. Real findings cross: an over-matched name
    span running into a label ("Leyla Kaya Vergi") and the tax_number that
    starts inside it ("Vergi Kimlik No: 0983930103") share four characters and
    neither contains the other. Dropping the loser wholesale rendered the part
    the winner does not cover as RAW document text, so the national/tax id was
    printed in full in a preview that is persisted in documents.privacy_profile
    and served as "PRIVACY-REVIEWED REDACTED EXPORT". The wide context spans
    used to hide this by covering the remainder; cutting identifiers out of
    those spans removed that accidental cover.

    The one deliberate difference from collect_replacements is the risk term in
    the sort key, which the DOCX path does not have: sorting on width alone, it
    always picks the widest finding at a tied start, while here the narrower,
    higher-risk one can win. That decides only WHICH finding is chosen. The
    loser is then clipped to its residual exactly like any other partial
    overlap -- a tied start is not a special case.

    Skipping the tied-start loser instead looked safe and was not. A CRITICAL
    context finding and a WIDER address can begin at the same offset, because
    a capitalised trigger word doubles as the start of a street name: in
    "Sağlık Caddesi No: 5 Daire: 12. Kat Kadıköy İstanbul adresinde oturuyor"
    health_data wins the tie at offset 0 and its sweep stops at the first
    period, so skipping the address printed ". Kat Kadıköy İstanbul adresinde
    oturuyor" -- house number, apartment and district -- in full. address is a
    direct identifier and this text is served as the redacted export.

    A finding that ends inside the chosen region contributes nothing, which is
    correct: every character of it is already being replaced.

    The max() that clips the start is deliberately kept although it cannot
    change this function's output: the emitted `cursor` is always the previous
    chosen finding's end, i.e. exactly the `last_end` that clipped the current
    one, so text[cursor:start] is already empty when start < cursor. A
    max->plain-start mutation is therefore an equivalent mutant (verified:
    identical previews on 10,018 texts, all fixtures included), the same status
    as the min->max choice in _enclosing_span_end. It stays because it makes
    `chosen` a correct list of disjoint regions for any future reader, instead
    of leaning on an inverted slice silently returning "".
    """
    chosen: list[tuple[int, dict]] = []
    last_end = 0
    for item in sorted(
        findings,
        key=lambda f: (f["start"], -RISK_ORDER.get(f["risk"], _MAX_RISK_ORDER), -(f["end"] - f["start"])),
    ):
        if item["end"] <= last_end:
            continue
        chosen.append((max(item["start"], last_end), item))
        last_end = item["end"]

    parts = []
    cursor = 0
    for start, item in chosen:
        parts.append(text[cursor:start])
        parts.append(redaction_policy.replacement_for_finding(item))
        cursor = item["end"]
    parts.append(text[cursor:])
    return "".join(parts)[:max_chars]


def recommended_strategy(findings: list[dict], context: dict) -> str:
    if any(f["risk"] == "CRITICAL" for f in findings) or context.get("appears_privileged_or_confidential"):
        return "Privacy-reviewed de-identification with targeted redaction, consistent pseudonymization, and mandatory human legal review."
    if any(f["risk"] == "HIGH" for f in findings):
        return "De-identification with consistent pseudonymization of direct identifiers and review of contextual identifiers."
    return "Light de-identification review; keep legal meaning while checking contextual re-identification risk."


def extraction_status_for(text: str, warning: str | None) -> dict:
    if warning and text:
        return {
            "status": "Partial",
            "warning": warning,
            "blocks_external_llm": True,
            "summary": "Text extraction produced a warning. Treat this document as not safe until OCR or manual review confirms coverage.",
        }
    if warning or not text.strip():
        return {
            "status": "Failed",
            "warning": warning or "No extracted text was available.",
            "blocks_external_llm": True,
            "summary": "No reliable text was extracted. Not extracted does not mean no sensitive data.",
        }
    return {
        "status": "Complete",
        "warning": None,
        "blocks_external_llm": False,
        "summary": "Text extraction completed for the automated pass.",
    }


def residual_risk_assessment(
    findings: list[dict],
    text: str,
    extraction_status: dict,
    coverage: dict | None = None,
) -> dict:
    """Measure residual risk, or refuse to.

    `coverage` is language_coverage() for this document. It defaults to the
    unsupported answer on purpose: a caller that does not say what language the
    document is in has not established that the rules could read it, and the
    level this function returns is the one the release gate opens on. Silence
    fails toward refusal.
    """
    coverage = coverage or language_coverage(None)
    if extraction_status["status"] in {"Partial", "Failed"}:
        return {
            "level": "Unknown",
            "summary": f"{extraction_status['summary']} Legal anonymization remains context-dependent and requires review before LLM use.",
            "not_fully_anonymous": True,
            "extraction_gated": True,
        }

    # A Low level here is a claim that an automated pass measured this document.
    # Outside Turkish the pass did not read it, so there is no level to report
    # and none is invented -- the honest answer is that it is not known.
    if not coverage["supported"]:
        return {
            "level": "Unknown",
            "summary": coverage["summary"],
            "not_fully_anonymous": True,
            "extraction_gated": False,
        }

    max_risk = max((RISK_ORDER.get(f["risk"], 1) for f in findings), default=1)
    contextual_signals = sum(token in text.lower() for token in ["adres", "mahkemesi", "savcılığı", "soruşturma", "marka", "ek-", "tarih"])
    if max_risk >= 4 or contextual_signals >= 3:
        level = "High"
    elif max_risk == 3 or contextual_signals:
        level = "Medium"
    else:
        level = "Low"
    return {
        "level": level,
        "summary": "Legal anonymization is context-dependent; factual patterns, dates, authorities, case numbers, party roles, locations, and transaction context may still permit re-identification.",
        "not_fully_anonymous": True,
        "extraction_gated": False,
    }


def remaining_findings_residual_risk(
    remaining_findings: list[dict],
    extraction_status: dict,
    coverage: dict | None = None,
) -> dict:
    """Residual risk over only the sensitive text that still remains in the output.

    `remaining_findings` is every finding whose review_status is NOT in
    RELEASE_CLEARED_REVIEW_STATUSES, i.e. (unresolved findings) ∪ (retained
    findings). A retained CRITICAL national id is resolved for review purposes
    but is still in the document, so it must keep the residual level above Low.
    A dismissed finding is excluded: the reviewer judged it a false positive, so
    its text remaining in the output is not residual risk at all — including it
    would put the release gate back out of reach for every document with a
    CRITICAL-risk false positive, which is the dead end this split exists to
    remove.

    The contextual-signal half of residual_risk_assessment() is measured over
    the remaining findings' samples rather than the full source text. At
    detection time those tokens ("adres", "mahkemesi", "tarih", …) are a proxy
    for "this document contains quasi-identifiers we may not have caught"; once
    every finding has been adjudicated, the finding set is the direct evidence
    and the bare label words that survive redaction are not identifiers. Scoring
    the source text here would also leave the gate permanently closed on any
    document containing the word "tarih", with no action a reviewer could take
    to satisfy it.
    """
    remaining_text = "\n".join(finding.get("sample") or "" for finding in remaining_findings)
    return residual_risk_assessment(remaining_findings, remaining_text, extraction_status, coverage)


def has_direct_identifiers(findings: list[dict]) -> bool:
    return any(f["category"] in DIRECT_IDENTIFIER_CATEGORIES for f in findings)


def external_llm_gate_policy(
    residual_risk: dict,
    findings: list[dict],
    extraction_status: dict,
    redaction_completed: bool = False,
    human_review_approved: bool = False,
    unresolved_critical_count: int | None = None,
    direct_identifiers_remaining: bool | None = None,
    ocr_status: str = "not_required",
    coverage: dict | None = None,
) -> dict:
    # Defaults to the unsupported answer for the same reason
    # residual_risk_assessment() does: a caller that does not say what language
    # the document is in has not established that the rules could read it.
    coverage = coverage or language_coverage(None)
    critical_count = unresolved_critical_count
    if critical_count is None:
        critical_count = sum(1 for f in findings if f["risk"] == "CRITICAL")
    direct_identifiers_detected = direct_identifiers_remaining
    if direct_identifiers_detected is None:
        direct_identifiers_detected = has_direct_identifiers(findings)
    ocr_state = ocr_status or "not_required"
    # Zero findings from rules that cannot read the document's language is
    # silence, not a clean result: an English settlement agreement naming three
    # people at two street addresses scored "clear" and "none remain" here, two
    # green ticks beside this gate's own refusal. So on an uncovered document
    # neither count may pass, the way residual risk reports "currently Unknown"
    # rather than Low. A count the rules did reach still stands -- it is a floor.
    assessable = bool(coverage["supported"])
    not_assessable = "not assessable — language not covered"
    # The one place the release conditions are stated. `failed_conditions` and
    # `policy` are both derived from it, and the studio renders the whole list
    # from `conditions` rather than rebuilding a shorter approximation of it in
    # the browser -- which is what put five browser-computed green ticks
    # directly above this gate's red "Blocked". Order is load-bearing:
    # `failed_conditions` preserves the sequence callers and tests already read.
    conditions = [
        {
            "id": "extraction_complete",
            "label": "Extraction complete",
            "policy": "Extraction status = Complete",
            "message": "Extraction status must be Complete.",
            "passed": extraction_status["status"] == "Complete",
            "detail": str(extraction_status.get("status") or "Unknown"),
        },
        {
            "id": "ocr_resolved",
            "label": "OCR accepted or not required",
            "policy": "OCR accepted or not required",
            "message": "OCR output must be accepted or not required.",
            "passed": not ocr_blocks_release(ocr_status),
            "detail": ocr_state,
        },
        # Third, with extraction and OCR, because all three answer the same
        # question: could the automated pass read this document at all? It is a
        # condition of its own rather than a notice, because the measured
        # failure was an English witness statement -- two names, an address, a
        # mobile number, a date of birth, a national insurance number and a
        # health disclosure -- producing zero findings, residual risk Low and
        # all seven conditions green. A warning in the presentation layer does
        # not refuse anything, and the one that existed was hidden on 18 of 20
        # English documents because the language came from the filename.
        {
            "id": "language_supported",
            "label": "Document language is covered by the detection rules",
            "policy": "Document language is covered by the detection rules (Turkish)",
            "message": LANGUAGE_COVERAGE_CONDITION,
            "passed": bool(coverage["supported"]),
            "detail": coverage["label"],
        },
        {
            "id": "residual_risk_low",
            "label": "Residual risk after review is Low",
            "policy": "Residual risk = Low",
            "message": "Residual risk must be Low.",
            "passed": residual_risk["level"] == "Low",
            "detail": f"currently {residual_risk.get('level') or 'Unknown'}",
        },
        {
            "id": "no_critical_findings",
            "label": "No undecided critical findings",
            "policy": "Critical findings = 0",
            "message": (
                "Critical findings must be zero." if critical_count or assessable
                else "Critical findings cannot be assessed: document language is not covered."
            ),
            "passed": assessable and not critical_count,
            "detail": (
                f"{critical_count} outstanding" if critical_count
                else "clear" if assessable
                else not_assessable
            ),
        },
        {
            "id": "no_direct_identifiers",
            "label": "No direct identifiers remain",
            "policy": "No direct identifiers remain",
            "message": (
                "No direct identifiers may remain in detected findings."
                if direct_identifiers_detected or assessable
                else "Direct identifiers cannot be assessed: document language is not covered."
            ),
            "passed": assessable and not direct_identifiers_detected,
            "detail": (
                "at least one remains" if direct_identifiers_detected
                else "none remain" if assessable
                else not_assessable
            ),
        },
        {
            "id": "redaction_completed",
            "label": "Redaction pass completed",
            "policy": "Redaction pass completed",
            "message": "Redaction pass must be completed.",
            "passed": bool(redaction_completed),
            "detail": "done" if redaction_completed else "not marked complete",
        },
        # Deliberately unconditional. There was an `or auto_mode_enabled` arm here
        # that substituted a stored flag for a person's approval; it had no UI, no
        # indicator and no audit surface, and it could only ever let a document out
        # that nobody had approved. It was removed rather than made visible. An
        # automated or batch mode, if it is ever wanted, is a feature to design with
        # its own visible switch and audit trail -- not an arm on this condition.
        {
            "id": "human_review_approved",
            "label": "Human review approved",
            "policy": "Human review approved",
            "message": "Human review approval is required.",
            "passed": bool(human_review_approved),
            "detail": "approved" if human_review_approved else "not approved",
        },
    ]
    failed_conditions = [condition["message"] for condition in conditions if not condition["passed"]]

    allowed = not failed_conditions
    if allowed:
        decision = EXTERNAL_LLM_ALLOWED
    elif extraction_status["blocks_external_llm"] or ocr_blocks_release(ocr_status):
        # Ahead of the language branch deliberately: with no reliable text there
        # is no language answer to report either, and OCR is the step that can
        # still change both.
        decision = "Blocked until OCR/manual review"
    elif not coverage["supported"]:
        decision = EXTERNAL_LLM_LANGUAGE_BLOCKED
    else:
        decision = _review_refusal(conditions, critical_count, residual_risk)

    return {
        "allowed": allowed,
        "decision": decision,
        "failed_conditions": failed_conditions,
        "critical_count": critical_count,
        "direct_identifiers_detected": direct_identifiers_detected,
        "conditions": conditions,
        "policy": [condition["policy"] for condition in conditions],
        "language_coverage": coverage,
    }


# The gate conditions a finished review is measured against: what the
# reviewer's decisions left in the document. legal_analyzer/status.py shows the
# gate's decision in place of its first failed condition when only these fail.
RELEASE_OUTCOME_CONDITIONS = frozenset({"residual_risk_low", "no_critical_findings", "no_direct_identifiers"})


def _review_refusal(conditions: list[dict], critical_count: int, residual_risk: dict) -> str:
    """The refusal once the text is readable: name what still stands in the way.

    This used to be one string, "Blocked until redaction is completed and
    reviewed", for every refusal here -- including a document whose reviewer
    had completed the redaction, approved it and deliberately kept an
    identifier, who was then told to do what they had already done. While the
    review is still open its steps are the next action, and the outcome
    conditions are re-measured by the decisions it makes, so the review is
    named; once it is finished, the conditions its decisions left unmet are.
    """
    failed = {condition["id"] for condition in conditions if not condition["passed"]}
    redaction_pending = "redaction_completed" in failed
    approval_pending = "human_review_approved" in failed
    if redaction_pending and approval_pending:
        return "Blocked until redaction is completed and reviewed"
    if redaction_pending:
        return "Blocked until redaction is completed"
    if approval_pending:
        return "Blocked until human review is approved"
    # Cause before consequence: a kept identifier is what holds residual risk up.
    reasons = []
    if "no_critical_findings" in failed:
        reasons.append(f"{critical_count} critical finding{'s' if critical_count != 1 else ''} still undecided")
    if "no_direct_identifiers" in failed:
        reasons.append("direct identifiers remain after review")
    if "residual_risk_low" in failed:
        reasons.append(f"residual risk after review is {residual_risk.get('level') or 'Unknown'}, not Low")
    # Anything else reaching here is reported in the gate's own words rather
    # than dropped from the refusal.
    reasons += [
        condition["message"].rstrip(".")
        for condition in conditions
        if not condition["passed"] and condition["id"] not in RELEASE_OUTCOME_CONDITIONS
    ]
    return f"Blocked: {'; '.join(reasons)}."


def ocr_blocks_release(ocr_status: str | None) -> bool:
    return (ocr_status or "not_required") not in {"not_required", "accepted"}


def redaction_status(
    findings: list[dict],
    extraction_status: dict,
    redaction_completed: bool = False,
    ocr_status: str = "not_required",
) -> str:
    if extraction_status["blocks_external_llm"] or ocr_blocks_release(ocr_status):
        return "Not started - extraction review required"
    if redaction_completed:
        return "Redaction pass completed - approval still required before release"
    if findings:
        return "Detected findings only - not redacted successfully"
    return "No automated findings - not a guarantee of de-identification"


def human_review_required(
    gate: dict,
    residual_risk: dict,
    findings: list[dict],
    extraction_status: dict,
    context: dict,
    coverage: dict | None = None,
) -> bool:
    coverage = coverage or language_coverage(None)
    return bool(
        not gate["allowed"]
        # Stated on its own rather than left to fall out of the Unknown residual
        # level above: on an uncovered document the reviewer is the only reader,
        # and this must not become False again if the risk arithmetic changes.
        or not coverage["supported"]
        or extraction_status["blocks_external_llm"]
        or residual_risk["level"] in {"Unknown", "High", "Medium"}
        or any(f["risk"] in {"HIGH", "CRITICAL"} for f in findings)
        or context.get("appears_privileged_or_confidential")
    )


def llm_ingestion_recommendation(
    gate: dict,
    residual_risk: dict,
    findings: list[dict],
    extraction_status: dict,
    ocr_status: str = "not_required",
) -> dict:
    level = residual_risk["level"]
    has_critical = any(f["risk"] == "CRITICAL" for f in findings)
    external = EXTERNAL_LLM_ALLOWED if gate["allowed"] else EXTERNAL_LLM_BLOCKED
    internal = "High - human review required" if level in {"Unknown", "High"} or has_critical else "Medium - review recommended"
    return {
        "external_hosted_llm_api": external,
        "internal_private_llm": internal,
        "embedding_model": external,
        "vector_database": external,
        "evaluation_dataset": EXTERNAL_LLM_BLOCKED
        if has_critical or extraction_status["blocks_external_llm"] or ocr_blocks_release(ocr_status)
        else "Needs review before use",
        "fine_tuning_dataset": EXTERNAL_LLM_BLOCKED,
    }


def refresh_release_state(
    privacy_profile: dict,
    redaction_completed: bool = False,
    human_review_approved: bool = False,
    unresolved_critical_count: int | None = None,
    direct_identifiers_remaining: bool | None = None,
    remaining_findings: list[dict] | None = None,
    ocr_status: str = "not_required",
) -> dict:
    """Re-evaluate release controls without rerunning detection.

    `remaining_findings` carries the review outcome the stored profile cannot
    know: the findings whose sensitive text is still in the document (see
    remaining_findings_residual_risk). When it is supplied, residual risk is
    recomputed from it — reusing the stored, detection-time value instead froze
    residual risk at its pre-review level and made the gate's "Residual risk
    must be Low" condition unsatisfiable for every document that had any
    CRITICAL finding. An empty list is a valid answer ("nothing remains"); only
    None means the caller could not supply the data, and then the stored value
    is kept rather than assumed Low.
    """
    findings = privacy_profile.get("risk_map", [])
    # `language_coverage` on the profile is the DETECTION-TIME answer, and this
    # function never sees the document text, so it cannot produce one. A profile
    # written before the language became a gate condition has no such key, and
    # that is silence, not Turkish: the gate is decided on uncovered and the
    # document stays blocked. The decision is recorded on the gate it decided,
    # never written back onto the profile as though detection had produced it --
    # doing that made an absent answer indistinguishable from a detected Unknown
    # and stranded every legacy document that any review, OCR or PDF write path
    # touched before db/backfill_language_coverage.py could claim it.
    coverage = privacy_profile.get("language_coverage") or language_coverage(None)
    extraction_status = privacy_profile.get("extraction_status", extraction_status_for("", "Missing extraction status."))
    residual_risk = privacy_profile.get(
        "residual_risk",
        {
            "level": "Unknown",
            "summary": "Residual risk was not available.",
            "not_fully_anonymous": True,
            "extraction_gated": True,
        },
    )
    if remaining_findings is not None:
        residual_risk = remaining_findings_residual_risk(remaining_findings, extraction_status, coverage)
    # A stored profile can predate any given key -- a skeletal one carries no
    # document_context at all -- and this function is now called for every row
    # the dashboard lists, not only for profiles detection wrote in full.
    context = privacy_profile.get("document_context") or {}
    gate = external_llm_gate_policy(
        residual_risk,
        findings,
        extraction_status,
        redaction_completed=redaction_completed,
        human_review_approved=human_review_approved,
        unresolved_critical_count=unresolved_critical_count,
        direct_identifiers_remaining=direct_identifiers_remaining,
        ocr_status=ocr_status,
        coverage=coverage,
    )

    privacy_profile["external_llm_gate"] = gate
    privacy_profile["external_llm_readiness"] = gate["decision"]
    # The level the gate was actually decided against, recorded separately so
    # that "residual_risk" stays the detection-time measurement the accuracy
    # audit and the risk badges are calibrated on.
    privacy_profile["post_review_residual_risk"] = residual_risk
    privacy_profile["redaction_status"] = redaction_status(findings, extraction_status, redaction_completed, ocr_status)
    privacy_profile["human_review_required"] = human_review_required(gate, residual_risk, findings, extraction_status, context, coverage)
    privacy_profile["release_controls"] = {
        "redaction_completed": redaction_completed,
        "human_review_approved": human_review_approved,
    }
    privacy_profile["llm_ingestion"] = llm_ingestion_recommendation(gate, residual_risk, findings, extraction_status, ocr_status)
    privacy_profile["human_review_checklist"] = human_review_checklist(context, findings, extraction_status, gate, ocr_status, coverage)
    return privacy_profile


def human_review_checklist(
    context: dict,
    findings: list[dict],
    extraction_status: dict,
    gate: dict,
    ocr_status: str = "not_required",
    coverage: dict | None = None,
) -> list[str]:
    checklist = [
        "Confirm that the output is described as de-identified, redacted, pseudonymized, privacy-reviewed, or anonymization-assisted rather than fully anonymous.",
        "Confirm extraction status before relying on the scanner: extraction warning or failure means the document is blocked for external LLM use.",
        "Separate detected findings from redaction success; this automated pass does not prove the document has been safely redacted.",
        "Do not allow external LLM use until every external LLM gate condition is satisfied.",
        "Review whether dates, authorities, case numbers, party roles, locations, and factual patterns still identify a person, client, transaction, or matter.",
        "Confirm whether a mapping table exists and restrict access to authorized reviewers only.",
    ]
    uncovered = bool(coverage) and not coverage["supported"]
    for condition in gate["failed_conditions"]:
        line = f"Resolve gate condition: {condition}"
        if uncovered and condition == LANGUAGE_COVERAGE_CONDITION:
            line += " Read this document yourself and record every item the rules did not reach."
        checklist.append(line)
    if extraction_status["blocks_external_llm"]:
        checklist.append("Run OCR or manual review for unextracted/partially extracted content, including scanned pages, signatures, stamps, ID cards, barcodes, handwritten notes, and exhibits.")
    if ocr_blocks_release(ocr_status):
        checklist.append("Review and accept OCR output before it can affect findings, redaction completion, or external LLM readiness.")
    # A context stored before the two signals were split carries only the
    # union, and keeps the line it always produced.
    privileged = context.get("appears_privileged", context.get("appears_privileged_or_confidential"))
    if privileged:
        checklist.append("Review attorney-client privilege, professional secrecy, and client confidentiality separately from personal data risk.")
    if context.get("appears_commercially_confidential"):
        checklist.append(
            "Contractual confidentiality is not attorney-client privilege: keep confidentiality, trade-secret and "
            "penalty clauses readable and redact only the identifiers and values inside them."
        )
    if any(f["risk"] == "CRITICAL" for f in findings):
        checklist.append("Manually approve all critical-risk transformations before external LLM use.")
    return checklist
