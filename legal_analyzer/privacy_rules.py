"""The rule table, DETECTION_RULES, and the helpers that look a rule up in it.

The table is assembled from the rules the concern modules define, plus the
rules written inline in it. The helpers below it reach their rule by identity
(PRIVILEGE_RULE, CRIMINAL_RULE, the Turkish case-number rule), so they live
here beside the table rather than in the concern module the rule belongs to. Moved
out of ``privacy.py`` unchanged; that module re-exports every name here.
"""

from __future__ import annotations

import bisect
import re
from collections import Counter

from legal_analyzer import legal_semantics

from .privacy_common import (
    DetectionRule,
    DIRECT_IDENTIFIER_CATEGORIES,
    _finding,
    _placeholder_for,
)
from .privacy_context import (
    _ALLEGATION_MARKER_RE,
    CONTEXT_SWEEP_CATEGORIES,
    _CONTEXT_SWEEP_CHAR,
    _context_sweep_pieces,
    _CONTEXT_TRIGGER_RE,
    _CONTEXT_TRIGGERS,
    _INVESTIGATION_WORD_RE,
    _is_docket_investigation,
    _segment_is_reportable,
    _SEGMENT_LEADING_NOISE_RE,
)
from .privacy_identifiers import (
    ENGLISH_ACCOUNT_RULE,
    ENGLISH_ADDRESS_RULE,
    ENGLISH_CASE_NUMBER_RULE,
    ENGLISH_DATE_RULE,
    ENGLISH_PHONE_RULE,
    MONEY_RULE,
    TURKISH_PHONE_RULE,
)
from .privacy_courts import (
    _AUTHORITY_CASE_END,
    _COURT_PLACE_PREFIX,
    DIRECTORATE_RULE,
    ENGLISH_COURT_RULE,
    GENERIC_COURT_SUFFIX_RULE,
    HIGH_COURT_RULE,
    NUMBERED_COURT_RULE,
    PROSECUTOR_OFFICE_RULE,
    TAX_OFFICE_FIELD_RULE,
)
from .privacy_names import (
    COMPANY_RULE,
    LAW_FIRM_RULE,
    PARTY_ROLE_RULE,
)


# An optional "no"/"numarası" and an optional ":" or "-" between a keyword and
# its number, each token owning the blanks after it. Written as three separate
# optional [ \t]* runs instead, a line of blanks after "ada" or "Yevmiye" with
# no number behind it took cubic time to reject -- measured at over 30 seconds
# for 3,000 spaces. With one run per token there is only one way to split them.
_PARCEL_LABEL = r"(?:(?:no|numarası|numarasi)[ \t]*)?(?:[:\-][ \t]*)?"


DETECTION_RULES = [
    DetectionRule("email_address", r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", "HIGH", "Replace with consistent pseudonym", "EMAIL"),
    TURKISH_PHONE_RULE,
    ENGLISH_PHONE_RULE,
    # The same numbers with the area or operator code in brackets -- "(0299)
    # 555 12 34", "+90 (522) 123 45 67", "0 (399) ..." -- which is how contract
    # party blocks, letterheads and enforcement filings print them. The rule
    # above needs a digit or separator right after the code, so a ")" there
    # matched nothing and the whole number shipped. Gaps are [ \t], never \s:
    # a sample with a newline cannot be removed from a DOCX. _identifier_key
    # keys a phone on its last ten digits, so "(0299) 555 12 34" and
    # "0299 555 12 34" are one line and one pseudonym.
    DetectionRule("phone_number", r"(?<![\d(])(?:\+90[ \t]?|0[ \t]?)?\(0?(?:5\d{2}|2\d{2}|3\d{2}|4\d{2})\)[ \t]?\d{3}[ \t.-]?\d{2}[ \t.-]?\d{2}(?!\d)", "HIGH", "Replace with consistent pseudonym", "PHONE"),
    DetectionRule("turkish_national_id", r"\b(?:T\.?\s*C\.?\s*(?:Kimlik|No|Numara|Kimlik No)?[:\s]*)?([1-9]\d{10})\b", "CRITICAL", "Remove completely or replace with neutral placeholder", "NATIONAL_ID"),
    DetectionRule("iban", r"\bTR\d{2}\s?(?:\d{4}\s?){5}\d{2}\b", "CRITICAL", "Remove completely or mask partially", "IBAN"),
    DetectionRule("tax_number", r"\b(?:VKN|vergi\s*(?:kimlik\s*)?(?:no|numarasi|numarası)|tax\s*(?:id|number))[:\s]*(\d{10})\b", "HIGH", "Mask partially", "TAX_NUMBER"),
    DetectionRule("mersis_number", r"\b(?:MERS[İI]S|mersis)\s*(?:no|numarası|numarasi)?[:\s]*\d{16}\b", "HIGH", "Replace with consistent pseudonym", "MERSIS"),
    DetectionRule("mersis_number", r"\b0\d{15}\b", "HIGH", "Replace with consistent pseudonym", "MERSIS"),
    # Second alternative widened from bare "sicil\s*no": a labelled personnel
    # registry number ("sicil numarası 884411") did not match at all, because
    # "no" consumed only the literal word "no" and left "numarası" unmatched.
    #
    # The optional leading "baro" in front of the alternation is what puts the
    # word "Baro" INSIDE the span. Without it "Baro Sicil No: 45678" matched
    # only from "Sicil": the first alternative needs "sicil"/"no"/"numarası"
    # directly after "baro" and gets "Sicil No", where "Sicil" is consumed and
    # "No" is not, so it fails and the second alternative picks the phrase up
    # one word late. "Baro" then lay outside every finding, so no reviewer could
    # approve it and it shipped in the deliverable beside a redacted number --
    # the same defect, and the same reason, as the court place word (see
    # _COURT_PLACE_PREFIX in privacy_courts).
    # Measured: Turkish bar-registration full-span recall was 2 of 6.
    #
    # It is optional, so the two shapes that never carried the word keep
    # matching unchanged: "baro no: 123" (the optional group gives its text back
    # to the first alternative on backtracking) and a bare "sicil numarası
    # 884411". The group spells its gap [ \t]* rather than \s*, so the word it
    # adds cannot join across a line break -- see _COURT_PLACE_PREFIX for why a
    # sample containing a newline can never be removed from a DOCX. The \s*
    # inside the alternation is untouched and predates this; the corpus-wide
    # invariant is pinned by tests/test_redaction_parity.py.
    #
    # "baro" alone covered only the bare nominative. The standard Turkish form
    # names the association possessively -- "İstanbul Barosu Sicil No: 45678" --
    # and matched from "Sicil", leaving "İstanbul Barosu" outside every finding
    # of any category. The (?:su)? suffix is that one inflection, enumerated
    # rather than a general suffix stripper, and each of the two spellings has a
    # test. The genitive "Barosunun" is deliberately NOT accepted: it occurs
    # nowhere in the corpus, the gold set or the suite, and "X Barosunun sicil
    # numarası" reads as the association's own number rather than a lawyer's --
    # a different referent, so matching it would assign the wrong category.
    #
    # The \s* inside the alternation crosses a newline, so "Sicil\nNo: 45678"
    # produces a finding whose sample carries one and therefore cannot be
    # removed from a DOCX. DO NOT narrow it to [ \t]* -- that trade is the wrong
    # way round. Today the number is at least REPORTED: a reviewer sees it, the
    # gate counts it, residual risk reflects it. Narrowed, that layout matches
    # nothing at all -- no finding, nothing to approve, nothing counted -- so
    # the number leaks AND the document clears the gate clean. A visible
    # unredactable finding beats an invisible one. The real remedy keeps the
    # match while holding the span off the newline, which is a design change
    # rather than a character-class edit. Pre-existing, unchanged here.
    #
    # The bar's CITY is the other half of the same defect, so the rule carries
    # the same list-gated place prefix as the two numbered court rules -- a
    # preceding token joins the span only when turkish_places recognises it,
    # which is why the shared _COURT_PLACE_PREFIX group is reused rather than a
    # second prefix being defined. The call site routes any rule carrying that
    # group through _court_start().
    #
    # A company's trade-registry number is not a lawyer's: "Ticaret Sicil No:
    # 48213" in a contract party block was reported here, as a bar registration
    # that "identifies a specific attorney of record". _follows_trade_registry_label
    # hands that shape to trade_registry_number below, whatever whitespace -- a
    # double space, a tab, a line break between table cells -- separates
    # "Ticaret" from "Sicil". It tests the TEXT before "sicil", so the place
    # prefix capturing "Ticaret" as a candidate word cannot step around it.
    DetectionRule("bar_registration_number", _COURT_PLACE_PREFIX + r"\b(?:baro(?:su)?[ \t]*)?(?:baro\s*(?:sicil|no|numarası|numarasi)|(?P<bar_sicil>sicil)\s*(?:no|numarası|numarasi))[:\s]*(?P<bar_number>\d{3,8})\b", "HIGH", "Replace with consistent pseudonym", "BAR_REGISTRATION"),
    # A company's number in the trade registry, as contract party blocks and
    # board resolutions print it beside the MERSİS number: "Ticaret Sicil No:
    # 48213", "Ticaret Sicili Numarası: 7731", "Tic. Sicil No: 512". Like a
    # MERSİS number it singles out one registered entity, so it is a direct
    # identifier and keyed on its digits. After a "no"/"numarası" label the gap
    # to the number is [:\s]*, the bar rule's own tolerance, so a number on the
    # next line ("Ticaret Sicil No:" closing one table cell) is still REPORTED
    # here as it was there -- see the bar rule's note on why a visible finding
    # beats an invisible one. Without a label the number must be on the line.
    # "Ticaret" and "Sicil" may be split by any whitespace, as the bar rule
    # yields every such layout here; when a line break splits them the finding
    # starts at "Sicil", so its sample does not run across the line.
    DetectionRule("trade_registry_number", r"\b(?:ticaret|tic\.)(?P<trade_gap>\s*)(?P<trade_sicil>sicil)(?:i)?(?:\s*(?:no|numarası|numarasi)[:\s]*|[ \t]*(?:[:\-][ \t]*)?)\d{1,8}\b", "HIGH", "Replace with consistent pseudonym", "TRADE_REGISTRY"),
    # Passport number: same government-issued, uniquely-identifying shape as
    # turkish_national_id/iban, so it carries the same CRITICAL risk.
    DetectionRule("passport_number", r"\b(?:pasaport|passport)\s*(?:no|numarası|numarasi|number)?[:\s]*[A-Z]{1,2}\d{6,9}\b", "CRITICAL", "Remove completely or replace with neutral placeholder", "PASSPORT"),
    # Labelled bank account number outside the IBAN format ("hesap no 1234-5678901").
    # Risk matched to tax_number/mersis/bar_registration_number rather than IBAN's
    # CRITICAL: it lacks IBAN's fixed, check-digited, country-coded structure.
    DetectionRule("account_number", r"\bhesap\s*(?:no|numarası|numarasi)[:\s]*\d[\d\-]{4,19}\d\b", "HIGH", "Mask partially", "ACCOUNT_NUMBER"),
    ENGLISH_ACCOUNT_RULE,
    # Social media handle. Risk matched to email_address (HIGH): both are direct,
    # public-facing contact identifiers for a specific person. The negative
    # lookbehind keeps this from matching the "@domain" tail of an email address
    # already covered by email_address ("test@example.com" -- "t" before "@" is
    # a word char, so this rule does not also fire on it).
    DetectionRule("social_media_handle", r"(?<![\w.])@[A-Za-z0-9_]{2,30}\b", "HIGH", "Replace with consistent pseudonym", "SOCIAL_HANDLE"),
    # Vehicle plate. Deliberately anchored on a "plaka" context word on one side
    # or the other -- an unanchored "\d{2}\s?[A-Z]{1,3}\s?\d{2,4}" shape also
    # matches fragments of case numbers, dates, and reference codes. See the
    # accuracy-audit note above `DETECTION_RULES` before loosening this.
    DetectionRule("vehicle_plate", r"\b(?:plaka(?:sı|si)?\s*(?:no(?:su)?)?[:\s]*\d{2}\s?[A-ZÇĞİÖŞÜ]{1,3}\s?\d{2,4}|\d{2}\s?[A-ZÇĞİÖŞÜ]{1,3}\s?\d{2,4}\s+plaka(?:lı|li|sı|si)?)\b", "HIGH", "Replace with consistent pseudonym", "VEHICLE_PLATE"),
    # A land-registry parcel: "1417 ada, 23 parsel", "Ada: 1417 Parsel: 23",
    # "Ada No: 1417, Parsel No: 23", "1417/23 ada/parsel", "ada/parsel:
    # 1417/23". Enforcement files (a haciz on real property), notarial notices
    # about a lease and sale contracts name the property this way, and the
    # parcel leads through the land registry to its owner -- the same reasoning
    # as vehicle_plate. Anchored on BOTH words, so a bare "ada" (island) or a
    # bare number never fires. The span ends at the stem, so "parseldeki" keeps
    # its case suffix outside it. Keyed on its digits, so every way of writing
    # one parcel is one pseudonym.
    DetectionRule("land_registry_parcel", r"\b\d{1,6}[ \t]*ada[ \t]*(?:[,;][ \t]*)?\d{1,5}[ \t]*parsel|\bada[ \t]*" + _PARCEL_LABEL + r"\d{1,6}[ \t]*(?:[,;][ \t]*)?parsel[ \t]*" + _PARCEL_LABEL + r"\d{1,5}\b|\b\d{1,6}[ \t]*/[ \t]*\d{1,5}[ \t]+ada[ \t]*/[ \t]*parsel|\bada[ \t]*/[ \t]*parsel[ \t]*" + _PARCEL_LABEL + r"\d{1,6}[ \t]*/[ \t]*\d{1,5}\b", "HIGH", "Replace with consistent pseudonym", "LAND_PARCEL"),
    # The second alternative is the citation form with the register word AFTER
    # the number -- "2025/318 Esas sayılı dosya" -- which the first never saw,
    # so the synthetic NDA's prior case number shipped beside its pseudonymised
    # court. The span is the number alone, held off the word by a lookahead so
    # "Esas" stays readable, and both gaps are [ \t] so the sample cannot run
    # across a line. The first alternative is tried first at every position, so
    # "Esas No: 2026/42 Esas" is still one finding from the label. The
    # abbreviated "2025/417 E." form is deliberately not added here: it is the
    # gap docs/accuracy.md records, and closing it is its own measured change.
    ENGLISH_CASE_NUMBER_RULE,
    #
    # The last two alternatives are a notary's journal number, which carries no
    # year: "Yevmiye No: 04512" on the instrument and "04512 yevmiye numarası
    # ile" in the notary's endorsement. Only the YYYY/NNN shape was matched, so
    # a notarial notice shipped with its journal number -- which, with the
    # notary's name, retrieves the instrument -- in every place it was printed.
    # Both forms capture the number as `yevmiye_number`, which
    # _case_number_key keys on, so the two mentions share one pseudonym. The
    # labelled form refuses a following "/" so it cannot cut "2026/18821" short
    # where the first alternative did not match; the trailing form needs a word
    # after "yevmiye", so "2 yevmiye defteri" is not a number. The cited form's
    # span carries the word "yevmiye": its sample is the redaction target,
    # matched as a string throughout the document, and a bare "12" would take
    # "2012" and "madde 12" with it.
    DetectionRule("case_or_investigation_number", r"\b(?:soruşturma|sorusturma|kovuşturma|kovusturma|esas|karar|dosya|takip|yevmiye|talimat|değişik iş|degisik is|UYAP)\s*(?:no|numarası|numarasi)?[:\s]*[0-9]{4}\s*/\s*[0-9A-Za-z.-]+\b|\b[0-9]{4}[ \t]*/[ \t]*[0-9]+(?=[ \t]+(?P<case_register>esas|karar)\b)|\b[0-9]{4}[ \t]*/[ \t]*[0-9]+(?=[ \t]+(?:sayılı|sayili)[ \t]+(?P<investigation_register>soruşturma|sorusturma|kovuşturma|kovusturma)|[ \t]+(?P<investigation_register_cited>soruşturma|sorusturma|kovuşturma|kovusturma)[ \t]+(?:numaralı|numarali|no[.'’]?lu)\b)|\b[0-9]{4}[ \t]*/[ \t]*[0-9]+(?=[ \t]*(?P<case_register_initial>(?-i:[EK]))\.)|\b(?P<case_register_lead>(?-i:[EK]))[.:][ \t]*[0-9]{4}[ \t]*/[ \t]*[0-9]+\b|\byevmiye[ \t]*" + _PARCEL_LABEL + r"(?P<yevmiye_number>[0-9]{1,7})\b(?![ \t]*/)|\b(?P<yevmiye_cited>[0-9]{1,7})[ \t]+yevmiye(?=[ \t]+(?:no|numarası|numarasi|numaralı|numarali|sayılı|sayili|ile)\b)", "HIGH", "Replace with consistent pseudonym", "CASE_NUMBER"),
    # Triggered on address *structure* words (street/neighbourhood/building/
    # district), never on a bare place name. The trigger list used to carry
    # "İstanbul", "Beşiktaş" and "Ümraniye"; those fired on a city or district
    # mentioned in running text and then swallowed 140 characters of whatever
    # followed, so "İstanbul 7. İş Mahkemesi kararının temyiz incelemesinde,
    # davacı işçi Hasan Demirci'nin" was reported as an address. A place name on
    # its own is not an address -- it does not narrow anyone down -- and every
    # address in the gold set is anchored on a structure word whose trailing
    # span already sweeps up the district and city that follow it ("Bağdat
    # Caddesi No: 41 Daire: 7 Maltepe"). Dropping the three place names removed
    # 8 false positives with gold-set recall held at 1.0. Accepted cost: an
    # address written as a bare "Beşiktaş / İstanbul", with no street or
    # neighbourhood, is no longer flagged as an address.
    DetectionRule("address", r"(?P<address_prefix>(?:[A-ZÇĞİÖŞÜ][a-zA-Z0-9çğıöşüÇĞİÖŞÜ]*\s+){0,2})\b(?:mah\.?|mahallesi|cad\.?|caddesi|sok\.?|sokak|sokağı|sokagi|bulvarı|bulvari|apartmanı|apartmani|apt\.?|[İi]lçesi|ilcesi|köyü|koyu)\b(?:[^.\n]|(?<=\d)\.){0,140}", "HIGH", "Generalize or replace with consistent pseudonym", "ADDRESS"),
    ENGLISH_ADDRESS_RULE,
    DetectionRule("date", r"\b(?:\d{1,2}[./-]\d{1,2}[./-]\d{2,4}|\d{4}[./-]\d{1,2}[./-]\d{1,2})\b", "MEDIUM", "Generalize unless legally necessary", "DATE"),
    ENGLISH_DATE_RULE,
    MONEY_RULE,
    DetectionRule("health_data", _CONTEXT_TRIGGERS["health_data"] + _CONTEXT_SWEEP_CHAR + r"{0,120}", "CRITICAL", "Flag for human legal review", "SENSITIVE_HEALTH_DATA"),
    DetectionRule("criminal_allegation", _CONTEXT_TRIGGERS["criminal_allegation"] + _CONTEXT_SWEEP_CHAR + r"{0,160}", "CRITICAL", "Flag for human legal review", "CRIMINAL_ALLEGATION"),
    # The trigger is a named group; _privilege_pieces asks legal_semantics
    # whether THAT word, in its clause, is privilege evidence.
    # The category keeps its historical key -- the database, the gold set, the
    # held-out eval and the UI all read it -- but it now means attorney-client
    # privilege only; see CATEGORY_EXPLANATIONS.
    DetectionRule("privileged_or_confidential", r"(?P<trigger>" + _CONTEXT_TRIGGERS["privileged_or_confidential"] + r")" + _CONTEXT_SWEEP_CHAR + r"{0,160}", "CRITICAL", "Flag for human legal review", "PRIVILEGED_CONTENT"),
    # The role word alone, deliberately NOT the surrounding clause. A party role
    # is a re-identification *signal* for a reviewer to weigh, not an identifier.
    # Trailing context ([^.\n]{0,120}) made this span the longest at its start,
    # so build_redacted_preview preferred it over the findings nested inside it:
    # a CRITICAL national id was emitted as a MEDIUM [PARTY_ROLE_n], spans cut
    # mid-date ("[PARTY_ROLE_1].03.2026"), and whole clauses of legal substance
    # were destroyed. The identifiers in that clause -- name, TCKN, address,
    # phone, email -- each have their own precise rule and are still redacted at
    # their own risk level. Measured over the gold set, dropping the context
    # exposes 142 characters, all scaffolding ("vekili Av", "ile", "(TCKN:"),
    # and no identifier. It also stops party_role samples from storing a party's
    # name and national id in cleartext.
    PARTY_ROLE_RULE,
    PROSECUTOR_OFFICE_RULE,
    GENERIC_COURT_SUFFIX_RULE,
    NUMBERED_COURT_RULE,
    # "Noteri" is the notary in person, as the endorsement on every notarial
    # instrument signs it -- "Beyoğlu 24. Noteri" -- and named the same office
    # as "Noterliği" while matching nothing at all. _venue_identity keys the two
    # forms as one venue.
    DetectionRule("court_or_authority", _COURT_PLACE_PREFIX + r"\b\d{1,2}\.\s*Noter(?:li[ğg]i|i)" + _AUTHORITY_CASE_END, "HIGH", "Replace with consistent pseudonym or generalize", "COURT_AUTHORITY"),
    DIRECTORATE_RULE,
    TAX_OFFICE_FIELD_RULE,
    # The high-court rule, and the only rule carrying a place prefix AND a
    # trailing sweep. Turkey's regional courts are each officially named for
    # their city ("Ankara Bölge İdare Mahkemesi"), so without the prefix the
    # city sat outside every finding and shipped beside the pseudonymised court
    # -- the same defect as the numbered rules, on nine real venues. The two
    # halves do not interact: the prefix is resolved by _court_start against the
    # place list, the sweep is bounded by [^.\n] as every sweep in this module
    # is, and _named_court_spans already reported this rule's claimed span.
    #
    # "Bölge Adliye Mahkemesi" used to be covered only by accident, because
    # "bölge adliye" is also in GENERIC_COURT_SUFFIX_RULE's alternation, while
    # its sibling "Bölge İdare Mahkemesi" was not -- two court names one word
    # apart behaving differently. Both go through the prefix now.
    HIGH_COURT_RULE,
    ENGLISH_COURT_RULE,
    # Case-sensitive, and its span narrowed to the name -- see COMPANY_RULE in
    # privacy_names.
    COMPANY_RULE,
    LAW_FIRM_RULE,
]

# The two narrowed context rules, by identity, the way GENERIC_COURT_SUFFIX_RULE
# is used: analyze_privacy routes them through _context_sweep_pieces instead of
# finditer().
PRIVILEGE_RULE = next(rule for rule in DETECTION_RULES if rule.category == "privileged_or_confidential")
_CASE_NUMBER_RULE = next(
    rule for rule in DETECTION_RULES
    if rule.category == "case_or_investigation_number" and rule is not ENGLISH_CASE_NUMBER_RULE
)
CRIMINAL_RULE = next(rule for rule in DETECTION_RULES if rule.category == "criminal_allegation")
_PRIVILEGE_SWEEP_RE = re.compile(PRIVILEGE_RULE.pattern, PRIVILEGE_RULE.flags)


def _privilege_trigger_evidence(
    text: str,
    trigger: re.Match,
    clauses: legal_semantics.ClauseIndex,
    evidence: legal_semantics.PrivilegeIndex,
) -> tuple[str | None, int]:
    """(privilege subtype or None, sweep end) for one privilege trigger.

    The clause is the trigger's own, stretched to where its sweep ends. Shared by
    the privilege rule and by _address_trim_triggers, so a trigger the address
    trim relies on is exactly one that reports.
    """
    sweep = _PRIVILEGE_SWEEP_RE.match(text, trigger.start())
    sweep_end = sweep.end() if sweep else trigger.end()
    clause_start, clause_end = clauses.containing(trigger.start(), sweep_end)
    return evidence.evidence(trigger.group(), clause_start, clause_end), sweep_end


_CRIMINAL_SWEEP_RE = re.compile(CRIMINAL_RULE.pattern, CRIMINAL_RULE.flags)


def _span_reach(spans: list[tuple[str, int, int]]) -> tuple[list[int], list[int]]:
    """(sorted span starts, running maximum of their ends), for _reaches_into."""
    ordered = sorted(spans, key=lambda span: span[1])
    reach, furthest = [], -1
    for _word, _start, end in ordered:
        furthest = max(furthest, end)
        reach.append(furthest)
    return [start for _word, start, _end in ordered], reach


def _reaches_into(span_reach: tuple[list[int], list[int]], start: int, end: int) -> bool:
    """Whether some span overlaps [start, end)."""
    starts, reach = span_reach
    index = bisect.bisect_left(starts, end)
    return index > 0 and reach[index - 1] > start


def _criminal_trigger_reports(
    text: str,
    trigger: re.Match,
    look_back_start: int,
    person_reach: tuple[list[int], list[int]],
) -> tuple[bool, int]:
    """(whether this criminal trigger reports, end of its sweep).

    Every trigger reports except "soruşturma" cited as a numbered docket or
    naming the office -- see _is_docket_investigation -- whose text from
    `look_back_start` to the end of its sweep carries neither an allegation
    marker nor a person name (`person_reach` is _span_reach of the person
    spans). The stretch before the trigger counts because Turkish puts the
    allegation and the person in front of the docket: "Ahmet Demir'in rüşvet
    aldığı iddiasıyla başlatılan 2024/12345 sayılı soruşturma dosyası". The
    criminal rule looks back to the sentence start, across wrapped lines;
    _address_trim_triggers looks back only to the clause start, never an
    earlier one, so every trigger the address trim relies on is one that
    reports.
    """
    sweep = _CRIMINAL_SWEEP_RE.match(text, trigger.start())
    if sweep is None:
        return False, trigger.end()
    if (
        _is_docket_investigation(text, trigger.start())
        and not _ALLEGATION_MARKER_RE.search(text, look_back_start, sweep.end())
        and not _reaches_into(person_reach, look_back_start, sweep.end())
    ):
        return False, sweep.end()
    return True, sweep.end()


def _criminal_pieces(
    text: str, clauses: legal_semantics.ClauseIndex, person_spans: list[tuple[str, int, int]]
):
    person_reach = _span_reach(person_spans)

    def accept(trigger: re.Match, _sweep_re: re.Pattern):
        look_back_start = clauses.sentence_start(text, trigger.start())
        reports, sweep_end = _criminal_trigger_reports(text, trigger, look_back_start, person_reach)
        return reports, None, sweep_end

    return _context_sweep_pieces(CRIMINAL_RULE, text, accept)


def _privilege_pieces(text: str, clauses: legal_semantics.ClauseIndex, evidence: legal_semantics.PrivilegeIndex):
    """The privilege rule's pieces: a trigger is accepted only as privilege evidence.

    A trigger that is not evidence in its clause -- "vekil" naming a party's
    lawyer, "Av." in a signature line -- contributes nothing, while a genuine
    marker inside its would-be sweep ("Avukat Ayşe Kaya sözleşmeyi imzaladı,
    müvekkil dosyası ektedir") still contributes its own.
    """
    def accept(trigger: re.Match, _sweep_re: re.Pattern):
        subtype, sweep_end = _privilege_trigger_evidence(text, trigger, clauses, evidence)
        return subtype is not None, subtype, sweep_end

    return _context_sweep_pieces(PRIVILEGE_RULE, text, accept)

# Placeholder prefix per context category, read off the rules themselves so a
# segment cut out of a context span keeps its rule's prefix rather than a
# hardcoded copy of it.
_CONTEXT_PLACEHOLDER_PREFIXES = {
    rule.category: rule.placeholder_prefix
    for rule in DETECTION_RULES
    if rule.category in CONTEXT_SWEEP_CATEGORIES
}


def _context_segment_findings(
    finding: dict,
    text: str,
    cuts: list[tuple[int, int]],
    counters: Counter[str],
    placeholder_state: dict[tuple[str, str], str],
) -> list[dict]:
    """Split one context finding around the identifier spans cut out of it.

    Bounds are tightened onto the stripped text, so the relation _finding()
    guarantees -- sample == text[start:end].strip()[:180] -- holds with no
    slack. Every consumer treats the sample as the literal, contiguous
    document text the span points at -- it is the redaction target
    (redaction_targets_for_document in app.py), the key the PDF exporter maps to
    coordinate boxes, and the string export QA searches the produced file for --
    so a segment must never claim a span wider than the text it reports.
    """
    merged: list[list[int]] = []
    for cut_start, cut_end in sorted(cuts):
        if merged and cut_start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], cut_end)
        else:
            merged.append([cut_start, cut_end])

    spans: list[tuple[int, int]] = []
    cursor = finding["start"]
    for cut_start, cut_end in merged:
        spans.append((cursor, cut_start))
        cursor = cut_end
    spans.append((cursor, finding["end"]))

    prefix = _CONTEXT_PLACEHOLDER_PREFIXES[finding["category"]]
    segments = []
    for index, (start, end) in enumerate(spans):
        noise = _SEGMENT_LEADING_NOISE_RE.match(text, start, end)
        offset = noise.end() if noise else start
        value = text[offset:end].strip()
        if not value or not _segment_is_reportable(value, is_leading=index == 0):
            continue
        # The context finding being replaced already consumed a placeholder
        # number, so the segments start one above it and the preview shows a gap
        # ("[SENSITIVE_HEALTH_DATA_2]" with no _1). Nothing parses placeholder
        # numbers or assumes they are contiguous -- they only have to be stable
        # within one analysis, which _placeholder_for guarantees by keying on the
        # text. Renumbering would mean allocating placeholders after the cut for
        # every rule, which is a larger change for a cosmetic gain.
        placeholder = _placeholder_for(prefix, value, counters, placeholder_state)
        segments.append(
            _finding(
                finding["category"],
                value,
                finding["risk"],
                finding["recommended_action"],
                placeholder,
                offset,
                offset + len(value),
                semantic_type=finding.get("semantic_type"),
            )
        )
    return segments


def _cut_identifiers_from_context_findings(
    findings: list[dict],
    text: str,
    counters: Counter[str],
    placeholder_state: dict[tuple[str, str], str],
) -> list[dict]:
    """Keep another person's direct identifiers out of the context rules' samples.

    A finding's sample is stored, shown to reviewers and shipped to the exporter
    as a redaction target, so a health_data or privileged_or_confidential
    finding that sweeps up a name and a TCKN stores those identifiers a second
    time under a category that has nothing to do with them. The sample cannot be
    masked or elided -- it has to stay literal document text consistent with
    start/end -- so the span is cut instead: wherever a direct identifier sits
    inside a context span, the segments before and after it become the findings
    and the identifier's text belongs to the identifier's own finding only.

    The trailing segments are kept rather than dropped (subject to
    _segment_is_reportable) because they carry the sensitive clause. Dropping
    "hakkında nitelikli dolandırıcılık iddiası ile soruşturma yürütülmektedir"
    would leave the allegation with no CRITICAL finding covering it and
    therefore nothing a reviewer could approve for redaction -- the identifier
    finding beside it redacts only the identifier.

    An identifier span co-extensive with the context span is left alone: that is
    a competing reading of the same text rather than an identifier nested in a
    clause, and _dedupe_findings/build_redacted_preview already rank the two on
    risk. Same "strictly inside" rule as _enclosing_span_end.
    """
    identifier_spans = sorted(
        (item["start"], item["end"])
        for item in findings
        if item["category"] in DIRECT_IDENTIFIER_CATEGORIES
    )
    if not identifier_spans:
        return findings

    result: list[dict] = []
    for finding in findings:
        if finding["category"] not in CONTEXT_SWEEP_CATEGORIES:
            result.append(finding)
            continue
        cuts = []
        for id_start, id_end in identifier_spans:
            overlap_start = max(id_start, finding["start"])
            overlap_end = min(id_end, finding["end"])
            if overlap_start >= overlap_end:
                continue
            if overlap_start == finding["start"] and overlap_end == finding["end"]:
                continue
            cuts.append((overlap_start, overlap_end))
        if not cuts:
            result.append(finding)
            continue
        result.extend(_context_segment_findings(finding, text, cuts, counters, placeholder_state))
    return result


_WEAK_PRIVILEGE_MARKER_RE = re.compile(legal_semantics.PRIVILEGE_WEAK_MARKERS, re.IGNORECASE | re.UNICODE)


def _address_trim_triggers(
    text: str,
    clauses: legal_semantics.ClauseIndex,
    evidence: legal_semantics.PrivilegeIndex,
    person_spans: list[tuple[str, int, int]],
) -> list[tuple[int, int]]:
    """The context triggers an address start may be trimmed after.

    _address_start drops a trigger word from the front of an address because
    "the excluded trigger stays covered by its CRITICAL context finding". A
    lawyer's or client's name-word no longer reports a finding unless its clause
    corroborates privilege, so trimming after an uncorroborated one gave the
    word back to nobody: "Avukat Gül Sokak No: 5" was delivered as
    "Avukat [ADDRESS_1]", the first word of a street name in cleartext. Such a
    trigger is left out here, and the address keeps the word. The decision is
    _privilege_trigger_evidence's, the same one the privilege rule makes.
    """
    triggers = []
    person_reach = _span_reach(person_spans)
    for match in _CONTEXT_TRIGGER_RE.finditer(text):
        if _WEAK_PRIVILEGE_MARKER_RE.fullmatch(match.group()):
            subtype, _sweep_end = _privilege_trigger_evidence(text, match, clauses, evidence)
            if subtype is None:
                continue
        elif _INVESTIGATION_WORD_RE.fullmatch(match.group()):
            clause_start, _clause_end = clauses.containing(match.start(), match.end())
            reports, _sweep_end = _criminal_trigger_reports(text, match, clause_start, person_reach)
            if not reports:
                continue
        triggers.append(match.span())
    return triggers


# The window the prosecutor's office used to sweep: the rest of its sentence,
# up to 120 characters, exactly as GENERIC_COURT_SUFFIX_RULE's tail reads it.
# A lookahead, so an office inside another office's window gets its own.
_PROSECUTOR_TAIL_RE = re.compile(PROSECUTOR_OFFICE_RULE.pattern + r"(?=(?P<office_tail>[^.\n]{0,120}))", PROSECUTOR_OFFICE_RULE.flags)
_FILE_NUMBER_RE = re.compile(r"(?<![0-9/])[0-9]{4}[ \t]*/[ \t]*[0-9]+(?![0-9/])")
# The register a file number names when the word after it says which:
# "2024/12 sayılı iddianame", "2024/12 sayılı iddianamesiyle", "2024/12
# Hazırlık No'lu". The group is the stem, so every inflection is one register.
_FILE_REGISTER_RE = re.compile(
    r"[ \t]+(?:(?:sayılı|sayili|numaralı|numarali|no[.'’]?lu)[ \t]+)?"
    r"(?P<register>esas|karar|iddianame|soruşturma|sorusturma|kovuşturma|kovusturma|hazırl[ıi](?=[kğg]))",
    re.IGNORECASE,
)


def _prosecutor_file_numbers(text: str, findings: list[dict], counters, placeholder_state) -> list[dict]:
    """Every YYYY/N number in the clause after a prosecutor's office, as its own finding.

    While the office was a GENERIC_COURT_SUFFIX_RULE alternative, its
    120-character sweep put whatever number followed it inside the office's
    finding -- over-redacting the clause, but never leaving the number out.
    PROSECUTOR_OFFICE_RULE ends at the office's name, and the case-number rule
    reads only the labelled and cited forms, so a number written any other way
    ("Başsavcılığı'nın 2024/123 numaralı dosyası", "... Hazırlık No'lu",
    "... nolu", "... tarafından yürütülen 2024/123 sayılı dosya") would sit in
    no finding at all: unapprovable, so shipped. Measured on 192 office x
    number-form sentences: 28 such regressions before this pass, 0 after.

    So each office's own window is still searched, and a number in it that no
    case-number finding already covers is reported, keyed on the register the
    word after it names ("2024/12 sayılı iddianame" is the iddianame register),
    or on the soruşturma register when none follows, so it shares a pseudonym
    with "Soruşturma No: <the same number>".
    """
    taken = [(f["start"], f["end"]) for f in findings if f["category"] == "case_or_investigation_number"]
    added = []
    for office in _PROSECUTOR_TAIL_RE.finditer(text):
        tail_start = office.start("office_tail")
        for number in _FILE_NUMBER_RE.finditer(office.group("office_tail")):
            start, end = tail_start + number.start(), tail_start + number.end()
            if any(s < end and start < e for s, e in taken):
                continue
            taken.append((start, end))
            value = text[start:end]
            register = _FILE_REGISTER_RE.match(text, end)
            key = legal_semantics.fold(register.group("register") if register else "soruşturma") + "\x00" + re.sub(r"\s", "", value)
            placeholder = _placeholder_for(_CASE_NUMBER_RULE.placeholder_prefix, key, counters, placeholder_state)
            added.append(
                _finding(_CASE_NUMBER_RULE.category, value, _CASE_NUMBER_RULE.risk, _CASE_NUMBER_RULE.action, placeholder, start, end)
            )
    return added
