"""Names: people, party roles, companies and law firms.

The title and role regexes, the company legal-form suffixes, the party-role and
law-firm rules, and the surname logic that finds later mentions of a named
person. Moved out of ``privacy.py`` unchanged; that module re-exports every
name here. ``_person_spans`` stays in ``privacy.py`` because it reads
``privacy.detect_english_person_names``.
"""

from __future__ import annotations

import bisect
import functools
import re
from collections import Counter

from legal_analyzer import legal_semantics, turkish_places
from legal_analyzer import english_names, turkish_names
from legal_analyzer.turkish_names import COMPANY_SUFFIX_RE, is_party_role_term, tr_fold

from .privacy_common import (
    DetectionRule,
    DIRECT_IDENTIFIER_CATEGORIES,
    _finding,
    _placeholder_for,
)
from .privacy_context import (
    CONTEXT_SWEEP_CATEGORIES,
    _i_forms,
)
from .privacy_courts import (
    _venue_prefix_start,
)


# --- company_name legal-form suffixes -------------------------------------
#
# The company_name rule is the ONE rule that runs without re.IGNORECASE, and
# that is deliberate. Do not "fix" it by adding the flag back: its leading
# [A-ZÇĞİÖŞÜ] class is what keeps a match from starting on a lowercase letter
# in the middle of a word, and IGNORECASE folds that class too, so
# "davali yildiz ltd. sti. adresinde" would start matching. The suffixes below
# therefore carry their own case classes instead of inheriting the flag, which
# leaves the leading class case-sensitive by construction -- no (?-i:) scope to
# get lost in a later edit.
#
# Every suffix is built through _i_forms(), for the reason docx_redactor.py
# gives at length: CPython's re does relate 'I' to 'ı' and 'İ' to 'i' under
# IGNORECASE, but that is an undocumented property of sre's internal
# equivalence table, not something the re documentation promises. Inheriting it
# here is worse than inheriting it in the context sweep, not better. company_name
# is in DIRECT_IDENTIFIER_CATEGORIES, so if that folding ever narrows, a company
# written "Ltd. Şti." stops matching, has_direct_identifiers() goes quiet, and
# the external-LLM gate silently loses one of its six arms -- which is precisely
# the bug this construction was written to close.
#
# _i_forms() spells both cases of every character and the whole i-family for
# i/I/ı/İ, so "ANONİM" also covers the diacritic-free "ANONIM" for free. It does
# NOT relate 'Ş' to 'S', so the s-forms are spelled out here: Turkish is
# routinely typed without diacritics ("LIMITED SIRKETI"), and matching only the
# cedilla ships the other spelling.
_S_FORMS = "[SsŞş]"
_SIRKETI = _S_FORMS + _i_forms("İRKETİ")
_STI = _S_FORMS + _i_forms("Tİ")

# The uppercase members of the i-family, for a suffix whose INITIAL is an i
# ("Inc." in English, "İnc" if it is ever typed with the Turkish dotted capital).
_UPPER_I_FORMS = "[IİÎ]"

# ONE criterion decides how wide each alternative below is folded, and it is
# applied uniformly:
#
#   a suffix whose all-lowercase spelling OCCURS AS A TOKEN IN ORDINARY PROSE
#   keeps an uppercase initial; a suffix that collides with nothing folds
#   completely.
#
# "occurs as a token", not "is a word", and the difference is load-bearing.
# Neither "ltd" nor "plc" is a dictionary word, so the word phrasing prescribes
# folding both -- and measured, folding them is wrong: "The device uses a plc
# module" and "Belge ltd olarak kaydedildi" become company findings. ("plc" is
# a programmable logic controller, ordinary vocabulary in the technical annexes
# attached to commercial filings.) Both are pinned in PROSE_PROBES, because
# review measured that folding either initial otherwise survives the whole
# suite.
#
# This is not fussiness. Fully folded, "limited" matches ordinary English legal
# prose -- "Damages are limited by the terms herein", "Liability is limited to
# the fees paid" -- and this engine runs on English documents by design. Each
# such match is a HIGH finding in a category that IS a direct identifier, so it
# asserts an identifier is present where there is none and offers "Damages are
# limited" to the exporter as a redaction target. It fails safe rather than
# leaking, but a review queue full of spurious HIGH direct-identifier findings
# is exactly the fatigue the false-positive work exists to prevent. Measured:
# eleven English/Turkish prose probes went from 0 matches to 11.
#
# The same criterion, same measurement, catches the generic Turkish
# company-FORM phrases: fully folded, "Taraflar bir anonim şirketi kurmuştur"
# ("the parties founded a joint-stock company") is flagged as a named company.
# An uppercase initial on the two-word forms removes those too.
#
# What this gives up is the all-lowercase spelling of the six word-shaped
# suffixes: "Yıldız limited", "Yildiz anonim sirketi", "Acme plc". Those are
# precisely the spellings that collide with prose, so the two cannot both be
# had. The compound and dotted forms below -- "ltd. şti.", "llc", "a.ş." --
# collide with no word in either language and therefore stay fully folded, so
# the sloppily-typed company spellings that DO occur are still caught.
#
# Order is load-bearing. The body of the rule is lazy, so at a given position
# the FIRST alternative that matches wins and the rest of the suffix is left
# outside the finding. Two-word forms must therefore precede the bare ones, or
# "LOJİSTİK LİMİTED ŞİRKETİ" ends its span at "LİMİTED" and the reviewer
# approves a span that stops short of the identifier.
_COMPANY_SUFFIXES = (
    "A" + _i_forms("NONİM") + " " + _SIRKETI,
    "L" + _i_forms("İMİTED") + " " + _SIRKETI,
    # Fully folded: neither "ltd" nor "şti"/"sti" is a word.
    _i_forms("LTD") + r"\.?\s*" + _STI + r"\.?",
    "L" + _i_forms("İMİTED"),
    "L" + _i_forms("TD") + r"\.?",
    "P" + _i_forms("LC"),
    _UPPER_I_FORMS + _i_forms("NC") + r"\.?",
    # "A.Ş." folds for case ("a.ş.") but keeps the cedilla REQUIRED. The
    # diacritic-free twin "A.S." is left out on measured evidence, not on
    # taste: Turkish courts anonymise parties to their initials, so "A.S." is
    # far more often a redacted person than a company. Adding it flagged
    # "Müşteki A.S. beyanında bulunmuştur" and "tanık A.S." as company names.
    _i_forms("A") + r"\.[Şş]\.",
    # Bare "AŞ" stays UPPERCASE-ONLY, the same criterion again and also
    # measured: folded to [Aa][Şş] it matches the ordinary Turkish word "aş",
    # and flagged "Belediye aş", "Kurum aş" and "sıcak aş" as companies. The
    # dotted form above covers the running-text spelling.
    r"AŞ",
    # Fully folded: "llc" is not a word. Nor is "llp", the form an English law
    # firm takes ("Penhallow & Grieve LLP") -- a firm name is its partners'
    # surnames.
    _i_forms("LLC"),
    _i_forms("LLP"),
)

# Named, rather than spelled inline in DETECTION_RULES, because _court_start()
# reuses this vocabulary: the court place prefix must not swallow a party-role
# word sitting in front of the venue ("Davacı Ankara 5. Asliye Hukuk
# Mahkemesi"). Referencing the rule keeps ONE list of role words; a second copy
# would drift.
PARTY_ROLE_RULE = DetectionRule("party_role", r"\b(?:davacı|davaci|davalı|davali|müşteki|musteki|şikayetçi|sikayetci|şüpheli|supheli|sanık|sanik|katılan|katilan|borçlu|borclu|alacaklı|alacakli|mağdur|magdur|tanık|tanik|müdahil|mudahil)\b", "MEDIUM", "Review party-role context for re-identification risk", "PARTY_ROLE")


# A law firm's name: "Kaya ve Ortakları Hukuk Bürosu", "Kaya, Demir ve Ortakları
# Hukuk Bürosu", "Kaya - Demir Hukuk Bürosu", "Demir Avukatlık Ortaklığı",
# "YILMAZ HUKUK BÜROSU", "Kaya Law Firm". It carries the lawyer's surname, so it
# re-identifies the counsel a [PERSON_n] placeholder stands for. Before
# legal_semantics it was covered by accident: "avukat" was a privilege trigger
# whose 160-character sweep ran over the firm name after a lawyer's, and with
# that trigger now needing corroboration the firm name shipped in cleartext
# beside the pseudonymised lawyer. It is a company_name finding -- a direct
# identifier, counted by the release gate -- typed [LAW_FIRM_n] so counsel stays
# distinguishable from a contracting party.
#
# Same construction as the company rule: no IGNORECASE, so the name words must
# start with a capital, and the firm-form words carry their own case classes
# (with the diacritic-free spellings). One to five name words, joined by
# spaces, "ve", "&", "+", a comma or a hyphen. No trailing boundary, as for the
# directorates: the span ends at the stem, so "Bürosu'nun" and "Bürosunun" keep
# their suffix outside it and the pseudonym does not change with the case of the
# sentence. Like the company rule it cannot tell a sentence-initial capitalised
# word from a surname ("Sözleşme Kaya Hukuk Bürosu" is one finding): that
# over-redacts one word and never misses the firm.
_U_FORMS = "[ÜüUu]"
_BUROSU = "[Bb]" + _U_FORMS + "[Rr][Oo][Ss][Uu]"
_LAW_FIRM_FORMS = (
    _i_forms("Hukuk") + r"[ \t]+(?:" + _BUROSU + "|" + _i_forms("Ofisi") + "|" + _i_forms("Grubu")
    + "|" + "(?:" + _i_forms("ve") + r"[ \t]+)?" + "[Dd][Aa][Nn][Iıİi][Şşs][Mm][Aa][Nn][Ll][Iıİi][Kk]" + ")",
    _i_forms("Avukatlık") + r"[ \t]+(?:" + _i_forms("Ortaklı") + "[ĞğGg]" + _i_forms("ı") + "|" + _BUROSU + ")",
    "[Ll][Aa][Ww]" + r"[ \t]+" + "(?:[Ff][Iİi][Rr][Mm]|[Oo][Ff][Ff][Iİi][Cc][Ee])",
)
_LAW_FIRM_NAME_JOIN = r"(?:[ \t]*[,+&\-–][ \t]*|[ \t]+(?:ve[ \t]+|and[ \t]+)?)"
LAW_FIRM_RULE = DetectionRule(
    "company_name",
    r"\b[A-ZÇĞİÖŞÜ][\w'’.]*(?:" + _LAW_FIRM_NAME_JOIN + r"[A-ZÇĞİÖŞÜ][\w'’.]*){0,4}?[ \t]+(?:"
    + "|".join(_LAW_FIRM_FORMS)
    + ")",
    "HIGH",
    "Replace with consistent pseudonym",
    "LAW_FIRM",
    flags=re.UNICODE,
)


# flags=re.UNICODE, i.e. no IGNORECASE, on purpose -- see the _COMPANY_SUFFIXES
# block above before changing it. The match is NOT the finding's span: the body
# is lazy, so the leftmost match starts at the EARLIEST capital within ninety
# characters of the suffix, and "Sözleşme gereği Davalı taraf olan yıldız
# holding a.ş." matched as one company. _company_start narrows the start to the
# name; the suffix group is where that walk begins.
COMPANY_RULE = DetectionRule(
    "company_name",
    r"\b[A-ZÇĞİÖŞÜ][A-Za-zÇĞİÖŞÜçğıöşü0-9&.,'\- ]{2,90}?\s(?P<company_suffix>" + "|".join(_COMPANY_SUFFIXES) + r")(?!\w)",
    "HIGH",
    "Replace with consistent pseudonym",
    "COMPANY",
    flags=re.UNICODE,
)

# Joining words that occur INSIDE company names ("Gıda Sanayi ve Ticaret",
# "Thornbury & Vale", "Bank of ..."). Crossed on the walk back, but only kept
# once a name word is found to their left, so "Davalı ve Mavi Ltd." does not
# keep the "ve".
_COMPANY_CONNECTORS = frozenset({"ve", "and", "of", "the", "&", "+", "-", "–"})

# Clause-level words that open or close the clause around a company and never
# occur inside its name: the evidence _company_start stops on ("taraf olan
# yıldızlı örnek holding a.ş.", "sözleşmeye göre Mavi Deniz ..."). Deliberately not
# the language gate's marker lists: those carry ordinary prepositions and
# articles ("for", "bir") that do occur inside names ("Centre for Effective
# Dispute Resolution Limited", "bir numara gıda a.ş."), and they are a measured
# calibration a company span must not move with. Matched in lowercase, and in
# title case only in front of an all-caps name: "Veri" or "Konu" can open a
# company name.
_COMPANY_STOP_WORDS = frozenset({
    "göre", "gore", "olan", "arasında", "arasinda", "gereği", "geregi", "nedenle",
    "nedeniyle", "uyarınca", "uyarinca", "ile", "tarafından", "tarafindan",
    "hakkında", "hakkinda", "olarak", "üzere", "uzere", "işbu", "isbu",
    "sözleşme", "sozlesme", "sözleşmenin", "sozlesmenin",
    "between", "with", "to", "for", "pursuant", "according", "against", "whereas", "hereby", "herein",
    "that", "which", "who", "whom", "whose",
})

# Turkish connective adverbs that open a sentence ("Ayrıca Mavi Deniz Lojistik
# Ltd. Şti. ...", "Ancak ..."). Sentence-initial, they are capitalised like the
# name behind them, so they end the name in any case; none names a company.
_COMPANY_SENTENCE_CONNECTIVES = frozenset({
    "ayrıca", "ayrica", "ancak", "fakat", "lakin", "dolayısıyla", "dolayisiyla",
    "oysa", "halbuki", "nitekim", "zira", "çünkü", "cunku", "üstelik", "ustelik",
    "böylece", "boylece",
})

# Words that open a "between A and B" pair: a person directly behind one, joined
# to the company by a connector, is the other party rather than part of the
# company's name ("between Thomas Whitacre and Halvorsen ... Inc." against
# "Selin Akbaş ve Ortakları Ltd. Şti.").
_COMPANY_PAIR_OPENERS = frozenset({"between"})

# Words that name a firm for its founder: behind a connector they keep the
# person inside the company even after "between" ("between John Smith and Sons
# Limited", "Selin Akbaş ve Ortakları Ltd. Şti.").
_COMPANY_FIRM_WORDS = frozenset(tr_fold(word) for word in (
    "Sons", "Partners", "Associates", "Brothers",
    "Ortakları", "Ortaklari", "Şürekası", "Şurekası", "Surekasi", "Kardeşler", "Kardesler", "Kardeşleri", "Kardesleri",
))

# Clause words that also join the words of a name: "act for Quillon Analytics
# LLC" against "Centre for Effective Dispute Resolution Limited", "Back to
# Basics Limited", "Made with Love Ltd", "Kaya Holding ile Mavi Deniz Ltd.
# Şti.". One of them
# ends the name unless the word to its left is capitalised and is neither a
# party-role word nor the end of a person's name ("made by John Smith to Acme
# Holdings Limited", "from the Claimant to Acme Holdings Limited").
_COMPANY_NAME_PREPOSITIONS = frozenset({"for", "with", "to", "against", "ile"})

# A lowercase word this short with a trailing period is an abbreviation inside
# the name ("mavi inş. turizm a.ş."), not the end of a clause.
_COMPANY_ABBREVIATION_LETTERS = 4

_PARTY_ROLE_WORD_RE = re.compile(PARTY_ROLE_RULE.pattern, PARTY_ROLE_RULE.flags)

_ENGLISH_LITIGATION_ROLES = frozenset({
    "claimant", "claimants", "defendant", "defendants", "respondent", "respondents",
    "applicant", "applicants", "appellant", "appellants", "petitioner", "petitioners",
    "plaintiff", "plaintiffs",
})


def _is_party_role_word(token: str) -> bool:
    """A party-role word in either language ("Davalı", "Claimant", "Buyer")."""
    word = re.split(r"['’]", token.strip(",;:"))[0]
    folded = word.lower()
    return bool(
        word
        and (
            _PARTY_ROLE_WORD_RE.fullmatch(word)
            or is_party_role_term(word)
            or folded in _ENGLISH_LITIGATION_ROLES
            or (folded,) in english_names.PARTY_ROLE_TERMS
        )
    )


def _company_token_case(word: str) -> str:
    """'upper' (YILDIZ), 'title' (Yıldız), 'lower' (yıldız) or 'other' (3M, A)."""
    letters = [char for char in word if char.isalpha()]
    if not letters:
        return "other"
    if letters[0].islower():
        return "lower"
    if len(letters) >= 2 and all(char.isupper() for char in letters):
        return "upper"
    if any(char.islower() for char in letters):
        return "title"
    return "other"


def _company_start(match: re.Match, text: str, person_spans: list[tuple[str, int, int]]) -> int:
    """Where the company NAME begins inside a COMPANY_RULE match.

    Walks back from the legal-form suffix one word at a time and stops at the
    first word that is not part of the name:

      * a party-role word ("Alacaklı Yıldız LTD. ŞTİ.") or a sentence-opening
        connective ("Ayrıca Mavi Deniz Lojistik Ltd. Şti."), in any case;
      * a lowercase clause word, or a lowercase word closing a clause
        ("... sözleşmeye göre Mavi Deniz Lojistik Ltd. Şti.", "taraf olan
        yıldızlı örnek holding a.ş.");
      * in front of an all-caps name, a title-case clause word or one closing
        a clause ("Devreden, ORNEK TEKNOLOJİ ANONİM ŞİRKETİ");
      * a connector whose left neighbour ends a person-name span opened by
        "between": "between Thomas Whitacre and Halvorsen ... Inc." -- the
        person has a finding of their own, so the words are still covered.
        Without that opener, or when a firm word follows the connector
        ("between John Smith and Sons Limited"), the person is part of the name
        and stays inside the company finding.

    It only ever NARROWS the match, and when nothing name-like is left in front
    of the suffix ("Davalı Ltd. Şti.") it keeps the match start: a span that
    cannot be narrowed to a name is reported as wide as the rule matched it,
    never dropped. Any other word is kept -- an unknown lowercase word, a
    change of case, a capitalised word in front of a capitalised name
    ("Sözleşme Kaya Ltd. Şti.", as LAW_FIRM_RULE does) -- because without
    positive evidence the walk cannot tell it from the name, and over-redacting
    a word is the side that never ships one.
    """
    body_start = match.start()
    tokens = [
        (body_start + token.start(), body_start + token.end(), token.group())
        for token in re.finditer(r"\S+", text[body_start:match.start("company_suffix")])
    ]
    name_case = None
    name_start = None
    for index in range(len(tokens) - 1, -1, -1):
        token_start, _token_end, token = tokens[index]
        word = re.split(r"['’]", token.strip(",;:"))[0]
        folded = tr_fold(word)
        if word and (_PARTY_ROLE_WORD_RE.fullmatch(word) or folded in _COMPANY_SENTENCE_CONNECTIVES):
            break
        if folded in _COMPANY_CONNECTORS or not any(char.isalnum() for char in word):
            if index and not (
                index + 1 < len(tokens)
                and tr_fold(re.split(r"['’]", tokens[index + 1][2].strip(",;:"))[0]) in _COMPANY_FIRM_WORDS
            ) and any(
                start <= tokens[index - 1][0] and tokens[index - 1][0] < end <= token_start
                and tr_fold("".join(text[max(0, start - 40):start].split()[-1:])) in _COMPANY_PAIR_OPENERS
                for _value, start, end in person_spans
            ):
                break
            continue
        case = _company_token_case(word)
        if name_case is None and case != "other":
            name_case = case
        # Only positive evidence ends the name: a clause word, or punctuation
        # closing a clause. A lowercase word or a change of case on its own is
        # not evidence -- "Kumsalpazarı e-çarşı Elektronik", "Yeşilvadi ÖRNEK
        # BANKASI" -- and stopping on it would ship the words left of it.
        if case == "lower" and (
            folded in _COMPANY_STOP_WORDS
            or token[-1] in ",;:"
            or (token[-1] == "." and sum(char.isalpha() for char in word) > _COMPANY_ABBREVIATION_LETTERS)
        ) and not (
            folded in _COMPANY_NAME_PREPOSITIONS
            and index
            and _company_token_case(tokens[index - 1][2]) in ("title", "upper")
            and not _is_party_role_word(tokens[index - 1][2])
            and not any(start < tokens[index - 1][1] and tokens[index - 1][1] == end for _value, start, end in person_spans)
        ):
            break
        if case == "title" and name_case == "upper" and (folded in _COMPANY_STOP_WORDS or token[-1] in ",;:"):
            break
        name_start = token_start
    return body_start if name_start is None else name_start

# Separator WITHIN a captured name: spaces and tabs only, never a line break --
# the same rule, for the same reason, as turkish_names._SEP. A person name does
# not wrap across lines, and "\s+" here let the capture swallow the next line's
# words: "Av. Şeyma Karaduman\nBaro Sicil No: ..." was captured whole, as
# 'Şeyma Karaduman\nBaro Sicil'. That sample is then unusable twice over. It
# hashes to a different pseudonym key than the clean 'Şeyma Karaduman' the
# gazetteer path finds at the same offset, so one lawyer became [PERSON_1] and
# [PERSON_2] in one document; and it can never be redacted from the exported
# DOCX, because a DOCX part's text is the concatenation of its w:t runs and
# contains no newline at the paragraph boundary. The gap BEFORE the capture
# keeps "\s+": a label may legitimately sit on its own line ("VEKİLİ:\nAv. X"),
# and that gap is not part of the span.
_NAME_SEP = r"[ \t]+"

# "Noteri" is a title the same way "Av." is: a notarial endorsement is signed
# "Masalkent 7. Noteri Nehir Ayyıldız", and the notary's name sat beside the
# venue in cleartext. The capture needs two capitalised words after it, so
# "Sayın Noter;" and a lowercase "noteri tarafından" stay out.
PERSON_CONTEXT_RE = re.compile(
    r"\b(?:Av\s?\.|Av\b|Avukat|Dr\s?\.|Dr\b|Sayın|Sn\.?|Mr\.?|Ms\.?|Noteri)\s+"
    r"([A-ZÇĞİÖŞÜ][A-Za-zÇĞİÖŞÜçğıöşü'\-]+(?:" + _NAME_SEP + r"[A-ZÇĞİÖŞÜ][A-Za-zÇĞİÖŞÜçğıöşü'\-]+){1,3})"
)

# The (?!Vekil) in front of the capture: "Alacaklı Vekili Av. Ekin Düşsel" --
# the standard way an enforcement file or a petition names counsel -- captured
# "Vekili Av" as a person, because the capitalised "Vekili" satisfies the name
# shape and "Av" is the next word. "Vekili" and "Vekilleri" are never a name;
# counsel's own name is still found behind the "Av." title.
ROLE_PERSON_RE = re.compile(
    r"\b(?i:davacı|davaci|davalı|davali|müşteki|musteki|şüpheli|supheli|sanık|sanik|müvekkil|muvekkil"
    r"|borçlu|borclu|alacaklı|alacakli|kiracı|kiraci|kiraya\s+veren|tanık|tanik|mağdur|magdur"
    r"|katılan|katilan|müdahil|mudahil|[İi]şçi|isci|[İi]şveren|isveren|hasta|mirasçı|mirasci|vasi)"
    r"\s+(?:[a-zçğıöşü]+\s+)?"
    r"(?!Vekil(?:i|leri)\b)([A-ZÇĞİÖŞÜ][a-zçğıöşü'\-]+(?:" + _NAME_SEP + r"[A-ZÇĞİÖŞÜ][a-zA-ZçğıöşüÇĞİÖŞÜ'\-]+){1,2})"
)


# A captured person span that is really a street line ("Bağdat Caddesi No: 41")
# or a company ("Alacaklı Verdi Faktoring A.Ş."). The role and title regexes
# capture the words after their trigger without looking at what follows, so both
# guards apply to every span.
_PERSON_ADDRESS_WORD_RE = re.compile(
    r"\b(?:cad\.?|caddesi|sok\.?|sokak|mah\.?|mahallesi|bulvarı|bulvari|no|daire|kat)\b",
    re.IGNORECASE,
)


_NOTARY_ENDORSEMENT_WORDS = frozenset(
    tr_fold(word) for word in "Tarafından Tarafindan Onaylanmıştır Onaylanmistir Onaylı Onayli Onaylanan".split()
)


def _court_claimed_spans(text: str, person_spans: list[tuple[str, int, int]]) -> list[tuple[int, int]]:
    """Spans the court place prefix must not reach back over.

    A person name and a party-role word are the two things that sit immediately
    in front of a venue in Turkish filings ("Av. Şeyma Karaduman 3. Asliye
    Hukuk Mahkemesine", "Davacı Ankara 5. Asliye Hukuk Mahkemesinde"), and both
    already have their own, better finding. Letting the place prefix absorb one
    costs more than the over-redaction: build_redacted_preview resolves an
    overlap by taking the longest span at an offset, so the absorbed name loses
    its PERSON placeholder and comes out as [COURT_AUTHORITY_n]. The same lawyer
    named again elsewhere still gets [PERSON_n], so one person becomes two
    pseudonyms in one document -- the identical failure the _NAME_SEP note
    describes, arrived at from the other side.

    The role words are read off PARTY_ROLE_RULE rather than copied, so there
    stays exactly one list of them.
    """
    spans = [(start, end) for _value, start, end in person_spans]
    spans.extend(match.span() for match in re.finditer(PARTY_ROLE_RULE.pattern, text, PARTY_ROLE_RULE.flags))
    return spans


# Words that may close a person name without being its surname: a Turkish
# honorific ("Ayşe Kaya Hanım") or an English generational suffix ("Robert
# Thistlewood Jr."), compared folded and without a trailing "." or ",".
_NAME_SUFFIXES = frozenset({"bey", "hanım", "hanim", "efendi", "jr", "sr", "ii", "iii"})


def _name_words(name: str) -> list[str]:
    """A person name's words without the honorifics and suffixes that close it."""
    words = name.split()
    while words and tr_fold(words[-1].rstrip(".,")) in _NAME_SUFFIXES:
        words.pop()
    return words


def _surname_of(name: str) -> str | None:
    """The folded last word of a multi-word person name, without a case suffix.

    "Mehmet Kartal'ın" -> "kartal", "Ayşe Kaya Hanım" -> "kaya". A one-word
    span has no separable surname, so it establishes nothing -- nor does
    "Ali Bey", whose last word is an honorific. Known limit: Roman
    generational suffixes are not stripped -- tr_fold maps ASCII "I" to
    dotless "ı", so "II"/"III" never match _NAME_SUFFIXES, and for "John Smith
    III" the bare "Smith" is not propagated.
    """
    words = _name_words(name)
    if len(words) < 2:
        return None
    return tr_fold(re.split(r"['’]", words[-1])[0]) or None


def _venue_surname_mentions(
    text: str, person_spans: list[tuple[str, int, int]], venues: list[re.Match]
) -> list[tuple[str, int, int, tuple[int, str, int]]]:
    """Bare surnames the court place prefix would otherwise weld onto a venue.

    The Ministry directory makes many judicial locations ordinary surnames too
    (Çınar, Kartal, Kiraz, Ceyhan, Sakarya, ...), so in "Davacı Mehmet Kartal
    ... Kartal Ankara 5. Asliye Hukuk Mahkemesine başvurdu" the second "Kartal"
    passed the place list and came out inside [COURT_1] while the first was
    [PERSON_1]: one person, two pseudonyms. A bare surname is not detected as a
    person, so _court_claimed_spans had nothing to protect.

    A leading place word is a person mention here when its folded form is the
    surname of a multi-word name `_person_spans` found in the same document.
    Each one is returned as (word, start, end, bearer), `bearer` being the full
    name's (start, name, end): the caller claims the span so _court_start trims
    the venue after it, AND emits it as a natural_person_name finding keyed on
    the name and anchored on the bearer's span, so the person keeps one
    placeholder. Both, always: releasing the word without the finding would hand
    it to nobody, the leak shape _named_court_spans warns about.

    Only the LEFTMOST run of kept place words is offered, stopping at the first
    word that is not an established surname. A claim makes _court_start drop
    everything before its end, so releasing a surname that stands to the right
    of a genuine place ("Ankara Kartal 5. ...") would take the place out of the
    venue and leave it in no finding at all. "Kartal Ankara 5. ..." gives back
    "Kartal" and keeps "Ankara".

    The accepted cost points the other way: when a party's surname IS the
    venue's district ("Ali Ceyhan ... Ceyhan 1. Asliye Hukuk Mahkemesi"), the
    district reads as [PERSON_n] in front of an unplaced court. The word is
    still removed from the deliverable; no reading of the text alone can tell
    the two apart, and a person split across two pseudonyms misleads the reader
    following that person through the document.

    Where two people share the surname, the mention goes to the nearest one
    named before it, else the nearest after it.

    A name of three or more words does not establish its surname where that
    last word is itself in a venue's place prefix. The bare-name detector reads
    a city straight into a name ("Davacı Ali Veli Ankara 5. Asliye ..." is the
    person "Ali Veli Ankara"); counting that as a surname would make every
    other "Ankara 5. Asliye Hukuk Mahkemesi" in the document a mention of Ali
    Veli, and take the city out of genuine venues. A two-word name is exempt
    unless its last word is one of the 81 provinces: without its last word it
    is no full name at all, so "Av. Selim Çınar 3. Asliye ..." establishes
    Çınar, while "Davacı Ali Ankara 5. ..." does not establish Ankara -- a
    mention is matched as a capitalised whole word, so that would rewrite
    every "Ankara" in the export, "Çankaya/Ankara" and "Ankara Barosu"
    included, as the party. What that leaves: a three-word name whose only
    full mention stands before a venue establishes nothing, and a one-word
    party name followed by a district that is not a province ("Davacı Ali
    Kadıköy 2. Asliye ...") can establish that district as a surname. Neither
    ships a word in cleartext.
    """
    named = _established_surnames(text, person_spans, venues)
    if not named:
        return []
    mentions: dict[tuple[int, int], tuple[str, int, int, tuple[int, str, int]]] = {}
    for match in venues:
        prefix_end = match.end("court_place")
        kept_start = _venue_prefix_start(match, text)
        for word in re.finditer(r"\S+", text[kept_start:prefix_end]):
            word_start, word_end = kept_start + word.start(), kept_start + word.end()
            people = named.get(tr_fold(word.group()))
            if not people or any(s < word_end and word_start < e for _v, s, e in person_spans):
                break
            mentions[(word_start, word_end)] = (word.group(), word_start, word_end, _nearest_bearer(people, word_start))
    return sorted(mentions.values(), key=lambda mention: mention[1])


def _established_surnames(
    text: str, person_spans: list[tuple[str, int, int]], venues: list[re.Match]
) -> dict[str, list[tuple[int, str, int]]]:
    """{folded surname: [(start, name, end), ...]} for the names that establish one.

    The one definition of "this document has named a person in full, so a bare
    word equal to that person's surname is a mention of them", shared by the
    venue give-back above and the prose propagation below
    (_propagated_surname_mentions). The exemptions are the ones
    _venue_surname_mentions' docstring sets out: a name of three or more words
    whose last word sits in a venue's place prefix establishes nothing, and a
    two-word name establishes its last word unless that word is a province.
    """
    prefixes = [match.span("court_place") for match in venues]
    named: dict[str, list[tuple[int, str, int]]] = {}
    for value, start, end in person_spans:
        surname = _surname_of(value)
        if not surname:
            continue
        words = _name_words(text[start:end])
        last_word_start = start + text[start:end].rfind(words[-1])
        exempt = len(words) < 3 and not turkish_places.is_province(surname)
        if exempt or not any(s < end and last_word_start < e for s, e in prefixes):
            named.setdefault(surname, []).append((start, value, end))
    return named


def _nearest_bearer(people: list[tuple[int, str, int]], position: int) -> tuple[int, str, int]:
    """The person a bare surname at `position` refers to: the nearest one named
    before it, else the nearest one named after it."""
    before = [person for person in people if person[0] < position]
    return max(before) if before else min(people)


# Every given name either gazetteer knows, in _mention_key's fold.
_GIVEN_NAME_KEYS = frozenset(
    {name.replace("ı", "i") for name in turkish_names.GIVEN_NAMES} | set(english_names.GIVEN_NAMES)
)

# A word as the propagation reads it: a letter, then letters, digits or
# hyphens. The apostrophe ends it, so a Turkish case suffix ("Kartal'ın") and
# an English possessive ("Quillfeather's") leave the bare surname as the
# finding; the lookbehind keeps "Brien" in "O'Brien" from reading as a word.
_MENTION_WORD_RE = re.compile(r"(?<![\w'’\-])[^\W\d_][\w\-]*")
# What may stand between the start of a sentence and a word that still opens
# it: quotes, brackets, list bullets and numbering ("1.", "(a)").
# A period ends a sentence only after a digit, a closing mark or a word of four
# or more letters: "Av. Demir", "Dr. Price" and "i.e. Price" are mid-sentence.
# The numbering is digits, one letter or a roman numeral.
_SENTENCE_OPENING_RE = re.compile(
    r"(?:^|[!?]|(?<![^\W\d_])\.|(?<=[^\W\d_]{4})(?<!Prof)\.)"
    r"[\s\"'“”‘’(\[\-–—•*]*(?:\(?(?:\d{1,3}|[^\W\d_]|[IVXivx]{1,4})[.)][\s]*)?$",
    re.M,
)
# How far before a surname mention _full_name_repeat looks for the bearer's
# given names, and _SENTENCE_OPENING_RE for the end of the previous sentence.
_REPEAT_WINDOW = 200
_NEXT_WORD_RE = re.compile(r"[ \t]+([^\W\d_][\w\-]*)")


def _mention_key(word: str) -> str:
    """Fold for comparing a mention with a surname: Turkish-aware, with the
    i-family collapsed so "QUILLFEATHER" and "Quillfeather" are one word."""
    return tr_fold(word).replace("ı", "i")


def _words_used_as_given_names(text: str, words: list[re.Match]) -> set[str]:
    """Keys of known given names the text writes in front of another name word.

    "Ali Rıza", "Demir Yılmaz": the word stands where a given name stands. The
    following word must be capitalised and neither a guard word nor a place,
    so "Demir Mahallesi" or "Kartal Ankara 5. ..." says nothing about it. A
    trailing honorific counts: "Ali Bey" is Ali used as a given name.
    """
    used: set[str] = set()
    for word in words:
        value = word.group()
        key = _mention_key(value)
        if not value[0].isupper() or key not in _GIVEN_NAME_KEYS:
            continue
        following = _NEXT_WORD_RE.match(text, word.end())
        if not following:
            continue
        name_word = following.group(1)
        if name_word[0].isupper() and not _is_guard_word(name_word) and not turkish_places.is_venue_place_token(name_word):
            used.add(key)
    return used


# A word after a capitalised word that makes that word a place: "Kartal
# ilçesindeki", "Kartal Mahallesi'nde". Stems, matched on the folded word, so
# every case ending is one entry.
_PLACE_NOUN_STEMS = (
    "ilçe", "ilinde", "ilinin", "mahalle", "köy", "cadde", "sokak", "sokağ", "bulvar", "semt",
    "belde", "kasaba", "şehr", "kent", "bölge",
)
# Honorifics that FOLLOW a Turkish name; a capitalised word after a surname
# that is one of these is still the person ("Kartal Bey").
_TRAILING_HONORIFICS = frozenset({"bey", "hanım", "hanim", "efendi"})


def _starts_a_proper_noun_or_place(word: str) -> bool:
    """The word after a surname mention says the mention is something else.

    A guard word (institution, street, role, heading) in either language; a
    place noun in any case ending; or any other capitalised word, because a
    surname followed by a capitalised word is the first word of a multi-word
    proper noun far more often than a person in running text ("Fox News",
    "Cook Islands", "Çelik Sanayi", "Toprak Mahsulleri Ofisi"). Two
    capitalised followers stay person evidence: a trailing honorific ("Kartal
    Bey") and a recognised place ("Karakoyunlu İstanbul'a taşındı").
    """
    folded = tr_fold(word)
    if folded.startswith(_PLACE_NOUN_STEMS):
        return True
    if not word[0].isupper():
        return False
    if folded in _TRAILING_HONORIFICS:
        return False
    if _is_guard_word(word):
        return True
    return not turkish_places.is_venue_place_token(word)


def _is_guard_word(word: str) -> bool:
    """An institution, street, role, document or heading word in either language."""
    return (
        tr_fold(word) in turkish_names.NON_PERSON_TOKENS
        or english_names._fold(word) in english_names.NON_PERSON_TOKENS
    )


def _propagated_surname_mentions(
    text: str, person_spans: list[tuple[str, int, int]], findings: list[dict], venues: list[re.Match]
) -> list[tuple[str, int, int, tuple[int, str, int]]]:
    """Bare surnames, anywhere in the text, of people the document names in full.

    "Marigold Quillfeather" is found by a title, a label or the gazetteer; three
    paragraphs later "Quillfeather confirmed that ..." has none of those, so it
    was in no finding and shipped in the reviewed export beside a pseudonymised
    full name -- which re-identifies the [PERSON_n] it stands next to. Each
    mention returned here becomes a natural_person_name finding under the same
    pseudonym as the full name and carries that name's span as its `anchor`,
    so the finding says which detection it rests on. No list, title or label is
    consulted for the mention itself: the evidence is the full name.

    Which full names establish a surname is _established_surnames, the rule the
    venue give-back already uses; which person a shared surname goes to is
    _nearest_bearer. On top of that, a word is a mention only when it is a
    capitalised whole word and none of these says it is something else:

    * it overlaps a finding already made (another person, a venue and its
      place prefix -- which _venue_surname_mentions owns -- an address, a
      company or a company's defined short name). The context sweeps
      (health, criminal, privilege) do not count: they are clauses, and a
      name inside one is still a name;
    * the surname is a given name the document itself uses as one -- a known
      given name followed by another name word somewhere ("Mehmet Ali" beside
      "Ali Rıza Bey" would otherwise make every "Ali" the same man; "Ayşe
      Demir" with no "Demir <Name>" anywhere still propagates, though Demir is
      a given name too) -- or one of the 81 provinces ("Selim Ankara" would make every "Ankara" the party -- the
      reason _venue_surname_mentions already refuses a province on a two-word
      name in a venue);
    * a legal form, a "/" (a district written "Kartal/İSTANBUL"), a place noun
      ("Kartal ilçesinde") or a capitalised word follows it ("Kartal
      Mahallesi", "Baker Street", "Fox News") -- see
      _starts_a_proper_noun_or_place for the two capitalised followers that do
      not end it;
    * it opens a sentence and the same word appears in lowercase elsewhere in
      the document, i.e. the document itself uses it as an ordinary word
      ("Demir kapı ..." beside "demir parmaklık"). A lowercase form inside
      another finding (the "stanbridge" of an e-mail address) or in a lowercase
      repeat of the bearer's full name ("ekin kartal") is not that use. A
      sentence-initial surname the document never otherwise writes in lowercase
      ("Quillfeather confirmed ...") is still a mention, and so is every
      mid-sentence one: the bias is to recall, because a missed mention ships a
      real person and an extra one costs the reviewer a rejection.

    What remains, stated so nobody reads silence as absence: a person whose own
    given name no rule found but who shares a detected person's surname
    ("Zerrin Kartal" beside a detected "Mehmet Kartal") has the surname
    redacted under Mehmet Kartal's pseudonym and the given name left in place;
    and a place that is also a party's surname ("Kartal" the district, written
    without "/" or a street word) is redacted as that person. Both remove the
    word from the deliverable; neither can be told apart from the text alone.
    In the other direction, a party's surname that is also a capitalised
    defined term is proposed as that person wherever it stands mid-sentence:
    "The Seller, Robert Price, warrants that the Price shall be paid" gives
    "the Price" Robert Price's pseudonym, and the reviewer rejects it.
    Approving a propagated bare-surname finding also redacts that word in place
    or street names such as "Baker Street" or "Kartal ilçesinde", which the
    reviewer sees on the canvas and can keep. Roman generational suffixes are not stripped: tr_fold maps ASCII "I" to
    dotless "ı", so "II"/"III" never match _NAME_SUFFIXES, and for "John Smith
    III" the bare "Smith" is not propagated.
    """
    words = list(_MENTION_WORD_RE.finditer(text))
    used_as_given = _words_used_as_given_names(text, words)
    named: dict[str, list[tuple[int, str, int]]] = {}
    # Known duplicate: analyze_privacy computes the same _established_surnames
    # result before calling this function; the output is identical.
    for surname, people in _established_surnames(text, person_spans, venues).items():
        key = surname.replace("ı", "i")
        if len(key) < 2 or key in used_as_given or _is_guard_word(surname) or turkish_places.is_province(surname):
            continue
        named.setdefault(key, []).extend(people)
    if not named:
        return []
    bearers = {key: _bearer_index(people) for key, people in named.items()}
    newlines = [match.start() for match in re.finditer("\n", text)]

    def line_start_of(position: int) -> int:
        index = bisect.bisect_left(newlines, position)
        return newlines[index - 1] + 1 if index else 0

    covered = _covered_counts(len(text), findings)

    def overlaps_a_finding(start: int, end: int) -> bool:
        return covered[end] > covered[start]

    ordinary: set[str] = set()
    for word in words:
        start, end = word.span()
        if not word.group()[0].islower() or overlaps_a_finding(start, end):
            continue
        key = _mention_key(word.group())
        if key in bearers and _full_name_repeat(text, line_start_of(start), start, bearers[key][2]):
            continue
        ordinary.add(key)
    mentions: list[tuple[str, int, int, tuple[int, str, int]]] = []
    for word in words:
        value = word.group()
        if not value[0].isupper():
            continue
        key = _mention_key(value)
        if key not in bearers:
            continue
        starts, people, groups = bearers[key]
        start, end = word.span()
        if overlaps_a_finding(start, end):
            continue
        following = _NEXT_WORD_RE.match(text, end)
        if COMPANY_SUFFIX_RE.match(text, end) or text.startswith("/", end) or (
            following and _starts_a_proper_noun_or_place(following.group(1))
        ):
            continue
        line_start = line_start_of(start)
        repeat = _full_name_repeat(text, line_start, start, groups)
        if repeat:
            repeat_start, bearer = repeat
            if not overlaps_a_finding(repeat_start, end):
                mentions.append((text[repeat_start:end], repeat_start, end, bearer))
                continue
        # Only a BARE surname can be read as a role; a full-name repeat above
        # ("Kemal Alıcı" again in prose) is the person whatever its surname is.
        if _in_party_role_term(text, value, start, following):
            continue
        if key in ordinary and _SENTENCE_OPENING_RE.search(text, max(line_start, start - _REPEAT_WINDOW), start):
            continue
        index = bisect.bisect_left(starts, start)
        mentions.append((value, start, end, people[index - 1] if index else people[0]))
    return mentions


_PREVIOUS_WORD_RE = re.compile(r"([^\W\d_][\w\-]*)[ \t]+$")


def _in_party_role_term(text: str, value: str, start: int, following: re.Match | None) -> bool:
    """The mention is (part of) a contract party role, not the person.

    A party whose surname is "Alan", "Veren" or "Alıcı" shares it with the
    roles a contract names its parties by ("Hizmet Alan: Murat Alan"). The
    role is the legal relationship and stays readable (turkish_names'
    contract-party guard), so a word that is a role on its own, or with the
    word right before or after it, is not propagated to the person.
    """
    if turkish_names.is_party_role_term(value):
        return True
    previous = _PREVIOUS_WORD_RE.search(text, max(0, start - 40), start)
    if previous and turkish_names.is_party_role_term(f"{previous.group(1)} {value}"):
        return True
    return bool(following and turkish_names.is_party_role_term(f"{value} {following.group(1)}"))


_Bearers = tuple[list[int], list[tuple[int, str, int]]]


def _bearer_index(
    people: list[tuple[int, str, int]],
) -> tuple[list[int], list[tuple[int, str, int]], dict[tuple[str, ...], _Bearers]]:
    """One surname's bearers sorted by position, and grouped by given names.

    Sorted, the bearer _nearest_bearer would pick is a bisection away, and a
    full-name repeat is tested once per distinct set of given names rather
    than once per occurrence of the full name.
    """
    ordered = sorted(people)
    groups: dict[tuple[str, ...], _Bearers] = {}
    for person in ordered:
        starts, members = groups.setdefault(_given_name_keys(person[1]), ([], []))
        starts.append(person[0])
        members.append(person)
    return [person[0] for person in ordered], ordered, groups


def _covered_counts(length: int, findings: list[dict]) -> list[int]:
    """counts[i] is how many of text[:i]'s characters lie inside a non-context
    finding, so a span overlaps one exactly when counts[end] > counts[start]."""
    depth = [0] * (length + 1)
    for finding in findings:
        if finding["category"] in CONTEXT_SWEEP_CATEGORIES:
            continue
        start, end = max(0, finding["start"]), min(length, finding["end"])
        if start < end:
            depth[start] += 1
            depth[end] -= 1
    counts = [0] * (length + 1)
    open_findings = 0
    for index in range(length):
        open_findings += depth[index]
        counts[index + 1] = counts[index] + (open_findings > 0)
    return counts


def _full_name_repeat(
    text: str, line_start: int, start: int, groups: dict[tuple[str, ...], _Bearers]
) -> tuple[int, tuple[int, str, int]] | None:
    """Where the words before a surname mention are a bearer's own given names.

    The same full name can be detected in one place (a label, a signature
    block) and missed in another (running prose, with a given name no
    gazetteer lists). Marking only the surname there would ship the given name
    beside the person's pseudonym, so the mention takes the whole repeat:
    (start of the repeat, the bearer it repeats). Words are compared in
    _mention_key's fold, so "MARIGOLD QUILLFEATHER" repeats "Marigold
    Quillfeather". A bearer whose name holds an initial or punctuation is not
    matched here and falls back to the surname alone.
    """
    window_start = max(line_start, start - _REPEAT_WINDOW)
    before = text[window_start:start]
    best: tuple[int, int, int, tuple[int, str, int]] | None = None
    for given, (starts, members) in groups.items():
        match = _given_names_before_re(len(given)).search(before)
        if not match or tuple(_mention_key(word) for word in match.group(1).split()) != given:
            continue
        index = bisect.bisect_left(starts, start)
        for person in members[max(0, index - 1):index + 1]:
            candidate = (abs(person[0] - start), person[0], window_start + match.start(1), person)
            if best is None or candidate[:2] < best[:2]:
                best = candidate
    return (best[2], best[3]) if best else None


@functools.lru_cache(maxsize=4096)
def _given_name_keys(name: str) -> tuple[str, ...]:
    """A name's words before its surname, in _mention_key's fold."""
    return tuple(_mention_key(word) for word in _name_words(name)[:-1])


@functools.lru_cache(maxsize=16)
def _given_names_before_re(count: int) -> re.Pattern:
    """`count` words, each followed by spaces or tabs, at the end of a string."""
    return re.compile(r"((?:[^\W\d_][\w\-]*[ \t]+){%d})$" % count)


def _company_alias_findings(text: str, findings: list[dict]) -> list[dict]:
    """A company's defined short name, under the company's own placeholder.

    'Kestane Yazılım Anonim Şirketi (bundan sonra "Kestane" olarak
    anılacaktır)' pseudonymised the full name everywhere and left every
    "Kestane" in cleartext beside its own [COMPANY_1] -- in the definition
    itself, in "Kestane adına irtibat kişisi", in each later reference.
    legal_semantics.defined_aliases accepts an alias only when every word of
    it comes from the company's own name, so a defined ROLE ("Satıcı",
    "Alıcı") stays readable: it is the legal relationship, not an identity.

    Each mention becomes a company_name finding -- a direct identifier, so it
    counts for the release gate like the full name does -- and inherits the
    full name's placeholder: one company, one pseudonym. A mention inside a
    direct identifier already reported (the full name, an e-mail address) is
    left to that finding.
    """
    companies = {(f["start"], f["end"]): f for f in findings if f["category"] == "company_name"}
    aliases = legal_semantics.defined_aliases(text, list(companies))
    if not aliases:
        return []
    covered = [(f["start"], f["end"]) for f in findings if f["category"] in DIRECT_IDENTIFIER_CATEGORIES]
    alias_findings = []
    for mention in legal_semantics.alias_mentions(text, aliases, covered):
        company = companies[(mention.entity_start, mention.entity_end)]
        alias_findings.append(
            _finding(
                "company_name",
                text[mention.start:mention.end],
                company["risk"],
                company["recommended_action"],
                company["placeholder"],
                mention.start,
                mention.end,
            )
        )
    return alias_findings


def _surname_mention_finding(
    word: str,
    start: int,
    end: int,
    bearer: tuple[int, str, int],
    counters: Counter[str],
    placeholder_state: dict[tuple[str, str], str],
) -> dict:
    """A bare surname's finding: the full name's pseudonym, anchored on its span.

    `anchor` is the offsets of the full-name detection the mention rests on --
    offsets only, never the name's text, which the full name's own finding
    already carries. It is what makes the finding cite its evidence: the
    mention was not matched by any rule on its own.
    """
    bearer_start, name, bearer_end = bearer
    finding = _finding(
        "natural_person_name",
        word,
        "HIGH",
        "Replace with consistent pseudonym",
        _placeholder_for("PERSON", name, counters, placeholder_state),
        start,
        end,
    )
    finding["anchor"] = {"start": bearer_start, "end": bearer_end}
    return finding
