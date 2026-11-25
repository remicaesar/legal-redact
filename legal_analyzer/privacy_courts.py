"""Courts and places: the court and authority rules, Turkish and English,
the place prefix and the span logic that decides which captured words are the
venue's, and the venue and bar-registration pseudonym grouping.

Moved out of ``privacy.py`` unchanged; that module re-exports every name here.
``_named_court_spans`` and ``_venue_matches`` stay in ``privacy.py`` because
they read ``privacy.DETECTION_RULES``.
"""

from __future__ import annotations

import re
from collections import Counter

from legal_analyzer import turkish_places

from .privacy_common import (
    DetectionRule,
    _placeholder_for,
    _pseudonym_key,
)
from .privacy_identifiers import (
    _EN_CAP_WORD,
    _title_and_caps,
)


# The leading place words of a Turkish venue name, prepended to EVERY
# court_or_authority rule that anchors on an institution ("Ankara 5. Asliye
# Hukuk Mahkemesi", "Beyoğlu 24. Noterliği", "İstanbul Cumhuriyet
# Başsavcılığı") and to the bar-registration rule. Those rules used to start at
# the institution word, so the place sat
# one character outside the finding -- and a span outside every finding cannot
# be approved by a reviewer, so it can never be redacted on export. Measured
# before this prefix existed: full-span recall on Turkish courts was 0 of 16,
# and "Ankara", "İzmir", "ÇANKAYA" and "BEŞİKTAŞ" each shipped verbatim in the
# delivered file directly beside a pseudonymised court. Court plus city is a
# strong re-identifier for a case: a reader who knows the matter can often
# recover it from the venue alone.
#
# The regex only CAPTURES the candidate words; which of them belong to the venue
# is decided in _court_start() against legal_analyzer/turkish_places.py, because
# a capitalised-word run alone cannot tell "Ankara" from "Dosya". Two rounds
# measured that from both sides: {0,2} absorbed the word in front of the venue
# in ordinary prose, and {0,1} still absorbed it whenever the venue carried no
# place word of its own. See the turkish_places module docstring.
#
# Three bounds, each load-bearing:
#
#   * (?-i:[A-ZÇĞİÖŞÜ]) -- the INITIAL is matched case-sensitively even though
#     these rules run under IGNORECASE, which would otherwise fold the class and
#     let the prefix absorb the ordinary lowercase words that actually precede a
#     court in running text ("... dosyasında İstanbul Anadolu 4. Asliye ...",
#     "Kadıköy Kaymakamlığı yazısı 3. Sulh Ceza Hakimliği"). The scoped-flag
#     form is the same one _CONTEXT_SWEEP_CHAR uses. The REST of the word keeps
#     both cases spelled out in the class rather than inheriting the flag, so an
#     all-caps heading ("ÇANKAYA 4. İCRA MÜDÜRLÜĞÜ", "İZMİR 2. İŞ MAHKEMESİ")
#     matches. The circumflex vowels are in both halves of the class because
#     the place list uses them -- "Hakkâri", "Milâs", "Kâhta", "İslâhiye" and
#     eight more directory entries. Without them the engine matched "H", "akk",
#     then needed [ \t]+ at "â" and failed, so the prefix collapsed to zero
#     words and the city shipped beside the pseudonymised venue while
#     is_venue_place_token() still returned True -- the list held the place and
#     only the capture half could not reach it. The class must cover the
#     alphabet the list actually uses, which
#     tests/test_court_full_span.py asserts over every entry rather than over a
#     remembered dozen. Turkish upper-casing is exactly where a naive fold breaks, since
#     "İZMİR" lower-cases to "izmir" only under the Turkish-aware tr_fold().
#     Spelling both cases into the class sidesteps the fold entirely here; where
#     a fold IS needed -- comparing a word against a set -- tr_fold() is used and
#     no second fold is written.
#
#   * {0,2} words -- "İstanbul Anadolu" is the longest real form; unbounded, the
#     prefix would run back over a whole capitalised heading. Widening this cap
#     is safe in a way it was not before _court_start() consulted the place
#     list, because a captured word the list does not recognise is given back
#     rather than reported. The quantifier is LAZY: the court rules pin the
#     prefix against a branch number so laziness cannot change what they match,
#     but the bar-registration rule's own leading word is optional, and a greedy
#     prefix ate it -- "İstanbul Barosu Sicil No" captured "İstanbul Barosu",
#     the list rejected "Barosu", and the span restarted at "Sicil".
#
#   * [ \t]+ between the words, never \s+ -- the same invariant, for the same
#     reason, as _NAME_SEP: a finding sample containing a newline matches
#     nothing in a DOCX part (whose text is the concatenation of its w:t runs),
#     so it can never be removed from the deliverable however it was reviewed.
#     A court heading on the line after "T.C." keeps the heading and drops the
#     line above it.
#
# The prefix is a named group because it is TRIMMED after matching -- see
# _court_start(), which is the court twin of _address_start(): it keeps only the
# captured words the place list recognises, and then gives back any of those a
# person or party-role finding already claimed ("Davacı Ankara 5. Asliye Hukuk
# Mahkemesi" -- the role word goes, the place stays).
_COURT_PLACE_PREFIX = r"(?P<court_place>(?:(?-i:[A-ZÇĞİÖŞÜÂÎÛ])[A-Za-zÇĞİÖŞÜçğıöşüÂÎÛâîû]{1,24}[ \t]+){0,2}?)"

# The catch-all court/authority rule. It fires on a bare institution SUFFIX
# ("Mahkemesi", "Hakimliği", "Noterliği", "Savcılığı") and then sweeps up to 120
# characters of the clause that follows, because the institution's own name is
# not part of the trigger. That is deliberate -- it is the only rule that sees
# an unnumbered or unfamiliar authority -- but it means that wherever one of the
# specific court rules below has already matched the full institution name, this
# rule reports a second, worse copy of the same institution starting at the
# suffix: "11. Aile Mahkemesi" reported again as "Mahkemesi Sayın Hakimliğine",
# "7. İş Mahkemesi" again as "Mahkemesi kararının temyiz incelemesinde, davacı
# işçi Hasan Demirci'nin" -- a fragment that carries no institution name and
# stores an unrelated person's name in the finding sample.
#
# _generic_court_matches() therefore skips a match of THIS rule that begins
# inside a span one of the name-anchored rules already matched, and resumes the
# scan at the END of that enclosing span rather than at the end of the skipped
# match. What is guaranteed as a result:
#
#   * a name-anchored court is never suppressed;
#   * the START of every authority mention lies inside at least one finding
#     span -- its own, or the name-anchored finding that encloses it -- so the
#     place name and institution stem can always be approved and redacted.
#     Not the whole mention: the Yargıtay/Danıştay rule's fixed 60-character
#     sweep can end mid-word, and the resume then lands where \b cannot match,
#     so the TAIL of that word ("lığına" of "Başsavcılığına") can fall outside
#     every finding. Measured on 15,000 adversarial sentences: 0 whole misses,
#     664 such tail fragments, none carrying an identifier.
#
# The resume is the load-bearing half. Dropping the match and letting
# re.finditer() continue is NOT equivalent: finditer is non-overlapping, so the
# scan would restart after the discarded 120-character match and any second,
# genuinely distinct authority inside it would never be offered again. That left
# "Kadıköy Cumhuriyet Başsavcılığı'na" with no finding of any kind in
# "İstanbul Anadolu 4. Asliye Ceza Mahkemesi kararı Kadıköy Cumhuriyet
# Başsavcılığı'na gönderilmiştir" -- and a span with no finding cannot be
# approved by a reviewer, so it is unredactable on export by construction.
#
# The institution words end in the third-person possessive ("Mahkeme-si",
# "Noterliğ-i", "Başsavcılığ-ı"), and Turkish attaches the case ending straight
# onto it with no apostrophe: "Mahkemesinin", "Başsavcılığına", "Noterliğinde",
# "Mahkemesince". Every court rule used to end its institution on \b, which
# such a word never offers, so the suffixed form matched nothing: "Beyoğlu 24.
# Noterliğinin" and "İstanbul Cumhuriyet Başsavcılığının" were in no finding,
# and "Ankara 5. Asliye Hukuk Mahkemesinin" backtracked to "Ankara 5. Asliye
# Hukuk" with the court word outside the span. _AUTHORITY_CASE_END accepts the
# case endings that follow a possessive -- genitive -nın/-nin/-nun/-nün,
# accusative -nı, dative -na, locative -nda, ablative -ndan, relative -ndaki,
# equative -nca and instrumental -yla, each with its vowel-harmony and
# diacritic-stripped spellings -- as a LOOKAHEAD, so the span still ends at the
# stem: the suffix stays outside it and the pseudonym key, exactly as
# DIRECTORATE_RULE leaves "Müdürlüğü'nden" and "Dairesine" outside its own.
# The bare \b is still accepted, so the apostrophe forms are unchanged.
#
# Accepting the dative also reaches a petition's salutation, "... Mahkemesi
# Sayın Hakimliğine" ("to the Honourable Judge"), which names no venue -- the
# court is the line before it. GENERIC_COURT_SUFFIX_RULE therefore refuses an
# institution word directly after "Sayın"; a place between the two ("Sayın
# İstanbul Valiliğine") still matches, because the lookbehind then sees the
# place word.
_AUTHORITY_CASE_END = r"(?=(?:n(?:[ıiuü]n|[ıiuü]|[ae]|d[ae](?:n|ki)?|c[ae])|yl[ae])?\b)"

GENERIC_COURT_SUFFIX_RULE = DetectionRule(
    "court_or_authority",
    _COURT_PLACE_PREFIX + r"\b(?<!sayın[ \t])(?<!sayin[ \t])(?P<institution>UYAP|mahkemesi|savcılığı|savciligi|hakimliği|hakimligi|hâkimliği|noterliği|noterligi|valiliği|valiligi|kaymakamlığı|kaymakamligi|[İi]cra müdürlüğü|icra mudurlugu|ticaret sicili? müdürlüğü|ticaret sicili? mudurlugu|bölge adliye|bolge adliye|türk patent|turkpatent|kurumu)" + _AUTHORITY_CASE_END + r"[^.\n]{0,120}",
    "HIGH",
    "Replace with consistent pseudonym or generalize",
    "COURT_AUTHORITY",
)

NUMBERED_COURT_RULE = DetectionRule(
    "court_or_authority",
    _COURT_PLACE_PREFIX + r"\b\d{1,2}\.\s*(?:Asliye|Ağır\s+Ceza|Agir\s+Ceza|Sulh|İdare|Idare|Ticaret|İş|Is|Aile|İcra|Icra|Hukuk|Ceza|Vergi)(?:\s+(?:Hukuk|Ceza|Ticaret|İş|Is|Aile|Mahkemesi|Dairesi|Müdürlüğü|Mudurlugu|Hakimliği|Hakimligi)){1,3}" + _AUTHORITY_CASE_END,
    "HIGH",
    "Replace with consistent pseudonym or generalize",
    "COURT_AUTHORITY",
)

# A public prosecutor's office, by name: "İstanbul Cumhuriyet Başsavcılığı",
# "Kadıköy Cumhuriyet Savcılığı", the OCR "IZMIR CUMHURIYET BASSAVCILIGI".
# These used to be alternatives of GENERIC_COURT_SUFFIX_RULE, and so carried
# its 120-character trailing sweep: "İstanbul Cumhuriyet Başsavcılığı
# 2024/12345 soruşturma numaralı dosya kapsamında ifade verdi" was ONE
# court_or_authority finding to the end of the sentence, so approving the
# office blanked the whole clause. The office's name is complete without the
# sweep -- the place in front of it is the place prefix, and "Cumhuriyet
# Başsavcılığı" is the institution -- so this rule is name-anchored like the
# numbered courts: it ends at the stem, _named_court_spans reports it, and a
# generic "Savcılığı" inside it yields to it. The investigation number after
# it is case_or_investigation_number's, and that rule now reads the forms that
# follow an office ("2024/12345 sayılı soruşturma").
#
# One optional tail is part of the name: an investigation bureau, "İstanbul
# Cumhuriyet Başsavcılığı Dolandırıcılık Suçları Soruşturma Bürosu", "...
# Terör ve Örgütlü Suçlar Soruşturma Bürosu" -- up to four capitalised words,
# optionally joined by "ve", ending in "Bürosu". The sweep used to cover it, and
# the bureau narrows the office to the unit handling the file, so leaving it
# out would ship it beside a pseudonymised office.
PROSECUTOR_OFFICE_RULE = DetectionRule(
    "court_or_authority",
    _COURT_PLACE_PREFIX
    + r"\b(?P<institution>Cumhuriyet[ \t]+(?:Ba[şs])?savc[ıi]l[ıi][ğg][ıi])"
    + r"(?:[ \t]+(?-i:[A-ZÇĞİÖŞÜ])[A-Za-zÇĞİÖŞÜçğıöşü]+(?:[ \t]+(?:ve[ \t]+)?(?-i:[A-ZÇĞİÖŞÜ])[A-Za-zÇĞİÖŞÜçğıöşü]+){0,3}[ \t]+B[üu]ros[uü]" + _AUTHORITY_CASE_END + r")?"
    + _AUTHORITY_CASE_END,
    "HIGH",
    "Replace with consistent pseudonym or generalize",
    "COURT_AUTHORITY",
)

# A high court's numbered chamber is part of the court's name: "Yargıtay 3.
# Hukuk Dairesi", "Danıştay 10. Daire", "İstanbul Bölge Adliye Mahkemesi 8.
# Hukuk Dairesi", "Yargıtay 12. CD". The trailing sweep below stops at the first
# full stop, and the chamber's ordinal is one, so without this branch the court
# was reported as "Yargıtay 3" and its chamber again as "3. Hukuk Dairesi" by
# NUMBERED_COURT_RULE: two overlapping findings and two placeholders for one
# court ("[COURT_2][COURT_1]'nin"), and a chamber keyed apart from its court, so
# the 3rd civil chamber of two different courts shared one pseudonym. "Danıştay
# 10. Daire" left ". Daire" in no finding at all.
#
# Tried before the sweep, so a chambered court ends at its chamber word and is
# keyed by _venue_identity on the same string at every mention; an unnumbered
# mention ("Yargıtay Hukuk Genel Kurulu'nun ...") still takes the sweep exactly
# as before. analyze_privacy drops the NUMBERED_COURT_RULE match inside the
# chamber, since every character of it is already in this finding.
_HIGH_COURT_CHAMBER = (
    r"(?P<chamber>[ \t]+\d{1,2}\.[ \t]*"
    r"(?:(?:(?:Hukuk|Ceza|İdari|Idari|İdare|Idare|Vergi)[ \t]+(?:Dava[ \t]+)?)?Daire(?:si)?(?=[^\W\d_]*\b)|(?-i:HD|CD)\b))"
)
HIGH_COURT_RULE = DetectionRule(
    "court_or_authority",
    _COURT_PLACE_PREFIX
    + r"\b(?P<institution>Yargıtay|Yargitay|Danıştay|Danistay|Anayasa\s+Mahkemesi|Bölge\s+Adliye\s+Mahkemesi|Bolge\s+Adliye\s+Mahkemesi|Bölge\s+İdare\s+Mahkemesi|Bolge\s+Idare\s+Mahkemesi)" + _AUTHORITY_CASE_END + r"(?:"
    + _HIGH_COURT_CHAMBER
    + r"|[^.\n]{0,60})",
    "HIGH",
    "Replace with consistent pseudonym or generalize",
    "COURT_AUTHORITY",
)

# The state directorates a case file cites by district -- land registry,
# population registry, tax office, police, social security, education:
# "Şişli Tapu Müdürlüğü", "Kadıköy Nüfus Müdürlüğü", "Beşiktaş Vergi Dairesi
# Müdürlüğü", "Kadıköy İlçe Emniyet Müdürlüğü", "Bakırköy SGK İl Müdürlüğü".
# Before this rule each of those shipped with district AND authority in
# cleartext: no court rule named them, and GENERIC_COURT_SUFFIX_RULE carries no
# bare "müdürlüğü" trigger. Authority plus district is the same re-identifier
# as court plus city.
#
# An EXPLICIT list of directorate names, deliberately not a bare "müdürlüğü"
# trigger, for two measured reasons:
#
#   * A bare trigger catches every company department. "İNSAN KAYNAKLARI
#     MÜDÜRLÜĞÜ" heads both termination letters in eval/corpus, and "şirket
#     genel müdürlüğüne" is in tests/fixtures/corporate_resolution.txt; each
#     would become a HIGH court_or_authority finding, and as a
#     GENERIC_COURT_SUFFIX_RULE alternative it would sweep 120 characters of
#     the letter after it.
#   * Gating a bare trigger on the place list does not rescue it: it would not
#     reach a single target. The directorate's own subject word sits between
#     the district and "Müdürlüğü" ("Şişli Tapu Müdürlüğü"), the list rejects
#     "Tapu", and _venue_prefix_start's right-to-left walk stops there -- so the
#     district is never kept. Making the subject word part of the INSTITUTION is
#     what lets the shared place prefix sit directly in front of it, exactly as
#     it sits in front of a court's branch number.
#
# So the institution is the directorate's full name and the rule is
# name-anchored like the numbered courts: no trailing sweep, _named_court_spans
# reports it, and _court_start / _venue_identity / _assign_grouped_placeholders
# handle its place word exactly as they do a court's -- "Şişli Tapu Müdürlüğü"
# and a bare "Tapu Müdürlüğü" later in the file are one pseudonym, "Şişli" and
# "Kadıköy" Tapu Müdürlüğü are two. An unlisted capitalised word in front
# ("Alsancak", "Dosya") is given back by the list gate, not absorbed.
#
# Three further bounds:
#
#   * The first institution word must be capitalised -- the (?-i:...) lookahead,
#     the same scoped-flag form _COURT_PLACE_PREFIX uses. A lowercase "ilgili
#     vergi dairesine" or "tapu müdürlüğüne başvurulur" is prose about the kind
#     of office, not a named one, and carries nothing to pseudonymise.
#   * No trailing \b: the span ends at the stem, so a case suffix -- "Müdürlüğü'
#     nden", "Müdürlüğüne", "Dairesine" -- is left outside it and the folded
#     core, and so the pseudonym, stays the same for every inflection.
#   * Each Turkish letter is a two-letter class ([üu], [ğg], [şs], ...), so the
#     diacritic-stripped OCR spelling ("TAPU MUDURLUGU", "NUFUS MUDURLUGU")
#     matches as every other court rule's ASCII alternate does. IGNORECASE
#     already treats İ/I/ı/i as one letter, so "İL" and "IL" need no class.
#
# And one bound on a single form: a bare "Vergi Dairesi", with no Müdürlüğü or
# Başkanlığı after it, is kept only when the list gate leaves a place word in
# front of it (_is_unplaced_tax_office). In a party block or an invoice the bare
# form is a field label -- "Satıcı ... A.Ş., Vergi Dairesi: Kadıköy, VKN: ..."
# -- or prose about the kind of office ("Vergi Dairesi ve Vergi No bilgileri
# eksiktir"), not a named office, and pseudonymising the label while the
# district after the colon ships only makes the output look handled. "Kadıköy
# Vergi Dairesi" is still one finding covering the district; the longer forms
# name the office themselves and are unchanged. The bare label-value form
# ("Vergi Dairesi: Kadıköy") is not detected by this rule but by
# TAX_OFFICE_FIELD_RULE below. The repeated form "Vergi Dairesi: Kozyatağı
# Vergi Dairesi" is: see _follows_tax_office_label.
#
# The enforcement offices that carry no branch number -- a district's single
# "Masalkent İcra Dairesi", and the offices named for their caseload, "Genel
# İcra Dairesi" and "Banka Alacakları İcra Dairesi" -- are named here for the
# same reason: no rule matched them, so the office and its city shipped beside
# a pseudonymised file number. Like a bare "Vergi Dairesi", a bare "İcra
# Dairesi" with no place word in front is the name of a kind of office, not of
# one, and is dropped (_is_unplaced_tax_office) -- which is also what keeps a
# numbered office ("MASALKENT 4. İCRA DAİRESİ", the numbered court rule's) from
# being reported a second time from its "İCRA DAİRESİ" tail.
_MUDURLUGU = r"M[üu]d[üu]rl[üu][ğg][üu]"
_DIRECTORATE_LEVEL = r"(?:(?:İl|İl[çc]e|B[öo]lge)[ \t]+)?"
_DIRECTORATE_SUBJECT = (
    r"(?:Tapu(?:[ \t]+(?:ve[ \t]+Kadastro|Sicili?))?"
    r"|Kadastro"
    r"|N[üu]fus(?:[ \t]+ve[ \t]+Vatanda[şs]l[ıi]k)?"
    r"|Emniyet"
    r"|Mill[iî][ \t]+E[ğg]itim"
    r"|SGK(?:[ \t]+Sosyal[ \t]+G[üu]venlik)?"
    r"|Sosyal[ \t]+G[üu]venlik)"
)
DIRECTORATE_RULE = DetectionRule(
    "court_or_authority",
    _COURT_PLACE_PREFIX
    + r"\b(?=(?-i:[A-ZÇĞİÖŞÜ]))(?:"
    + _DIRECTORATE_LEVEL + _DIRECTORATE_SUBJECT + r"[ \t]+" + _DIRECTORATE_LEVEL + _MUDURLUGU
    + r"|(?P<tax_office>Vergi[ \t]+Dairesi)(?:[ \t]+(?:" + _MUDURLUGU + r"|Ba[şs]kanl[ıi][ğg][ıi]))?"
    + r"|(?:SGK[ \t]+)?Sosyal[ \t]+G[üu]venlik[ \t]+Merkezi"
    + r"|(?P<enforcement_office>(?:(?:Genel|Banka[ \t]+Alacaklar[ıi])[ \t]+)?[İI]cra[ \t]+Dairesi))",
    "HIGH",
    "Replace with consistent pseudonym or generalize",
    "COURT_AUTHORITY",
)

# The VALUE of a "Vergi Dairesi:" field: "Satıcı ... A.Ş., Vergi Dairesi:
# Kadıköy, VKN: ...". The label says the value is a tax office, so the words
# after the colon are its name whether or not the place list carries them --
# the reasoning _follows_tax_office_label applies to the repeated form, here
# without a second institution word to anchor on. Next to the VKN that name is a
# re-identifier, and before this rule it shipped in cleartext beside a
# pseudonymised tax number.
#
# The finding covers the value only. The label is not sensitive, it stays in
# the deliverable ("Vergi Dairesi: [TAX_AUTHORITY_1], VKN: [TAX_NUMBER_1]"),
# and the bare "Vergi Dairesi" DIRECTORATE_RULE matches inside it is still
# dropped by _is_unplaced_tax_office, so a label with no value -- "Vergi
# Dairesi:" at a line end, "Vergi Dairesi ve Vergi No bilgileri eksiktir" --
# is still no finding at all.
#
# The value ends at its own boundary and never swallows the rest of the line:
#
#   * it is a run of one to four CAPITALISED words on the label's line (a
#     lowercase "ve" may join two), so a comma, a semicolon, a period, a
#     lowercase word or the line end stops it, and a sample never carries a
#     newline;
#   * a word that opens the next field stops it -- one followed directly by a
#     colon ("Tel:"), one of the identifier labels a party block carries
#     after the tax office ("VKN", "Vergi No", "Mersis", "Ticaret Sicil", ...),
#     or the first word of a label that ends in a label word ("Fatura No:",
#     "Fatura Tarihi:"), so "Vergi Dairesi: Kadıköy VKN: ..." and "Vergi
#     Dairesi: Kadıköy Fatura No: ..." without a comma still end at "Kadıköy";
#   * the run is taken possessively: a run longer than four words yields its
#     first four, which always carry the district, rather than no finding --
#     refusing it would ship the district in cleartext -- and a shorter prefix
#     is never tried, so a value that goes on to repeat the institution is
#     never reported in part;
#   * a value that repeats the institution ("Kozyatağı Vergi Dairesi") is left
#     to DIRECTORATE_RULE, which already reports the whole office through
#     _follows_tax_office_label -- two findings there would issue it two
#     pseudonyms.
#
# Its pseudonym is grouped with DIRECTORATE_RULE's under the same (core, place)
# key -- core "Vergi Dairesi", place the value -- so "Vergi Dairesi: Kadıköy"
# in a party block and "Kadıköy Vergi Dairesi" in the body are one office
# (_tax_office_field_identity).
_TAX_OFFICE_FIELD_STOP = r"(?:VKN|TCKN|MERS[İI]S|IBAN|Vergi|No|Tel|Telefon|GSM|Faks|Fax|Adres|Adresi|E-?posta|Sicil|Ticaret[ \t]+Sicil)\b"
_TAX_OFFICE_FIELD_WORD = (
    r"(?!" + _TAX_OFFICE_FIELD_STOP + r")"
    r"(?-i:[A-ZÇĞİÖŞÜÂÎÛ])[A-Za-zÇĞİÖŞÜçğıöşüÂÎÛâîû]+\b(?![ \t]*:)"
)
_TAX_OFFICE_FIELD_NEXT_WORD = (
    r"(?:[ \t]+(?-i:ve))?[ \t]+" + _TAX_OFFICE_FIELD_WORD
    + r"(?![ \t]+(?:No|Numaras[ıi]|Tarihi|Kodu)\b)"
)
TAX_OFFICE_FIELD_RULE = DetectionRule(
    "court_or_authority",
    r"\b(?P<field_label>Vergi[ \t]+Dairesi)[ \t]*:[ \t]*"
    + r"(?P<tax_office_value>" + _TAX_OFFICE_FIELD_WORD + r"(?:" + _TAX_OFFICE_FIELD_NEXT_WORD + r"){0,3}+)"
    + r"(?![ \t]+Vergi[ \t]+Dairesi)",
    "HIGH",
    "Replace with consistent pseudonym or generalize",
    "COURT_AUTHORITY",
)

# A court or tribunal named in English: "High Court of Justice", "Central
# London County Court", "County Court at Manchester", "Employment Tribunal",
# "Court of Appeal (Civil Division)", "King's Bench Division", "United States
# District Court for the Southern District of New York". Three shapes:
#
#   * capitalised qualifiers in front of Court/Tribunal, with an optional
#     "of/at/for <Name>" after it and a parenthesised chamber;
#   * Court/Tribunal with that continuation and no qualifier ("Court of
#     Appeal", "Court of Session");
#   * the divisions and court groupings that do not end in the word Court.
#
# A bare "the Court" -- the defined term every judgment and contract uses --
# names no venue and is left alone, as is the plural "courts of England and
# Wales" in a jurisdiction clause: that clause is legal substance, and the
# Turkish rules leave its "Mahkemeleri" alone for the same reason. Determiners,
# prepositions and party roles are never qualifiers, so "THE" in "IN THE HIGH
# COURT" and "Claimant" in front of a venue stay outside the span.
#
# The empty court_place group routes the match through _court_start and
# _venue_identity like every other court_or_authority rule: an English venue
# carries its place inside its own name, so there is no prefix to resolve.
_EN_COURT_STOP = (
    r"(?!(?i:the|this|that|these|those|such|said|any|each|every|a|an|in|of|for|and|at|to|by|on|before"
    r"|from|with|under|its|our|your|their|his|her|claimant|defendant|respondent|applicant|appellant"
    r"|petitioner|plaintiff|party|parties|witness|filed|issued|lodged|sealed|order|judgment)(?![\w'’]))"
)
_EN_COURT_NOUN = r"(?:" + _title_and_caps("Court", "Tribunals", "Tribunal") + r")(?![\w'’])"
# An "and" inside the continuation joins the words of one name ("Courts of
# England and Wales"), never a second venue: "Court of Appeal and the Supreme
# Court" is two courts, so "and" may not lead on to another court noun.
_EN_COURT_CONTINUATION = (
    r"(?:[ \t]+(?i:of|at|for|in)[ \t]+(?:(?i:the)[ \t]+)?" + _EN_CAP_WORD
    + r"(?:[ \t]+(?:(?:(?i:of|upon|for)|(?i:and)(?![ \t]+(?:(?i:the)[ \t]+)?(?:" + _EN_CAP_WORD + r"[ \t]+){0,3}"
    + _EN_COURT_NOUN + r"))[ \t]+(?:(?i:the)[ \t]+)?)?" + _EN_CAP_WORD + r"){0,5})"
)
_EN_COURT_CHAMBER = r"(?:[ \t]*\([A-Z][^()\n]{0,60}\))"
_EN_COURT_DIVISIONS = (
    r"(?i:(?:king|queen)['’]s[ \t]+bench[ \t]+division|chancery[ \t]+division|family[ \t]+division"
    r"|business[ \t]+and[ \t]+property[ \t]+courts)"
)
# "Employment Tribunal sitting at Watford", "County Court sitting at Central
# London": the hearing centre is part of the venue's name, and left outside
# the span the town ships beside the placeholder.
_EN_COURT_SITTING = r"(?:[ \t]+(?i:sitting)[ \t]+(?i:at|in)[ \t]+" + _EN_CAP_WORD + r"(?:[ \t]+" + _EN_CAP_WORD + r"){0,3})"
_EN_COURT_QUALIFIERS = r"(?:" + _EN_COURT_STOP + _EN_CAP_WORD + r"[ \t]+(?:(?i:and|of)[ \t]+)?){1,4}"
_EN_COURT_UNIT = (
    r"(?:" + _EN_COURT_QUALIFIERS + _EN_COURT_NOUN
    + _EN_COURT_CONTINUATION + r"?" + _EN_COURT_CHAMBER + r"?" + _EN_COURT_SITTING + r"?"
    r"|" + _EN_COURT_NOUN + _EN_COURT_CONTINUATION + _EN_COURT_CHAMBER + r"?" + _EN_COURT_SITTING + r"?"
    r"|" + _EN_COURT_DIVISIONS + _EN_COURT_CONTINUATION + r"?)"
)
# The High Court names itself parent first, a comma between each level: "High
# Court of Justice, Business and Property Courts of England and Wales,
# Insolvency and Companies List (ChD)". As separate findings the commas stay
# uncovered and one venue gets two or three placeholders, so a PARENT -- the
# High Court or the Business and Property Courts -- carries the levels after it
# into one span: a division, another parent, or a named court, list or
# registry. Only a parent reaches over a comma; "the County Court, Court of
# Appeal and ..." is a list of venues and stays one finding each. Accepted
# cost: a prose list that begins with the High Court and names a "<Word>
# Court" next ("the High Court, Crown Court or …") is read as one venue -- one
# placeholder for two courts, both still covered.
_EN_COURT_PARENT = r"(?=(?i:high[ \t]+court|business[ \t]+and[ \t]+property[ \t]+courts)(?![\w'’]))"
_EN_COURT_LEVEL = (
    r"(?:" + _EN_COURT_DIVISIONS + _EN_COURT_CONTINUATION + r"?" + _EN_COURT_CHAMBER + r"?"
    r"|" + _EN_COURT_QUALIFIERS + r"(?:" + _title_and_caps("Court", "Division", "List", "Registry") + r")(?![\w'’])"
    + _EN_COURT_CONTINUATION + r"?" + _EN_COURT_CHAMBER + r"?)"
)
ENGLISH_COURT_RULE = DetectionRule(
    "court_or_authority",
    r"(?P<court_place>)(?<![\w'’])(?P<institution>"
    + _EN_COURT_PARENT + _EN_COURT_UNIT + r"(?:[ \t]*,[ \t]*" + _EN_COURT_LEVEL + r")+"
    r"|" + _EN_COURT_UNIT
    + r")",
    "HIGH",
    "Replace with consistent pseudonym or generalize",
    "COURT_AUTHORITY",
    flags=re.UNICODE,
)


def _inside_any(start: int, end: int, spans: list[tuple[int, int]]) -> bool:
    return any(span_start <= start and end <= span_end for span_start, span_end in spans)


def _enclosing_span_end(start: int, spans: list[tuple[int, int]]) -> int | None:
    """End of the narrowest span in `spans` that strictly contains `start`.

    Strictly: a match starting at exactly the same offset as a span is a
    competing reading of the same text, not a fragment of it, and is kept so
    _dedupe_findings and build_redacted_preview can rank the two on risk.

    Narrowest or widest is inconsequential: if two spans both strictly contain
    `start`, every offset between their ends is still strictly inside the wider
    one and is skipped again on the next iteration, so both choices reach the
    same resume point. min() is used for determinism; a min->max mutation is an
    equivalent mutant (verified: identical findings on a 15,000-sentence fuzz).
    """
    ends = [span_end for span_start, span_end in spans if span_start < start < span_end]
    return min(ends) if ends else None


def _generic_court_matches(text: str, named_spans: list[tuple[int, int]]):
    """Yield GENERIC_COURT_SUFFIX_RULE matches that are not a named court's tail.

    Resumes at the end of the enclosing named span instead of the end of the
    skipped match, so an authority mentioned inside the skipped 120-character
    span is still offered its own finding. See the note on
    GENERIC_COURT_SUFFIX_RULE for why dropping the match outright is not
    equivalent. Terminates because every resume offset is strictly greater than
    the INSTITUTION ANCHOR of the match that produced it, which is itself at or
    after the previous offset.

    Suppression is decided on that anchor -- where the institution word itself
    begins, i.e. the end of the place prefix -- and never on the widened match
    start. The place prefix is a decoration on the same institution, so how far
    back it reaches must not change which matches are suppressed.

    The case that separates them is a named span ENDING between a place word and
    its institution word, which the Yargıtay/Danıştay rule's fixed 60-character
    sweep produces. Measured: with the sweep boundary at offset 68 and
    "Kadıköy Valiliği" straddling it, the anchor (68) is not strictly inside the
    span (0, 68) and the governorship keeps "Kadıköy Valiliği islemi ..."; the
    widened start (60) is, so the match is skipped and the scan resumes at 68 --
    re-finding the authority as "Valiliği islemi ...", without its city. The
    city is then only inside the Danıştay finding, so it carries a DIFFERENT
    venue's placeholder. Nothing is lost outright (the resume offset never
    passes the institution word, so the match is always re-found), but the
    narrowing is exactly the defect this branch exists to remove.
    """
    pattern = re.compile(GENERIC_COURT_SUFFIX_RULE.pattern, GENERIC_COURT_SUFFIX_RULE.flags)
    position = 0
    while True:
        match = pattern.search(text, position)
        if match is None:
            return
        enclosing_end = _enclosing_span_end(match.end("court_place"), named_spans)
        if enclosing_end is None:
            yield match
            position = match.end()
        else:
            position = enclosing_end


# A "Vergi Dairesi:" field label at the end of the text in front of a match, on
# the same line. Every Turkish party block carries one.
_TAX_OFFICE_LABEL_RE = re.compile(r"\bvergi[ \t]+dairesi[ \t]*:[ \t]*$", re.IGNORECASE | re.UNICODE)


def _follows_tax_office_label(match: re.Match, text: str) -> bool:
    """A tax office written as the value of a "Vergi Dairesi:" field.

    "Vergi Dairesi: Kozyatağı Vergi Dairesi" names the office twice over: the
    label says the value is a tax office, and the value repeats the institution
    after its name. The capitalised words between the colon and the second
    "Vergi Dairesi" are therefore the office's name, whether or not the place
    list carries them. Tax offices are named for neighbourhoods and historical
    names as often as for districts -- Kozyatağı, Göztepe, Büyük Mükellefler --
    and the list gate gave every one of those back to nobody, so the synthetic
    NDA delivered "Vergi Dairesi: Kozyatağı Vergi Dairesi" verbatim beside the
    party's pseudonymised tax number.

    The rule is exactly that shape. It does not reach the bare form
    "Vergi Dairesi: Kadıköy" -- there is no second institution word to anchor
    on; TAX_OFFICE_FIELD_RULE reports that value -- and it never applies outside
    the field, so prose like "Dosya Vergi Dairesi" stays given back as before.
    """
    if "tax_office" not in match.re.groupindex or match.group("tax_office") is None:
        return False
    line_start = text.rfind("\n", 0, match.start()) + 1
    return bool(_TAX_OFFICE_LABEL_RE.search(text, line_start, match.start()))


def _venue_prefix_start(match: re.Match, text: str) -> int:
    """First captured prefix word that belongs to the venue name.

    Every court_or_authority rule but TAX_OFFICE_FIELD_RULE, and the
    bar-registration rule, carry the prefix, so every one of their matches
    gets here; the call site checks for
    the group first, and a rule without it never does.

    Walks the captured words RIGHT TO LEFT and keeps the maximal trailing run
    the place list recognises, stopping at the first word it does not.
    "İstanbul Anadolu 4. Asliye" keeps both; "Ada Lojistik Limited Şirketi 3.
    Asliye" keeps neither, so the company's legal-form word stays in its own
    company_name finding instead of being welded onto the court's.

    The list is the only signal but one: a tax office written as the value of a
    "Vergi Dairesi:" field keeps every captured word (_follows_tax_office_label).

    A line-position escape stood here for one round
    -- a venue alone on its line kept every captured word, whatever the list
    said -- and it reached past a place the list had already recognised:
    "Sayın Ankara 5. Asliye Hukuk Mahkemesi" as a heading took "Sayın" too, so
    the same venue in the body below it came out a second pseudonym and "Sayın"
    was deleted from the deliverable. The bundled sample's invented "Masalkent"
    is in turkish_places.SYNTHETIC_CORPUS_PLACES instead, which covers it in a
    heading and mid-prose alike.
    """
    if _follows_tax_office_label(match, text):
        return match.start("court_place")
    start = match.end("court_place")
    for word in reversed(list(re.finditer(r"\S+", match.group("court_place")))):
        if not turkish_places.is_venue_place_token(word.group()):
            break
        start = match.start("court_place") + word.start()
    return start


def _court_start(match: re.Match, text: str, claimed: list[tuple[int, int]]) -> int:
    """Start of the venue name: its place words, minus anything already claimed.

    The court twin of _address_start(). Trimming to the END of an overlapping
    claim rather than dropping the prefix outright is what keeps "Davacı Ankara
    5. Asliye Hukuk Mahkemesi" covering Ankara: the role word goes, the place
    stays. A claim that runs past the prefix and into the court name itself
    cannot be trimmed around, so the whole prefix is given up -- clamping at
    prefix_end, never past it, is what keeps the returned start inside the
    match.
    """
    prefix_end = match.end("court_place")
    start = _venue_prefix_start(match, text)
    for claim_start, claim_end in claimed:
        if claim_start < prefix_end and start < claim_end:
            start = min(claim_end, prefix_end)
    remainder = text[start:prefix_end]
    return start + len(remainder) - len(remainder.lstrip(" \t"))


def _is_unplaced_tax_office(match: re.Match, start: int) -> bool:
    """A bare "Vergi Dairesi" or "İcra Dairesi" match with no place word left in front of it.

    Decided on the start _court_start() returned, so a district the list
    rejects or another finding claims counts as absent. Every consumer of
    DIRECTORATE_RULE's matches drops these, so a dropped match neither emits a
    finding nor hides a generic-rule match as a named span.
    """
    return any(
        group in match.re.groupindex
        and match.group(group) is not None
        and match.end(group) == match.end()
        and start >= match.end("court_place")
        for group in ("tax_office", "enforcement_office")
    )


def _venue_identity(text: str, value: str, start: int, place_end: int) -> tuple[str, str]:
    """A court finding's (core, place), folded, for pseudonym grouping.

    The core is the venue itself -- the branch number and branch words for a
    numbered rule, the institution word onward for the others. The place is
    whatever of the leading place prefix survived _court_start().

    Every court_or_authority rule but TAX_OFFICE_FIELD_RULE carries the prefix,
    so `place_end` is always a real offset here and there is one keying rule
    rather than two; that rule's value is keyed by _tax_office_field_identity. Cores still
    cannot collide across rules: a numbered core begins with a digit and every
    other core begins with an institution word.
    """
    core = _pseudonym_key(text[place_end:start + len(value)])
    # "7. Noteri" (the notary) and "7. Noterliği" (the notary's office) name one
    # venue, so they share its pseudonym.
    core = re.sub(r"\bnoteri$", "noterliği", core)
    # Likewise every spelling of one high-court chamber -- "3.Hukuk Dairesi",
    # "3. Hukuk Daire", "3. HD", and a suffixed "Dairesinin" whose finding ends
    # at the stem -- keys as "3. hukuk daire". Only the key is folded, never the
    # sample or span; a different court or ordinal still keys apart
    # (tests/test_court_citations.py).
    core = re.sub(r"(\d\.)\s*(?=[^\W\d])", r"\1 ", core)
    core = re.sub(r"(\d\. )hd$", r"\1hukuk daire", core)
    core = re.sub(r"(\d\. )cd$", r"\1ceza daire", core)
    core = re.sub(r"(\d\..*\bdaire)si$", r"\1", core)
    return core, _pseudonym_key(text[start:place_end])


def _tax_office_field_identity(match: re.Match) -> tuple[str, str]:
    """A "Vergi Dairesi:" field value's (core, place), keyed as the office is.

    DIRECTORATE_RULE keys "Kadıköy Vergi Dairesi" on core "Vergi Dairesi" and
    place "Kadıköy"; the field value carries the same two parts in the other
    order, so it takes the same key and the two mentions share one pseudonym.
    """
    return _pseudonym_key(match.group("field_label")), _pseudonym_key(match.group("tax_office_value"))


def _bar_identity(text: str, number: str, start: int, place_end: int) -> tuple[str, str]:
    """A bar-registration finding's (core, place), the same shape as a venue's.

    The number arrives from the rule's own `bar_number` group rather than being
    re-parsed out of the sample. A second copy of the digit pattern here would
    be a second definition of one thing 800 lines from the first, and the day
    the rule's tail grew a checksum letter or a grouped number the re-parse
    would raise inside analyze_privacy -- on the detection path for every
    document, rather than at the rule that changed.

    The core is the registration NUMBER, because that is the identifier: the
    leading "İstanbul Barosu" / "Baro" / nothing is decoration, exactly as a
    venue's city is decoration on its branch number. Widening this rule's span
    to cover the association and its city changed the matched string, and
    keying the pseudonym on that string made one registration read as two --
    "Baro Sicil No: 45678" in a signature block and a bare "Sicil No: 45678" in
    an annex came out [BAR_REGISTRATION_1] and [BAR_REGISTRATION_2].

    The place still goes in the identity so the collision guard has something to
    work with: registration numbers are issued per bar association, so the same
    digits under two different cities are two different lawyers and must stay
    distinct.
    """
    return _pseudonym_key(number), _pseudonym_key(text[start:place_end])


def _assign_grouped_placeholders(
    prefix: str,
    findings: list[dict],
    identities: dict[int, tuple[str, str]],
    counters: Counter[str],
    state: dict[tuple[str, str], str],
) -> None:
    """One entity, one pseudonym -- unless the document names two of them.

    Shared by courts and bar registrations, which have the same problem in the
    same shape: a widened span put decoration in the matched string, and the
    pseudonym was keyed on that string. One grouping rule serves both; a second
    copy would be the place the next divergence hides.

    "Masalkent 3. Asliye Hukuk Mahkemesi" in a heading and "3. Asliye Hukuk
    Mahkemesi" in the body below it are the same court, and keying the pseudonym
    on the raw matched string made them [COURT_AUTHORITY_1] and
    [COURT_AUTHORITY_2] -- two courts where there is one, which misleads a
    reader following the document. Grouping on the folded core fixes that.

    Keying on the core ALONE introduces the mirror image, which is worse: the
    core of "Ankara 5. Asliye Hukuk Mahkemesi" and of "İzmir 5. Asliye Hukuk
    Mahkemesi" is the same string, so both would collapse into one placeholder
    and the document would assert that two courts in two different cities are
    the same venue. So a group unifies only when it carries at most one DISTINCT
    place, with "no place" compatible with that one; where two or more places
    share a core, each finding keeps its own place in the key and they stay
    distinct.

    That guard counts the places actually CAPTURED, so the bounded place list
    disarms it as well as leaking: where one of the two cities is not in
    turkish_places, its mention captures no place, the group has one distinct
    place, and the two entities collapse into one pseudonym. Measured on "Konak
    27. Noterliği ile Alsancak 27. Noterliği arasında" -- Alsancak is checked
    against the list in this commit and is absent, which is the whole point of
    choosing it: it is the residual turkish_places keeps open deliberately, so
    unlike a real district it will not quietly enter the list and rot this
    example the way an earlier "Çubuk" one did. Both findings take
    [COURT_AUTHORITY_1] and the preview reads "[COURT_AUTHORITY_1] ile Alsancak
    [COURT_AUTHORITY_1] arasında". The keying is still strictly finer than the
    whole-sample keying it replaced, but the list gap costs more than the leak
    alone.

    That last branch has a known cost, taken deliberately: a mention carrying NO
    place word keys on its own empty place, so a document naming "Ankara 5.
    Asliye Hukuk Mahkemesi", "İzmir 5. Asliye Hukuk Mahkemesi" and a bare "5.
    Asliye Hukuk Mahkemesi" comes out as [COURT_AUTHORITY_1], _2 and _3 -- three
    venues where it names two. It is preferred because the bare mention cannot
    be attributed to either named court without guessing, and the error in the
    other direction is worse: keying on the core alone collapsed all three into
    one placeholder and had the delivered document assert that the Ankara and
    İzmir courts were the same venue. No text leaks either way.

    Assigning here rather than in the rule loop is what lets the decision see
    the whole document. Court findings are appended in the loop with an empty
    placeholder and filled in order, so the numbering a document gets is the
    same one the loop would have produced, minus the numbers merging frees.
    """
    places_by_core: dict[str, set[str]] = {}
    for core, place in identities.values():
        places_by_core.setdefault(core, set())
        if place:
            places_by_core[core].add(place)

    for index, (core, place) in identities.items():
        key = core if len(places_by_core[core]) <= 1 else f"{place}\x00{core}"
        findings[index]["placeholder"] = _placeholder_for(prefix, key, counters, state)
