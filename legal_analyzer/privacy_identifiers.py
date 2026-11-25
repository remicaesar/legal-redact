"""Identifiers: phone numbers, dates, amounts, case and claim numbers,
accounts and addresses, and how each is keyed to one pseudonym.

The English rules other than the court rule (which is in ``privacy_courts``)
start here, after the section note that covers them all. Moved out of
``privacy.py`` unchanged; that module re-exports every name here.
"""

from __future__ import annotations

import re
from collections import Counter

from legal_analyzer import legal_semantics

from .privacy_common import (
    DetectionRule,
    _finding,
    _placeholder_for,
)


# --- English rules --------------------------------------------------------
#
# The rules below read English documents: UK and North American phone numbers,
# English dates, sterling and dollar amounts, English and American courts,
# claim and case numbers, and UK and US postal addresses. The court rule is in
# privacy_courts; English person names are legal_analyzer/english_names.py,
# called from _person_spans.
#
# They run on EVERY document, Turkish included, because the documents that
# need them most are mixed ones -- a Turkish filing with an English witness
# annex, a bilingual contract -- and a rule gated on the document's language
# would go quiet exactly there. So each one anchors on structure that does not
# occur in Turkish: a UK or NANP number plan, an English month name, a £ sign,
# the English words Court and Tribunal, a "Claim No." label, an English street
# type or a UK postcode. tests/test_english_detection.py pins that the Turkish
# fixtures gain no finding from them.
#
# None of this touches the language gate. A document that does not read as
# Turkish is still refused clearance (language_coverage); these rules give the
# reviewer findings to approve on that document instead of nothing.
#
# Each English rule is case-SENSITIVE (flags=re.UNICODE) where a capital
# letter is the evidence -- a court's name, a street name, a postcode -- and
# spells the all-caps heading form beside the title-case one, the same
# criterion _COMPANY_SUFFIXES applies.


def _title_and_caps(*words: str) -> str:
    """'Street' -> 'Street|STREET', for the case-sensitive English rules."""
    return "|".join(form for word in words for form in (word, word.upper()))


# A capitalised English word, ending on a word boundary so "LS1" in a postcode
# is not read as the word "LS".
_EN_CAP_WORD = r"[A-Z][A-Za-z'’\-]*(?![\w'’])"

# Named so analyze_privacy can let it yield to ENGLISH_PHONE_RULE: its shape
# also reads a North American number, starting its span on the space before.
TURKISH_PHONE_RULE = DetectionRule("phone_number", r"(?<!\d)(?:\+90|0)?\s?(?:5\d{2}|2\d{2}|3\d{2}|4\d{2})[\s.-]?\d{3}[\s.-]?\d{2}[\s.-]?\d{2}(?!\d)", "HIGH", "Replace with consistent pseudonym", "PHONE")

# UK numbers, national or +44 form: the national number after the trunk 0 is
# 10 digits (9 for a few old exchanges) grouped 2-4-4 (020 7946 0123), 3-3-4
# (0161 496 0000, 0300 …), 4-6 (07700 900123, 01632 960123), 4-3-3 (07700 900
# 123) or 4-5. The first digit after the trunk is 1, 2, 3, 7, 8 or 9 -- never
# 5 or 4, which keeps every Turkish mobile (05xx) and every Turkish landline
# grouped xx xx out of it; a Turkish number written without spaces matches
# both rules with the same span and is deduplicated. North American numbers
# are (NXX) NXX-XXXX or NXX-NXX-XXXX with an optional +1 -- a shape that also
# reads the tail of a Turkish number grouped 3-3-4 ("+90 5xx xxx xxxx"), which
# analyze_privacy drops in favour of the Turkish finding around it.
_UK_PHONE = (
    r"(?:\+44[ \t]?(?:\(0\)[ \t]?)?|\(?0)"
    r"(?:2\d\)?[ \t-]?\d{4}[ \t-]?\d{4}"
    r"|[1389]\d{2}\)?[ \t-]?\d{3}[ \t-]?\d{4}"
    r"|[17]\d{3}\)?[ \t-]?(?:\d{6}|\d{3}[ \t-]\d{3}|\d{5}))"
)
_NANP_PHONE = r"(?:\+1[ \t.-]?)?(?:\([2-9]\d{2}\)[ \t]?|[2-9]\d{2}[ \t.-])[2-9]\d{2}[ \t.-]\d{4}"
ENGLISH_PHONE_RULE = DetectionRule(
    "phone_number",
    r"(?<![\d+])(?:" + _UK_PHONE + "|" + _NANP_PHONE + r")(?!\d)",
    "HIGH",
    "Replace with consistent pseudonym",
    "PHONE",
)

# "12 March 2025", "12th March, 2025", "the 3rd (day) of April 2025", "March 12,
# 2025", "Sept. 9 2025". A day and a year are both required: a bare "March
# 2025" narrows nothing to a person, and requiring both is what keeps "may" and
# "march" the verbs out. [ \t] between the parts, never \s: a finding sample
# carrying a newline cannot be removed from a DOCX.
_EN_MONTH = (
    r"(?:January|February|March|April|May|June|July|August|September|October|November|December"
    r"|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sept|Sep|Oct|Nov|Dec)\.?"
)
_EN_ORDINAL_DAY = r"\d{1,2}(?:st|nd|rd|th)?"
ENGLISH_DATE_RULE = DetectionRule(
    "date",
    r"\b(?:" + _EN_ORDINAL_DAY + r"(?:[ \t]+(?:day[ \t]+)?of)?[ \t]+" + _EN_MONTH + r",?[ \t]+\d{4}"
    r"|" + _EN_MONTH + r"[ \t]+" + _EN_ORDINAL_DAY + r",?[ \t]+\d{4})\b",
    "MEDIUM",
    "Generalize unless legally necessary",
    "DATE",
)

# A claim or case number behind its English label: "Claim No. HC-2024-001234",
# "Claim Number: J01AB123", "Case No: 2201234/2025", "Appeal No. CA-2025-
# 000123", "Civil Action No. 1:24-cv-01234", "Index No. 654321/2025", "Our
# Ref: ABC/123/2025". The label must carry its marker -- No., Number, # or a
# colon -- so "in this case 2025 was" is not a case number, and the identifier
# must contain a digit and run at least four characters.
#
# The SPAN keeps its label, as the Turkish rule's does ("Esas No: 2025/318"):
# the label names the register, and the reference is the pair. The pseudonym
# is keyed on the identifier alone, so "Claim No." and "Case Number:" in front
# of the same one give one placeholder. The export still removes, and the
# output scan still searches for, the bare identifier everywhere
# (finding_needles); a bare repeat later in the letter ("… in claim
# HC-2024-001234") is also reported by _bare_case_number_repeats as its own
# finding under the same placeholder, so the reviewer sees it.
ENGLISH_CASE_NUMBER_RULE = DetectionRule(
    "case_or_investigation_number",
    r"\b(?:claim|case|appeal|(?:civil[ \t]+)?action|index|docket|cause|matter|file"
    r"|(?:court|our|your|matter|client)[ \t]+ref(?:erence)?|ref(?:erence)?)"
    r"[ \t]*(?:(?:no|nos|number)\.?[ \t]*[:.]?|#|:)[ \t]*"
    r"(?P<case_id>(?=[A-Za-z0-9/.:\-]*\d)(?=[A-Za-z0-9/.:\-]{4})[A-Za-z0-9](?:[A-Za-z0-9/.:\-]*[A-Za-z0-9])?)",
    "HIGH",
    "Replace with consistent pseudonym",
    "CASE_NUMBER",
)

# A UK bank account behind its label: "sort code 30-99-47", "Account No.
# 41095528", "a/c no. 41095528", "bank account number: 0041095528". The span
# keeps its label, as the Turkish "hesap no" rule's does; _identifier_key keys
# an account_number on its digits, so the same account under two labels is
# one pseudonym. A sort code is reported as account_number too: it is half of
# the pair that names the account. The label is required -- eight bare digits
# are anything -- and so are at least six digits after it, which keeps "take
# into account number 4" and "the accounts for 2025" out. Gaps are [ \t]:
# a sample carrying a line break cannot be removed from a DOCX.
ENGLISH_ACCOUNT_RULE = DetectionRule(
    "account_number",
    r"\bsort[ \t]+code[ \t]*(?:no\.?|number)?[ \t]*[:.]?[ \t]*(?P<sort_code>\d{2}(?P<sort_gap>[ \t-]?)\d{2}(?P=sort_gap)\d{2})(?![\d-])"
    r"|(?:\b(?:bank[ \t]+)?account|\ba/c)[ \t]*(?:no\.?|nos\.?|number|num\.?|#)?[ \t]*[:.]?[ \t]*(?P<account_id>\d{6,17})(?![\d-])",
    "HIGH",
    "Mask partially",
    "ACCOUNT_NUMBER",
)


# A labelled identifier's finding keeps its label in the sample, but the
# identifier is what must leave the deliverable wherever it stands: in a file
# name ("KB-2026-000418.pdf"), a URL, a hyperlink target, document metadata, or
# a repeat no finding was made for. So each of these rules hands every
# consumer that removes or scans for an approved sample -- the redaction plan,
# the export targets, the PDF box suggestions and the output scan -- the bare
# identifier as a second substring needle, through finding_needles().
_LABELLED_IDENTIFIER_GROUPS = (
    (ENGLISH_CASE_NUMBER_RULE, ("case_id",)),
    (ENGLISH_ACCOUNT_RULE, ("sort_code", "account_id")),
)


def finding_needles(category: str | None, sample: str | None) -> tuple[str, ...]:
    """Every text an approved finding removes from, and is scanned for in, an export.

    The sample itself, and -- when the sample is a labelled English case or
    account number -- its bare identifier as well.
    """
    sample = sample or ""
    if not sample:
        return ()
    for rule, groups in _LABELLED_IDENTIFIER_GROUPS:
        if category != rule.category:
            continue
        match = re.fullmatch(rule.pattern, sample.strip(), rule.flags)
        if match is None:
            continue
        identifier = next((match.group(group) for group in groups if match.group(group)), "")
        if identifier and identifier != sample:
            return (sample, identifier)
    return (sample,)

# A UK or US postal address. Two shapes, one rule, so a line is reported once:
#
#   * a STREET LINE -- an optional flat/unit/floor, a house number, one to
#     three capitalised words and an English street type ("Flat 3, 12 Rose
#     Lane", "221B Baker Street", "350 Fifth Avenue") -- followed by whatever
#     of its town, county, postcode, state and ZIP follow on the same line;
#   * anything ending in a UK POSTCODE, reaching back over the building,
#     street and town segments in front of it ("Riverside House, Canal Road,
#     Leeds LS1 4XY") -- the form a letterhead or a party block takes when it
#     has no house number. A postcode alone is still an address: a UK
#     postcode covers about fifteen households.
#
# Bounded the way the Turkish address rule is: a place name alone is not an
# address, so "London" in prose is nothing. The tail only takes capitalised
# segments, a postcode or a ZIP, and stops at the first lowercase word, so
# "12 Rose Lane, Leeds, on 3 March" ends at "Leeds". Every gap is [ \t] or a
# comma, never a line break: an address set out over several lines is reported
# line by line, and a lone town line ("Leeds") is the place-name case above.
# "Court" and "Plaza" are deliberately not street types here: "paragraph 4
# High Court" is prose, and "Plaza" is an ordinary word in Turkish addresses,
# where the Turkish rule already reads it. Both are building words, which count
# only in the second shape, in front of a postcode.
_EN_STREET_TYPE = (
    r"(?:" + _title_and_caps(
        "Street", "Road", "Lane", "Avenue", "Drive", "Close", "Place", "Square", "Gardens", "Terrace",
        "Crescent", "Way", "Row", "Mews", "Walk", "Grove", "Parade", "Boulevard", "Wharf", "Quay", "Yard",
        "Hill", "Park", "Green", "Gate", "Circus", "Embankment", "Broadway", "Highway", "Parkway", "Rise",
        "Vale", "Passage", "Approach",
    ) + r"|(?:St|Rd|Ln|Ave|Dr|Pl|Sq|Cres|Blvd|Pkwy|Hwy|Gdns|ST|RD|AVE)\.?)(?![\w'’])"
)
_EN_BUILDING_TYPE = (
    r"(?:" + _title_and_caps(
        "House", "Building", "Buildings", "Centre", "Center", "Tower", "Chambers", "Mansions", "Lodge",
        "Cottage", "Farm", "Hall", "Works", "Estate", "Point", "Exchange", "Wharf", "Park", "Court", "Plaza",
    ) + r")(?![\w'’])"
)
_EN_SUBUNIT = (
    r"(?:(?:" + _title_and_caps("Flat", "Apartment", "Unit", "Suite", "Room", "Office") + r"|Apt)\.?[ \t]+[A-Za-z0-9\-]{1,6}"
    r"|(?:" + _title_and_caps("Ground", "First", "Second", "Third", "Fourth", "Fifth") + r"|\d{1,2}(?:st|nd|rd|th))[ \t]+(?:Floor|FLOOR)"
    r"|(?:" + _title_and_caps("Floor", "Level") + r")[ \t]+\d{1,3}(?!\d))"
)
_EN_HOUSE_NUMBER = r"(?<![\w.,/:£$€-])\d{1,5}[A-Za-z]?(?:[ \t]?[-–][ \t]?\d{1,5}[A-Za-z]?)?,?[ \t]+"
_EN_STREET_NAME = r"(?:(?:" + _EN_CAP_WORD + r"|\d{1,3}(?:st|nd|rd|th))[ \t]+){1,3}"
_EN_DIRECTIONAL = r"(?:[ \t]+(?:N|S|E|W|NE|NW|SE|SW)(?![\w'’]))?"
_UK_POSTCODE = r"[A-Z]{1,2}\d[A-Z\d]?[ \t]+\d[A-Z]{2}(?![\w'’])"
_US_ZIP = r"\d{5}(?:-\d{4})?(?!\d)"
_EN_LOCALITY = _EN_CAP_WORD + r"(?:[ \t]+(?:(?:upon|under)[ \t]+)?" + _EN_CAP_WORD + r"){0,3}"
_EN_ADDRESS_TAIL = (
    r"(?:(?:[ \t]*,[ \t]*|[ \t]+)(?:" + _UK_POSTCODE + "|" + _EN_SUBUNIT + r")"
    r"|[ \t]*,[ \t]*" + _EN_LOCALITY + r"|[ \t]+" + _US_ZIP + r"){0,6}"
)
# In front of a postcode a building may carry its number: "7 Linnet Court" is
# a block of flats, and "Court" is a street word only here, where the postcode
# already proves the line is an address. The country after the postcode is
# the same line of the same address ("Leeds LS1 4AP, United Kingdom").
_EN_SEGMENT = (
    r"(?:" + _EN_SUBUNIT
    + r"|(?:" + _EN_HOUSE_NUMBER + r")?" + _EN_STREET_NAME + _EN_STREET_TYPE
    + r"|(?:" + _EN_HOUSE_NUMBER + r")?" + _EN_CAP_WORD + r"(?:[ \t]+" + _EN_CAP_WORD + r"){0,3}[ \t]+" + _EN_BUILDING_TYPE + r")"
)
_EN_COUNTRY = (
    r"(?:United[ \t]+Kingdom|UNITED[ \t]+KINGDOM|UK|U\.K\.|England|ENGLAND|Scotland|SCOTLAND|Wales|WALES"
    r"|Northern[ \t]+Ireland|NORTHERN[ \t]+IRELAND|Great[ \t]+Britain|GREAT[ \t]+BRITAIN)(?![\w'’])"
)
ENGLISH_ADDRESS_RULE = DetectionRule(
    "address",
    r"(?:" + _EN_SUBUNIT + r",?[ \t]+)?" + _EN_HOUSE_NUMBER + _EN_STREET_NAME + _EN_STREET_TYPE + _EN_DIRECTIONAL + _EN_ADDRESS_TAIL
    + r"|(?<![\w'’])(?:" + _EN_SEGMENT + r"[ \t]*,[ \t]*){0,4}(?:" + _EN_LOCALITY + r"[ \t]*,?[ \t]+)?" + _UK_POSTCODE
    + r"(?:[ \t]*,[ \t]*" + _EN_COUNTRY + r")?",
    "HIGH",
    "Generalize or replace with consistent pseudonym",
    "ADDRESS",
    flags=re.UNICODE,
)

# A currency amount, in either order: "50.000,00 TL", "USD 10.000", "£12,500",
# "€3.5 million", "GBP 2,000", "12,500 pounds sterling", "$1.2bn", "US$400".
#
# The symbols sit OUTSIDE the leading \b. A word boundary in front of "£", "$",
# "€" or "₺" holds only where a LETTER precedes the symbol, so in the form an
# amount is actually written -- " $1,000", "(€500)" -- the boundary failed and
# the alternative never matched: "$" and "€" amounts were listed here and
# detected nowhere but in "US$". The amount ends on a digit, so a sentence's
# full stop or comma is not carried into the sample ("TL 1.000," is the value
# "TL 1.000"), and a "million"/"m"/"bn"/"k" after it is part of the amount.
#
# The gap between amount and currency is _INLINE_SPACE, never \s: a sample
# carrying a line break matches nothing in a DOCX part and cannot be redacted,
# but a non-breaking space (U+00A0, narrow U+202F) is how Word keeps an amount
# on one line with its currency, and is ordinary run text. An amount whose
# currency sits across a line break is still an amount, so it is reported ALONE
# -- the currency is read through a lookahead ("12,500\npounds sterling") or one
# fixed-width lookbehind per currency token ("USD\n10.000") and left out of
# the span.
_INLINE_SPACE = r"[^\S\r\n\v\f\x85\u2028\u2029]"
_AMOUNT = r"\d(?:[\d.,]*\d)?"
_AMOUNT_MULTIPLIER = r"(?:" + _INLINE_SPACE + r"?(?:million|billion)(?![\w'’])|(?:m|bn|k)(?![\w'’]))?"
_PREFIX_CURRENCY_CODES = ("USD", "EUR", "GBP", "TRY", "TL", "CHF", "US$")
_PREFIX_CURRENCY_SYMBOLS = ("£", "$", "€", "₺")
_WRAPPED_PREFIX_CURRENCY = "(?:" + "|".join(
    [rf"(?<=\b{re.escape(code)}\n)" for code in _PREFIX_CURRENCY_CODES]
    + [rf"(?<={re.escape(symbol)}\n)" for symbol in _PREFIX_CURRENCY_SYMBOLS]
) + ")"
_SUFFIX_CURRENCY = r"(?:(?:USD|EUR|GBP|TRY|TL|CHF|dolar|euros?|pounds?(?:" + _INLINE_SPACE + r"+sterling)?|sterling|dollars?)\b|₺)"
MONEY_RULE = DetectionRule(
    "money_amount",
    r"(?:\b(?:USD|EUR|GBP|TRY|TL|CHF)|\bUS\$|[£$€₺])" + _INLINE_SPACE + r"?" + _AMOUNT + _AMOUNT_MULTIPLIER
    + r"|" + _WRAPPED_PREFIX_CURRENCY + _AMOUNT + _AMOUNT_MULTIPLIER
    + r"|\b" + _AMOUNT + r"(?:" + _INLINE_SPACE + r"?(?:million|billion))?"
    + r"(?:" + _INLINE_SPACE + r"?" + _SUFFIX_CURRENCY + r"|(?=\n[ \t]*" + _SUFFIX_CURRENCY + r"))",
    "MEDIUM",
    "Generalize or keep if legally necessary",
    "AMOUNT",
)


_TRADE_REGISTRY_LABEL_END = re.compile(r"\b(?:ticaret|tic\.)\Z", re.IGNORECASE)


def _follows_trade_registry_label(text: str, match: re.Match) -> bool:
    """Whether a bar-rule "sicil" is the tail of a "Ticaret/Tic. Sicil" label."""
    if match.group("bar_sicil") is None:
        return False
    end = match.start("bar_sicil")
    while end and text[end - 1].isspace():
        end -= 1
    return _TRADE_REGISTRY_LABEL_END.search(text, max(0, end - 8), end) is not None


def _address_start(match: re.Match, text: str, triggers: list[tuple[int, int]]) -> int:
    """Exclude a preceding context trigger without case-narrowing street names.

    IGNORECASE is needed for "gül sok" as well as "GÜL SOK". It also lets
    the two-word prefix absorb "hasta" in "hasta Gül Sok". Cut that prefix
    after a context trigger only if another street-name word remains before
    the structure word. "Sağlık Caddesi" must keep Sağlık as its name.

    A multiword trigger can begin before the regex match ("ticari sır Gül
    Sok" matches from "sır"); use its complete span, not a prefix-token list.
    Ambiguous compound names containing a trigger can be split this way too,
    but the excluded trigger stays covered by its CRITICAL context finding.
    No address tail or context sweep is shortened here.

    The loop scans every trigger because the earliest one in the document is
    often NOT the applicable one -- in "hasta olan hasta<tab>Gul Sok" the address
    match starts at the SECOND "hasta", so the first ends before the span and
    must be skipped, not taken. At most one cut can actually apply, though:
    the prefix is two words at most, so once start has moved past a trigger the
    only remaining candidate is the second prefix word, whose remainder is
    whitespace and fails the guard above. Measured over 66,528 synthetic
    trigger/filler/structure-word analyses: 46,620 matches took one cut, 19,908
    took none, none took two. So the loop is a scan for the right trigger, not
    an accumulator -- do not "simplify" it to triggers[0].
    """
    start = match.start()
    prefix_end = match.end("address_prefix")
    for trigger_start, trigger_end in triggers:
        if trigger_start >= prefix_end:
            break
        if start < trigger_end < prefix_end:
            remainder = text[trigger_end:prefix_end]
            if remainder.strip():
                start = trigger_end + len(remainder) - len(remainder.lstrip())
    return start


def _assign_typed_value_placeholders(
    findings: list[dict],
    typed_values: dict[tuple[str, str], list[tuple[int, str]]],
    counters: Counter[str],
    state: dict[tuple[str, str], str],
) -> None:
    """One placeholder per amount or date VALUE, typed by its role when it has one.

    An amount is a [PENALTY_AMOUNT_n] in a penalty clause and a
    [LIABILITY_CAP_n] in a cap; a date is a [BIRTH_DATE_n] after a birth-date
    label. The type has to be decided for the VALUE, not the occurrence,
    because the reviewed export replaces every occurrence of an approved
    sample by string match: one value can carry only one replacement in the
    delivered file. So a value seen in one role everywhere takes that role's
    placeholder, and a value seen in two roles ("liability is capped at the
    fees paid", and the fee itself) takes the plain [AMOUNT_n] / [DATE_n] --
    a neutral label at both sites rather than a cap label on the fee.

    `typed_values` maps (family prefix, folded value) to every (finding index,
    role prefix) the rule loop saw it under, in document order. Each finding
    keeps its own semantic_type, so a birth date is still treated as one by the
    policy wherever it appears.
    """
    for (family, key), occurrences in typed_values.items():
        roles = {prefix for _index, prefix in occurrences}
        prefix = roles.pop() if len(roles) == 1 else family
        placeholder = _placeholder_for(prefix, key, counters, state)
        for index, _prefix in occurrences:
            findings[index]["placeholder"] = placeholder


# Identifiers written with and without their spacing or label are one value:
# "MERSİS No: 0111111111400015" and a bare "0111111111400015" in the same party
# block came out [MERSIS_1] and [MERSIS_3], and "TR00 0001 ..." beside
# "TR000001..." would have done the same. The key is the identifier itself;
# the label and the spacing are how it happened to be written.
_KEYED_ON_DIGITS = {"mersis_number", "tax_number", "turkish_national_id", "account_number", "trade_registry_number"}
_CASE_NUMBER_CORE_RE = re.compile(r"\d{4}\s*/\s*[0-9A-Za-z.-]+")
_PASSPORT_CORE_RE = re.compile(r"[A-Za-z]{1,2}\d{6,9}$")


def _identifier_key(category: str, value: str) -> str:
    """The value a numeric identifier's pseudonym is remembered under."""
    if category in _KEYED_ON_DIGITS:
        return re.sub(r"\D", "", value) or value
    if category == "iban":
        return re.sub(r"[^0-9A-Za-z]", "", value).upper() or value
    if category == "passport_number":
        core = _PASSPORT_CORE_RE.search(value.strip())
        return core.group().upper() if core else value
    if category == "phone_number":
        # The last ten digits: "+90 522 ...", "0522 ..." and "522 ..." are one line.
        return re.sub(r"\D", "", value)[-10:] or value
    if category == "land_registry_parcel":
        # Block then parcel: "1417 ada, 23 parsel" and "Ada: 1417 Parsel: 23"
        # are one parcel. Every form the rule matches writes the block first.
        return "/".join(str(int(number)) for number in re.findall(r"\d+", value)) or value
    return value


def _carries_bare_repeats(case_id: str) -> bool:
    """Whether an English case identifier's whole-token repeats become findings.

    False only for a short all-digit identifier; see _bare_case_number_repeats.
    """
    return not (case_id.isdigit() and len(case_id) < 6)


def _bare_case_number_repeats(text: str, case_ids: dict[str, str], findings: list[dict]) -> list[dict]:
    """A labelled English reference's later bare mentions, each where it stands.

    `case_ids` maps each identifier ENGLISH_CASE_NUMBER_RULE found behind a
    label to its placeholder. Every other occurrence of that identifier, as a
    whole token, outside every case-number finding already made, becomes a
    finding under the same placeholder. A purely numeric identifier under six
    digits is not carried: "Case No: 2026" would otherwise put a review card on
    every year in the document. The export still removes, and the output scan
    still searches for, its repeats through the bare-identifier needle
    (finding_needles), as for every labelled case number.
    """
    taken = [(f["start"], f["end"]) for f in findings if f["category"] == "case_or_investigation_number"]
    repeats = []
    for case_id, placeholder in case_ids.items():
        if not _carries_bare_repeats(case_id):
            continue
        pattern = r"(?<![A-Za-z0-9/.:\-])" + re.escape(case_id) + r"(?![A-Za-z0-9/\-]|[.:][A-Za-z0-9])"
        # Case-insensitive, like the backstop this replaces ("kb-2026-…").
        for match in re.finditer(pattern, text, re.IGNORECASE):
            start, end = match.span()
            if any(span_start < end and start < span_end for span_start, span_end in taken):
                continue
            taken.append((start, end))
            repeats.append(
                _finding(
                    ENGLISH_CASE_NUMBER_RULE.category,
                    match.group(),
                    ENGLISH_CASE_NUMBER_RULE.risk,
                    ENGLISH_CASE_NUMBER_RULE.action,
                    placeholder,
                    start,
                    end,
                )
            )
    return repeats


def _case_number_key(match: re.Match, value: str) -> str:
    """A case number's pseudonym key: its REGISTER and its number.

    "Esas No: 2025/318", the citation form "2025/318 Esas" and the court
    citation initials "2025/318 E." / "E. 2025/318" are one case and share a
    placeholder. "Karar No: 2025/318" and "Soruşturma No: 2025/318" are
    not -- a decision number, a docket number and an investigation number can
    carry the same digits -- and keying on the digits alone told the reader
    they were one. The register is the label word in front of the number, or,
    for the citation form, the word after it.
    """
    for group in ("yevmiye_number", "yevmiye_cited"):
        if group in match.re.groupindex and match.group(group):
            # A notary's journal number, with or without its leading zeros.
            return "yevmiye\x00" + str(int(match.group(group)))
    core = _CASE_NUMBER_CORE_RE.search(value)
    if not core:
        return value
    register = next(
        (match.group(group) for group in ("case_register", "investigation_register", "investigation_register_cited")
         if group in match.re.groupindex and match.group(group)),
        None,
    )
    if not register:
        # The court-citation initials: "2025/318 E." and "E. 2025/318" are the
        # Esas register, "K." the Karar register, so each shares a placeholder
        # with the same number written out as "Esas No: 2025/318".
        initial = next(
            (match.group(group) for group in ("case_register_initial", "case_register_lead")
             if group in match.re.groupindex and match.group(group)),
            None,
        )
        if initial:
            register = "esas" if initial == "E" else "karar"
    if not register:
        register = value[:core.start()].split()[0] if value[:core.start()].split() else ""
    number = re.sub(r"\s", "", core.group())
    # Folded with the diacritics stripped: "Soruşturma No" and the OCR or ASCII
    # "Sorusturma No" are one register.
    return legal_semantics.fold(register) + "\x00" + number


def valid_turkish_national_id(value: str) -> bool:
    digits = re.sub(r"\D", "", value)
    if len(digits) != 11 or digits[0] == "0":
        return False
    nums = [int(char) for char in digits]
    tenth = ((sum(nums[0:9:2]) * 7) - sum(nums[1:8:2])) % 10
    eleventh = sum(nums[:10]) % 10
    return nums[9] == tenth and nums[10] == eleventh


def valid_turkish_tax_number(value: str) -> bool:
    """Validate a 10-digit VKN using the published check-digit algorithm."""
    digits = re.sub(r"\D", "", value)
    if len(digits) != 10:
        return False
    nums = [int(char) for char in digits]
    total = 0
    for index in range(9):
        tmp = (nums[index] + 9 - index) % 10
        if tmp == 0:
            continue
        part = (tmp * (2 ** (9 - index))) % 9
        if part == 0:
            part = 9
        total += part
    return (10 - (total % 10)) % 10 == nums[9]
