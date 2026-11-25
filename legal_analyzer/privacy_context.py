"""Context rules: the health, criminal-allegation and privilege sweeps.

Their trigger vocabulary, the character class a sweep runs over, the sweep and
segment machinery, document-context classification and the language-evidence
spans. The pieces that look a rule up in DETECTION_RULES by identity live in
``privacy_rules``, which is built from this module. Moved out of
``privacy.py`` unchanged; that module re-exports every name here.
"""

from __future__ import annotations

import re

from .docx_redactor import TURKISH_I_FORMS
from legal_analyzer import legal_semantics
from .classifier import entity_spans_of

from .privacy_common import (
    DetectionRule,
)


# One character of the trailing sweep used by health_data, criminal_allegation
# and privileged_or_confidential. It is "any character except a newline or a
# period", with three exceptions, each for a period that does NOT end a
# sentence: after a single capital letter ("T.C.", "A.Ş."), between two digits
# ("22.09.2025"), and after one of a short enumerated list of lowercase
# abbreviations ("Ltd. Şti.", "vb.", "Av."). Each is justified below.
#
# The plain [^.\n] class stopped the sweep at the first period of "T.C.", so in
# "hasta Leyla Kaya T.C. Kimlik No: 10000000146 kanser" the match ended at
# "Kaya T" and the diagnosis after the identifier -- the CRITICAL part of the
# clause -- got no finding at all, and therefore no way for a reviewer to
# approve it for redaction.
#
# Every other period still stops the sweep, so a real sentence boundary ends it:
# "hasta iyileşti. Yeni cümle Ahmet Yılmaz" stops after "iyileşti" and the name
# in the next sentence stays out of this finding. The exception is deliberately
# scoped with (?-i:...) because these rules run under re.IGNORECASE, which would
# otherwise make the character class match any letter and let a lowercase
# single-letter word ("a. bendi") through the stop. The leading \b keeps it to a
# one-letter word: the period in "iyileşti." follows "i", but there is no word
# boundary before that "i", so it remains a stop.
#
# Accepted cost, stated because the exception has one: a period ending an
# abbreviation is written exactly like a period ending a sentence that HAPPENS
# to end on an abbreviation ("... Yıldızlı Örnek Holding A.Ş. Sonraki cümlede ..."), and
# no regex can tell them apart. Measured on 336 generated probes of that shape,
# the sweep crossed into the next sentence in all 336, against 288 probes where
# it newly reached a diagnosis or allegation that no finding covered before.
# That is over-detection, which is the direction this engine is built to fail
# in: the extra segments are dismissible reviewer noise, they raise residual
# risk rather than lower it, direct identifiers inside them are still cut out by
# _cut_identifiers_from_context_findings, and build_redacted_preview clips
# rather than drops the findings they overlap, so nothing is printed raw. A
# missed CRITICAL clause has none of those consolations -- no reviewer is shown
# it, so nobody can approve it for redaction.
#
# Also asymmetric, harmlessly: "I." is crossed (word boundary before the
# single capital) while "II." is not (no boundary before the second I).
#
# The single-capital exception left two shapes as stops, and both cost the same
# reachable CRITICAL miss it was written to fix. They are exceptions two and
# three, added here with their own measurements.
#
# TWO: a date's period. The address rule has always allowed a period after a
# digit via a plain (?<=\d)\., but copying that verbatim is what made this a
# separate change: it also crosses an ordinal institution name, so
# "Müvekkil ... Kadıköy 2. Noterliği onaylı örneği" starts storing an authority
# name in a privileged_or_confidential sample -- the direction the identifier
# cut was added to move AWAY from. The two are separable on one character:
# a date's period sits BETWEEN digits ("22.09.2025") while an ordinal's is
# followed by a space ("Kadıköy 2. Noterliği"). (?<=\d)\.(?=\d) therefore
# crosses the date and leaves the ordinal a stop. Verified against all three
# pinned samples: the privileged one and the tied-address one are unchanged;
# only the medical one grows, across a date and no further, because the
# newline after "22.09.2025" stops the sweep.
#
# THREE: a lowercase abbreviation -- "Ltd. Şti.", "vb.", "Av.", "Dr.", the
# street-address forms -- whose period follows a lowercase letter and so gets
# no help from the single-capital rule. The clause after
# "Müvekkil Yıldız Ltd. Şti. ile gizli görüşme" was uncovered.
#
# These are ENUMERATED rather than generalised to "any short token", because a
# general rule would cross the period after every short sentence-final word. A
# variable-width alternation cannot go inside ONE lookbehind -- Python's re
# rejects it with "look-behind requires fixed-width pattern" -- so each entry
# gets its own fixed-width lookbehind, joined as alternatives.
#
# Each entry is case-insensitive: filings write "Ltd. Şti." in body text,
# "LTD. ŞTİ." in headers and "ltd. şti." when sloppy.
#
# The i-family letters are spelled out rather than left to (?i:). Python does
# relate i/I/ı/İ under IGNORECASE -- measured, not assumed -- but
# docx_redactor.py refuses to depend on that, calling it "an undocumented
# implementation detail: nothing in the re documentation promises 'I' will keep
# matching 'ı'", and enumerates TURKISH_I_FORMS by hand instead. The same
# argument applies here and the consequence is the same kind: if that folding
# ever narrows, "ŞTİ." stops being crossed and the clause after it loses the
# CRITICAL finding this exception exists to give it. "Şti" is the only entry
# containing an i-family letter, so the cost of taking the house rule's side is
# one expanded string, and _i_forms() below is what expands it.
#
# What each entry costs cannot be read off the gold set: 14 of the 15
# candidates appear zero times in it, and adding all of them singly or together
# moves gold-set precision by 0.0000. Priced on generated probes instead, the
# same way the single-capital exception was: each entry buys the clause after
# its own mid-sentence use (36/48 probes) and costs a crossing when a sentence
# genuinely ends on it (48/48) -- the A.Ş. trade above, unchanged in kind.
#
# Two candidates were dropped, because for them the sentence-final position is
# ordinary prose rather than a typographic coincidence:
#   * "No" -- English "no." ends sentences routinely and these rules run on
#     English documents too ("medical", "patient", "privileged" are triggers).
#     Its buy is thin: the common address form is "Sok. No: 5", already crossed
#     via "Sok" with no period after "No", and what follows "Esas No." is a
#     case number, a direct identifier the cut removes from the span anyway.
#   * "age" -- English "age." ends sentences, and the Turkish "a.g.e." is
#     written with a dot after every letter, so the single-token form this
#     would target is rare.
_CONTEXT_SWEEP_ABBREVIATIONS = (
    "Ltd", "Şti", "Sti",       # "Yıldız Ltd. Şti." -- both, since each stops the other.
                               # "Sti" is spelled out because _i_forms() expands the
                               # i-family and NOTHING else: it does not relate 'Ş' to 'S',
                               # and it must not be taught to. The company suffixes (privacy_names)
                               # fold that pair by hand as _S_FORMS, and they also
                               # deliberately do NOT fold it for "A.Ş." -- courts anonymise
                               # parties to their initials, so a diacritic-free "A.S." is
                               # more often a redacted person than a company. A blanket
                               # Ş/S fold inside _i_forms() would reach that decision and
                               # silently reverse it. Turkish is routinely typed without
                               # diacritics, so "Ltd. Sti." is ordinary, not a typo; before
                               # this entry the sweep stopped dead at its period and the
                               # clause after it carried no finding, which is the
                               # unredactable-by-construction failure the whole exception
                               # list exists to prevent.
                               #
                               # This entry is NOT free, and an earlier version of this
                               # comment claimed it was. "STI." ends English sentences --
                               # sexually transmitted infection -- and it does so in
                               # exactly the documents health_data runs on, whose triggers
                               # include the English "medical" and "patient". Measured:
                               # "The medical record notes a prior STI. Kadikoy 2.
                               # Noterligi onayli ornegi" widens the health_data span from
                               # 'medical record notes a prior STI' to '... STI. Kadikoy
                               # 2', pulling an authority name into a CRITICAL sample --
                               # court_or_authority is not a DIRECT_IDENTIFIER_CATEGORY,
                               # so the cut does not take it back out.
                               #
                               # Kept anyway, and this is the criterion at the head of this
                               # block applied rather than dodged. "No" and "age" were
                               # dropped because their buy was thin AND their sentence-final
                               # use is ordinary prose. Here only the second half holds: the
                               # buy is a Turkish company form written the way Turkish is
                               # actually typed, and losing it costs a whole clause its only
                               # CRITICAL finding. The cost runs the safe way -- a health
                               # span reaching too far is over-redaction, not a leak -- and
                               # its precondition is the literal token "STI." at an English
                               # sentence end, far narrower than the ordinal case rejected
                               # above, which fired on every Turkish filing. Pinned by
                               # test_english_sti_sentence_end_is_crossed_as_an_accepted_cost
                               # so the trade is observed rather than asserted.
    "Doc",                     # "Doç" without the cedilla -- the same fold as "Sti", for the
                               # same reason: _i_forms() relates no letter but the i-family,
                               # so the "Doç" entry below never matched "Doc." and the sweep
                               # stopped there. "Doç. Dr." heads the physician's name in a
                               # Turkish medical record, so what came next was typically the
                               # diagnosis: "hasta ... Doc. Dr. X gözetiminde kanser tanısı
                               # aldı" carried health_data on 'hasta ... Doc' and nothing on
                               # the diagnosis -- in no finding, so never redacted.
                               #
                               # Priced the way "Sti" was, and dearer than it. English "doc."
                               # ends sentences in ordinary prose ("see the attached doc."),
                               # and health_data's triggers include "medical", "health" and
                               # "patient" (the privilege sweep has "privileged"). Measured on
                               # generated probes: the Turkish title, typed without the
                               # cedilla, after seven health triggers in four title forms,
                               # newly reaches the clause after the name in 168/168 (0/168
                               # without the entry); an English health sentence ending on
                               # "doc." is now crossed in 24/30 (0/30 without) -- the six
                               # that stop end on "report.doc.", whose earlier period already
                               # stops the sweep. In those 24 the span takes in the next
                               # sentence -- "... attached doc. Kadikoy 2" puts part of an
                               # authority name into a CRITICAL sample, as "STI." does -- but
                               # stored no direct identifier: the cut takes names, phones and
                               # the rest back out. The gold set has no English "doc."; its
                               # one "Doc." is medical_report_unaccented, added with this entry.
                               #
                               # Kept on the criterion above, as "Sti" was: the sentence-final
                               # use is ordinary prose, but the buy is not thin -- it is the
                               # one clause in a medical record most likely to be the
                               # diagnosis -- and the cost runs the safe way: a span reaching
                               # too far is over-redaction, not a leak. Pinned by
                               # test_english_doc_sentence_end_is_crossed_as_an_accepted_cost.
    "vb", "vs",                # "ve benzeri" / "vesaire"; "vs." is also English "versus"
    "Av", "Dr", "Prof", "Doç",  # professional titles, always followed by a name
    "Sok", "Cad", "Mah", "Apt",  # street-address structure words
    "Bkz",                     # "bakınız"; always followed by the reference
)

def _i_forms(abbreviation: str) -> str:
    """Spell an abbreviation as a class-per-character, expanding the i-family.

    Every character becomes a class holding both its cases, and any of i/I/ı/İ/î/Î
    becomes a class holding all of them -- so "Şti" matches "ŞTİ", "ŞTI", "şti"
    and "ştı" without asking re.IGNORECASE to relate the dotted and dotless
    letters. Fixed width per character, which the lookbehind requires.

    CONSTRAINT ON CALLERS: pass only characters whose case mapping stays a
    single character. str.upper()/str.lower() can WIDEN one -- 'ß'.upper() is
    'SS' and 'İ'.lower() is 'i' plus a combining dot, two characters -- and a
    multi-character form dropped into a class below silently changes what that
    class matches, and breaks the fixed width the sweep's lookbehind needs.
    case_variants() in docx_redactor.py filters on len(form) == 1 for exactly
    this reason; this helper does not, because it is built for the abbreviation
    and legal-suffix vocabularies below, where nothing widens (verified over
    _CONTEXT_SWEEP_ABBREVIATIONS and every company suffix word: zero widening
    characters, and 'İ' is taken by the i-family branch before .lower() is ever
    called on it).

    The note is here rather than in a commit message because this helper has
    already grown one caller beyond the fixed abbreviation tuple it was written
    for -- the company_name suffixes -- and the next caller will read this
    docstring, not the history.
    """
    parts = []
    for char in abbreviation:
        if char in TURKISH_I_FORMS:
            parts.append("[" + "".join(sorted(TURKISH_I_FORMS)) + "]")
        else:
            forms = {char, char.upper(), char.lower()}
            parts.append("[" + "".join(sorted(forms)) + "]")
    return "".join(parts)


_CONTEXT_SWEEP_CHAR = "(?:" + "|".join(
    [
        r"[^.\n]",
        r"(?<=\b(?-i:[A-ZÇĞİÖŞÜ]))\.",
        r"(?<=\d)\.(?=\d)",
    ]
    + [rf"(?<=\b{_i_forms(abbreviation)})\." for abbreviation in _CONTEXT_SWEEP_ABBREVIATIONS]
) + ")"

# Share the trigger vocabulary with address-boundary detection. Match the
# triggers alone there: a full context sweep can hide a later trigger beside
# an address ("hasta olan hasta Gül Sok"). Keep the existing word boundaries.
#
# Two of these vocabularies are legal language as often as they are sensitive
# content, and legal_semantics decides which:
#
#   * "cezai" is the adjective of the contract-law term "cezai şart" / "cezai
#     koşul" (a contractual penalty) as well as of criminal liability. Before
#     one of legal_semantics.PENALTY_CLAUSE_HEADS it is not a trigger, so an
#     NDA's penalty clause and its "CEZAİ ŞART" heading are no longer criminal
#     allegations. Every other trigger in the clause still fires: a lookahead
#     on one word cannot hide a "şüpheli" or a "suç" beside it.
#   * The privilege vocabulary no longer carries "gizli", "confidential",
#     "ticari sır" or "trade secret". Those are contractual confidentiality,
#     not attorney-client privilege -- an NDA is built out of them, and they
#     replaced its definition, exceptions and obligations with
#     [PRIVILEGED_CONTENT_n_REDACTED]. What is left is privilege evidence:
#     strong markers on their own, and the words that merely NAME a lawyer
#     ("vekil", "av.", "avukat") only with an advice or communication word in
#     the same clause -- see _privilege_pieces.
_CONTEXT_TRIGGERS = {
    "health_data": r"\b(?:sağlık|saglik|hasta|hastane|cerrahi|tedavi|teşhis|teshis|reçete|recete|medical|health|patient)\b",
    "criminal_allegation": (
        r"\b(?:suç|suc|şüpheli|supheli|sanık|sanik|cezai(?!\s+" + legal_semantics.PENALTY_CLAUSE_HEADS + r")"
        r"|kamu davası|arama|el koyma|soruşturma|sorusturma)\b"
    ),
    "privileged_or_confidential": (
        r"\b(?:" + legal_semantics.PRIVILEGE_STRONG_MARKERS + r"|" + legal_semantics.PRIVILEGE_WEAK_MARKERS + r")\b"
    ),
}
_CONTEXT_TRIGGER_RE = re.compile("|".join(_CONTEXT_TRIGGERS.values()), re.IGNORECASE | re.UNICODE)

# "soruşturma" cited as a docket rather than as an investigation of someone
# (gold/README.md, "Docket references are not allegations"). The word is a
# criminal_allegation trigger, so every procedural citation of an
# investigation file became a CRITICAL finding:
# "2024/12345 sayılı soruşturma dosyası incelendi", "Soruşturma No: 2024/12345
# olan dosya incelendi", and the office name "Soruşturma Bürosu". Its case
# number already has its own HIGH finding; a CRITICAL badge beside it on
# "olan dosya incelendi" teaches a reviewer that CRITICAL does not mean what
# it says.
#
# A docket is the word tied to a case number, or the office name:
#
#   * right after a cited number, optionally through "sayılı"/"numaralı":
#     "2024/12345 sayılı soruşturma", "2025/4410 Soruşturma No'lu";
#   * right before a numbered label: "Soruşturma No: 2024/12345",
#     "Sorusturma No : 2026 / 3377";
#   * the office name "Soruşturma Bürosu".
#
# The number is required, because "soruşturma dosyası" with no number is just
# as often "X'in soruşturma dosyası" -- a person's file -- and "2 soruşturma"
# is a count, not a citation. Both stay triggers.
#
# Such a trigger is dropped only when its own sweep carries no
# _ALLEGATION_MARKER_RE word AND no person name anywhere in its sentence up
# to the end of that sweep (_criminal_trigger_reports).
# It is NOT removed from the vocabulary: the docket word is sometimes the only
# trigger in front of an allegation. "suçunu" is not "suç" to the \b-bounded
# trigger, so in "2024/12348 sayılı soruşturma dosyasında X'in dolandırıcılık
# suçunu işlediği iddia edilmektedir" nothing else reports the clause, and
# dropping the word outright would ship the allegation with no finding at all.
# The person check is what makes the marker list fail closed: a docket clause
# naming someone is about that person ("... dosyasında Ahmet Demir'in ifadesi
# alındı"), whether or not its offence word is one the list knows.
#
# Bare "soruşturma" ("X hakkında soruşturma başlatılmıştır") is untouched:
# that is an investigation of a person, not a reference to a file.
_INVESTIGATION_WORD = r"(?:soruşturma|sorusturma)"
_INVESTIGATION_WORD_RE = re.compile(_INVESTIGATION_WORD, re.IGNORECASE)
_DOCKET_CASE_NUMBER = r"[0-9]{4}[ \t]*/[ \t]*[0-9]+"
# Matched against the text ENDING at the trigger (a variable-width lookbehind
# is not available in Python's re, so the caller searches a short window).
_DOCKET_NUMBER_BEFORE_RE = re.compile(
    r"\b" + _DOCKET_CASE_NUMBER + r"[ \t]+(?:(?:sayılı|sayili|numaralı|numarali)[ \t]+)?\Z",
    re.IGNORECASE,
)
_DOCKET_NUMBER_BEFORE_WINDOW = 40
_DOCKET_AFTER_RE = re.compile(
    _INVESTIGATION_WORD
    + r"(?:[ \t]+(?:no|numarası|numarasi)[ \t]*[:.]?[ \t]*" + _DOCKET_CASE_NUMBER
    + r"|[ \t]+(?:bürosu|burosu)\b)",
    re.IGNORECASE,
)


def _is_docket_investigation(text: str, start: int) -> bool:
    """Whether the "soruşturma" at `start` cites a numbered docket or names the office."""
    if _DOCKET_AFTER_RE.match(text, start):
        return True
    window = text[max(0, start - _DOCKET_NUMBER_BEFORE_WINDOW):start]
    return _DOCKET_NUMBER_BEFORE_RE.search(window) is not None


# A word that turns a docket clause into an allegation. Prefix-matched, so the
# inflected forms count: "suçunu", "suçlaması", "iddia edilen", "isnat
# edilmektedir", "atılı suç", "işlediği", "tutuklandığı". Beside the allegation
# vocabulary it carries the measures (gözaltı, tutuklama, hüküm, mahkûmiyet),
# common offence names, and "tespit": a finding of fact inside a docket clause
# is reported rather than guessed procedural. The party-role words are listed
# although they are triggers themselves, so a docket clause naming a suspect
# keeps the clause from the docket word on, not only from the suspect.
# Over-reach is the safe direction here: "Dolandırıcılık Suçları Soruşturma
# Bürosu" keeps its finding, and so does a docket word in the same clause. The list is bounded
# and an offence it does not name still ships when no person is named either;
# that residual is the price of not flagging every docket.
_ALLEGATION_MARKER_RE = re.compile(
    r"\b(?:suç|suc|iddia|isnat|isnad|itham|atılı|atili|işledi|isledi|tespit"
    r"|şüpheli|supheli|sanık|sanik|fail\b|cürüm|curum"
    r"|gözaltı|gozalti|tutuk|hüküm|hukum|mahk[uû]m|mahkûm|cezalandır|cezalandir"
    r"|rüşvet|rusvet|dolandır|dolandir|hırsız|hirsiz|zimmet|irtikap|kaçakçı|kacakci"
    r"|uyuşturucu|uyusturucu|yaralama|öldür|oldur|kasten|taksirle|tehdit|hakaret|şantaj|santaj"
    r"|yağma|yagma|gasp|darp|cinsel|taciz|istismar|sahte|aklama|terör|teror|örgüt|orgut)",
    re.IGNORECASE,
)


class _SweepPiece:
    """A context-sweep span shaped like the re.Match the rule loop reads."""

    re = re.compile("")
    lastindex = None

    def __init__(self, text: str, start: int, end: int) -> None:
        self._text, self._start, self._end = text, start, end

    def group(self, _index=0) -> str:
        return self._text[self._start:self._end]

    def start(self, _index=0) -> int:
        return self._start

    def end(self, _index=0) -> int:
        return self._end


_CONTEXT_TRIGGER_RES = {
    category: re.compile(pattern, re.IGNORECASE | re.UNICODE) for category, pattern in _CONTEXT_TRIGGERS.items()
}


def _context_sweep_pieces(rule: DetectionRule, text: str, accept):
    """Yield (piece, semantic type): the union of every ACCEPTED trigger's own sweep.

    Used for the two context rules whose vocabulary this change narrowed
    (criminal_allegation, privileged_or_confidential). finditer() over
    "trigger + 160-character sweep" is non-overlapping: a trigger inside an
    earlier sweep starts nothing, and where the chain ends depends on which
    triggers exist. Removing one therefore moved the chain, not just the
    removed sweep: with "cezai şart" no longer a trigger, the chain in
    "... cezai şart ödenir ve ... suç teşkil eden ... şüpheli sıfatıyla
    ifadesi alınan çalışanın daha önce iki kez hırsızlıktan hüküm giydiği ..."
    began at "suç" and its cap ended before the prior conviction that the old
    chain's second match covered -- which then shipped.

    So each accepted trigger contributes [trigger, end of its own sweep), and the
    union is emitted as pieces: one starting at a trigger, and, where a later
    trigger's sweep runs past the previous piece, a continuation from that end.
    A sweep's end only grows with its trigger's position inside one sentence (the
    stop character is shared and the cap moves right), so the union over any set
    of triggers covers what the old chain covered from any of them; what is
    released is only text that no remaining trigger reaches -- the contract
    language in front of the first real trigger. Each piece lies inside one
    sweep, so its sample fits the 180-character cap a sample is cut to.

    A piece that starts at its trigger keeps the raw match bounds, exactly as
    finditer() reported them. A continuation is tightened onto its text and
    dropped when it carries no word, the same rule _segment_is_reportable
    applies to a segment left between two identifiers.
    """
    sweep_re = re.compile(rule.pattern, rule.flags)
    cursor = 0
    for trigger in _CONTEXT_TRIGGER_RES[rule.category].finditer(text):
        accepted, semantic_type, sweep_end = accept(trigger, sweep_re)
        if not accepted or sweep_end <= cursor:
            continue
        start = trigger.start()
        if start < cursor:
            noise = _SEGMENT_LEADING_NOISE_RE.match(text, cursor, sweep_end)
            start = noise.end() if noise else cursor
            stripped = text[start:sweep_end].rstrip()
            if not stripped or not _segment_is_reportable(stripped, is_leading=False):
                cursor = sweep_end
                continue
            yield _SweepPiece(text, start, start + len(stripped)), semantic_type
        else:
            yield _SweepPiece(text, start, sweep_end), semantic_type
        cursor = sweep_end


# The two context vocabularies as they stood before legal_semantics learned that
# some of their words are contract law ("gizli", "confidential", "ticari sır",
# "trade secret", a bare "vekil"/"av."/"avukat", "cezai" in "cezai şart"). They
# are used for ONE thing: the language signal.
#
# Every finding's span is excluded from the Turkish-word count, so the language
# gate measured the document AROUND those sweeps. Retiring a trigger removed a
# span, which can only ADD Turkish evidence -- the clearing direction. Measured:
# an all-English witness form naming three lawyers as "Avukat X, X ve Ortakları
# Hukuk Bürosu" went from (0 Turkish, refused) to (3 Turkish -- the "ve" of each
# firm name -- read as TR, "Allowed for external LLM use") with a London
# address, a UK mobile, a national insurance number and a health line in it.
# A proper noun is not a language, and that was a fourth route through the same
# door. So the language signal keeps every exclusion the detector had before
# this change: what is offered for redaction may shrink, what clears the gate
# may not grow. Excluding more Turkish evidence can only refuse more.
_RETIRED_CONTEXT_SWEEPS = tuple(
    re.compile(trigger + _CONTEXT_SWEEP_CHAR + r"{0,160}", re.IGNORECASE | re.UNICODE)
    for trigger in (
        r"\b(?:müvekkil|muvekkil|vekil|av\.|avukat|attorney-client|privileged|gizli|confidential|ticari sır|trade secret)\b",
        # The whole criminal vocabulary as it stood, not "cezai" alone: the
        # old chain's spans depend on every trigger in it, so only the full
        # list reproduces them.
        r"\b(?:suç|suc|şüpheli|supheli|sanık|sanik|cezai|kamu davası|arama|el koyma|soruşturma|sorusturma)\b",
    )
)


def language_evidence_spans(text: str, findings: list[dict]) -> list[tuple[int, int]]:
    """The spans `detect_language` must not count Turkish words inside.

    Every finding's span (see classifier.entity_spans_of), plus the sweeps of the
    retired context vocabulary above. analyze_privacy and
    app.backfill_language_coverage both call this, so detection and the backfill
    reach the same answer from the same text.
    """
    spans = entity_spans_of(findings)
    for pattern in _RETIRED_CONTEXT_SWEEPS:
        spans.extend(match.span() for match in pattern.finditer(text or ""))
    return spans


def classify_document_context(filename: str, text: str) -> dict:
    """Coarse document-level signals for the review checklist.

    Privilege and commercial confidentiality are reported apart. They used to
    be one flag raised by "gizli" as readily as by "müvekkil", so every NDA was
    told to review attorney-client privilege -- the same conflation that
    deleted its clauses. `appears_privileged_or_confidential` is kept, as the
    union of the two, for every reader that already keys on it.
    """
    sample = f"{filename}\n{text[:5000]}".lower()
    privileged = any(token in sample for token in ["müvekkil", "muvekkil", "vekil", "av.", "attorney-client", "privileged"])
    # Word-bounded: as a bare substring "nda" is inside "arasında" and every
    # other locative, so every Turkish document read as an NDA.
    confidential = bool(re.search(r"\b(?:gizli|gizlilik|confidential|ticari sır|trade secret|nda)\b", sample))
    return {
        "appears_to_contain_personal_data": any(token in sample for token in ["av.", "şüpheli", "supheli", "t.c", "adres", "email", "telefon", "müvekkil"]),
        # "cezai" only outside the contract-law term, as in the trigger itself.
        "appears_to_contain_sensitive_data": any(token in sample for token in ["sağlık", "saglik", "suç", "suc", "şüpheli", "supheli", "soruşturma"])
        # str.lower() turns "CEZAİ" into "cezai" plus a combining dot, which the
        # lookahead would read as "not followed by şart"; the dot goes first.
        or bool(re.search(r"cezai(?!\s+" + legal_semantics.PENALTY_CLAUSE_HEADS + r")", sample.replace("\u0307", ""))),
        "appears_privileged": privileged,
        "appears_commercially_confidential": confidential,
        "appears_privileged_or_confidential": privileged or confidential,
        "contains_contextual_reidentification_risk": any(token in sample for token in ["soruşturma no", "esas no", "mahkemesi", "savcılığı", "adres", "marka", "ek-"]),
    }


# The three rules that match a sensitive *clause* rather than an identifier:
# a trigger keyword plus a trailing sweep of up to 120-160 characters. The sweep
# is what makes them useful -- an allegation, a diagnosis or a privileged
# instruction is a clause, not a token -- and it is also how an unrelated
# person's name and national id ended up stored a second time, in a finding
# whose category has nothing to do with them ("hasta Leyla Kaya (TCKN:
# 66666666660), Özel Marmara Hastanesi'nde 22"). This is the same trailing-span
# over-reach that party_role was fixed for, except party_role could drop its
# context outright and these cannot: the clause IS the finding.
CONTEXT_SWEEP_CATEGORIES = {"health_data", "criminal_allegation", "privileged_or_confidential"}

# A word, for the purpose of deciding whether a leftover segment says anything:
# a run of two or more letters. Digits and punctuation do not count, so "22",
# "),", and "(" are wordless.
_SEGMENT_WORD_RE = re.compile(r"[^\W\d_]{2,}", re.UNICODE)

# Clause punctuation a cut can orphan at the FRONT of a leftover segment.
#
# The address rule deliberately stops before an abbreviation's period: its tail
# is (?:[^.\n]|(?<=\d)\.){0,140}, and "sok\.?" backtracks off the dot so the
# trailing \b can hold. So in "hasta Gul Sok. 5 adresinde ikamet etmektedir"
# the address span is "Gul Sok" [6,13) and the period at 13 -- which belongs to
# "Sok." -- lands outside it. The health span covers the whole clause and is cut
# around the address, so that orphaned period became the FIRST character of the
# trailing health finding: '. 5 adresinde ikamet etmektedir'. A finding's sample
# is what a reviewer reads and what the exporter searches for, and a CRITICAL
# health finding that opens on another rule's abbreviation dot reads as a bug in
# the reviewer UI.
#
# Only whitespace and clause separators are consumed, never a letter, digit,
# currency symbol or bracket: dropping ".5 mg" to "5 mg" is right, dropping
# "(HIV" to "HIV" or "TL500" is not this function's call to make. A punctuation
# character carries no identifier, so no coverage that matters is lost -- and a
# segment that is ONLY punctuation was already dropped by
# _segment_is_reportable(), which counts words, not characters.
#
# No "^" anchor: this is used as .match(text, start, end), which already
# anchors at start, while "^" would additionally demand the real start of a
# line and so silently never fire at any offset past the first character.
_SEGMENT_LEADING_NOISE_RE = re.compile(r"[\s.,;:]+")

# All-capital abbreviations that LABEL a direct identifier rather than say
# anything themselves. They are the only reason a leftover segment can consist
# of a single all-caps token that is not content -- "(TCKN:" is the punctuation
# between a name and the national id that was just cut out of the span, and it
# must not become a CRITICAL finding, while "HIV" in the same position must.
# Form alone cannot separate those two, so this list is required.
#
# It is written out rather than derived from the identifier rules' own patterns.
# That derivation was tried and does not work, for three reasons:
#   * "TCKN" -- the token this exists for -- appears in no pattern at all. The
#     national-id rule spells its prefix "T\.?\s*C\.?", two single letters that
#     no word extractor keeps, and documents write "TCKN".
#   * Character classes mangle the words: "MERS[Ii]S" extracts as "MERSS".
#   * The case-number rule's pattern carries "sorusturma", "karar", "esas" and
#     "dosya", which are content words in criminal_allegation's own domain, so
#     deriving would trade this over-drop for a worse one.
# Only all-capital forms are listed. A capitalised label ("Kimlik", "No") on its
# own after a cut is rare, and reporting it is the safe direction. Keep this list
# to identifier labels -- it is the one thing allowed to suppress a segment.
IDENTIFIER_LABEL_ABBREVIATIONS = frozenset({"TCKN", "TC", "VKN", "MERSIS", "MERSİS", "IBAN", "UYAP"})


def _segment_is_reportable(segment: str, is_leading: bool) -> bool:
    """Whether a segment left over after the identifier cut earns its finding.

    The leading segment always starts on the rule's trigger keyword -- all three
    patterns are anchored on one -- so a single word there is the keyword itself
    and is kept. That is exactly what party_role keeps: the signal word without
    the clause, and it is what holds gold-set recall on labels like "Şüpheli
    Kemal Arslan hakkında nitelikli dolandırıcılık", whose leading "Şüpheli"
    segment is the part that still matches.

    Any other segment has lost the keyword, and is kept if it carries any word at
    all. The one exception is a segment whose ONLY word is an all-capital
    identifier label ("(TCKN:"): that is the punctuation between a name and the
    national id that was cut out around it, it says nothing on its own, and the
    identifier's own finding already covers it.

    Everything else is reported, including a single word. That word is often the
    whole sensitive point -- "kanser", "HIV", "Alzheimer" -- and an earlier
    version of this rule that asked non-leading segments for two words, and a
    later one that also dropped a lone Capitalised word as a proper noun, both
    silently lost it: the diagnosis stayed in the document with no CRITICAL
    finding, so no reviewer could approve it for redaction. Over-detection is
    the direction this system is built to fail in.

    Accepted cost, stated because the rule has it: a lone place-name tail such
    as "), Kadıköy 2" is now reported as a CRITICAL segment. It is a real but
    rare noise finding, and a reviewer dismisses it in one click. That is the
    cheap side of this trade -- a missing finding cannot be dismissed, because
    nobody is shown it.
    """
    words = _SEGMENT_WORD_RE.findall(segment)
    if is_leading:
        return len(words) >= 1
    if not words:
        return False
    return not (len(words) == 1 and words[0].upper() in IDENTIFIER_LABEL_ABBREVIATIONS)
