"""Document classification helpers."""

from __future__ import annotations

import bisect
import re
from collections.abc import Iterable
from pathlib import Path

from .taxonomy import CLASSIFICATION_RULES, IGNORE_PATTERNS, SUPPORTED_EXTENSIONS


def should_skip(filename: str) -> bool:
    return any(pattern in filename for pattern in IGNORE_PATTERNS)


def supported_file(path: str | Path) -> bool:
    return Path(path).suffix.lower() in SUPPORTED_EXTENSIONS


TURKISH = "TR"
ENGLISH = "EN"
UNKNOWN_LANGUAGE = "Unknown"

# The language signal drives a safety decision (see language_coverage() and the
# external-LLM gate in legal_analyzer/privacy.py), so it is derived from the
# document's own text. It used to be derived from the FILENAME: a keyword list
# against `path.name`, defaulting to "TR". Measured over 40 held-out synthetic
# documents, that recorded 18 of 20 English documents as Turkish, which hid the
# English-coverage notice on exactly the documents that needed it.
#
# This is a language signal, not a classifier, and it reads exactly one thing:
# Turkish function WORDS from a short list, matched as whole tokens. Letters are
# not a signal -- a letter-based path existed, keyed the safety gate on personal
# data, and was removed; see docs/language-gate.md before considering one
# again. Diacritics survive only as spelling: the marker list carries both the
# diacritic and the ASCII form of each word ("için" and "icin"), because Turkish
# legal text is routinely typed without them.
_WORD_RE = re.compile(r"[^\W\d_]+", re.UNICODE)

# "İ".lower() is "i" + U+0307 COMBINING DOT ABOVE, so the dot is stripped before
# a token is compared against the marker lists ("İLE" -> "ile"). The marker list
# carries both the diacritic and ASCII spelling of each word, so "için" and
# "icin" both count here.
_COMBINING_DOT_ABOVE = "\u0307"

_TURKISH_MARKERS = frozenset({
    "ve", "ile", "için", "icin", "bu", "bir", "olan", "olarak", "tarafından", "tarafindan",
    "arasında", "arasinda", "madde", "sayılı", "sayili", "tarih", "tarihli", "işbu", "isbu",
    "aşağıda", "asagida", "kanun", "kanunu", "hükümleri", "hukumleri", "sözleşme", "sozlesme",
    "sözleşmenin", "sozlesmenin", "taraf", "taraflar", "gereği", "geregi", "ilgili", "ayrıca",
    "ayrica", "ancak", "göre", "gore", "karar", "kararı", "karari", "dava", "davacı", "davaci",
    "davalı", "davali", "mahkeme", "mahkemesi", "adres", "adresi", "konu", "konusu", "talep",
    "beyan", "nedeniyle", "hakkında", "hakkinda", "veya", "değil", "degil", "olup", "olduğu",
    "oldugu", "bulunan", "edilir", "edilmiştir", "edilmistir", "etmiştir", "etmistir",
    "yapılan", "yapilan", "kişisel", "kisisel", "veri", "verilerin", "şirket", "sirket",
    "şirketi", "sirketi", "vekili", "sayın", "sayin", "tarafı", "tarafi", "üzere", "uzere",
})

# No entry here may also be an ordinary Turkish word. "her" (each), "on" (ten),
# "not" (note), "an" (moment), "as", "at", "in", "it" and "may" were, and a
# monolingual Turkish supply contract using "her iki taraf", "her türlü", "on
# gün" and "on iş günü" scored six English hits and was refused as
# unidentifiable -- the non-Turkish refusal applied to the one language the
# rules are written for. English has no shortage of markers without them.
_ENGLISH_MARKERS = frozenset({
    "the", "and", "of", "to", "that", "for", "with", "shall", "be", "by",
    "from", "this", "or", "any", "was", "will", "are", "has", "have",
    "which", "all", "such", "hereby", "herein", "hereof", "thereof", "whereas", "agreement",
    "between", "party", "parties", "said", "been", "were", "would", "my", "his",
    "their", "we", "you", "if", "but", "must", "other", "than", "into", "upon",
    "statement", "witness", "number", "under", "who", "where", "when", "there", "these",
})

# Below this many words there is not enough text to tell the languages apart,
# and below this much marker evidence the answer is a guess. Both fall through
# to UNKNOWN_LANGUAGE, which the release gate treats exactly as it treats
# English: silence is not clearance.
_MIN_WORDS = 20
_MIN_SIGNAL = 3

# The question is whether the document carries an English SECTION -- an exhibit,
# a witness-statement annex, the English column of a bilingual contract -- whose
# personal data would then be measured by rules with 0.831 recall on English and
# 38 of 76 on English person names (0.246 and 0 of 76 before the English rules)
# against 0.934 on Turkish. That is a question about POSITION, not
# quantity, and every quantity answer tried here failed on one side or the
# other: a share of the marker evidence was defeated by padding the Turkish body
# (an English witness statement behind nine repetitions of one boilerplate
# clause reached a 0.23 share and came back Turkish, gate allowed), and an
# absolute count refused ordinary Turkish filings as they got longer (the same
# monolingual share-purchase agreement clears at 100 words and is refused at
# 300, at identical English density).
#
# So the measure is the longest CLUSTERED run of English markers: Turkish
# evidence resets the run, two English markers more than _ENGLISH_RUN_GAP words
# apart do not chain, and anything else is neutral and skipped. The run is
# invariant to document length -- measured, the agreement scores 2 at 100 words
# and 2 at 1000, and the annex scores 12 behind a Turkish body of 1, 9 or 28
# clauses. Padding cannot buy English content and length cannot accumulate a
# refusal.
#
# Measured on this corpus the highest must-clear run is 2 and the lowest
# must-refuse run is 9; 5 sits in that band, 2.5x above one and 1.8x below the
# other. The gap is not sensitive: at G = 3, 4, 6 and 8 the band is identical,
# so there is nothing here to tune.
#
# A bilingual title needs no special case: "HISSE DEVIR SOZLESMESI (SHARE
# PURCHASE AGREEMENT)" contributes one marker, because "share" and "purchase"
# are not in _ENGLISH_MARKERS, and isolated markers separated by Turkish
# evidence never chain.
#
# KNOWN, ACCEPTED EDGE: a Turkish contract quoting one long English clause
# verbatim scores 14 and is refused. That is a contiguous English section on
# this definition, and the direction of failure is the safe one -- a false
# refusal costs a manual review, a false clearance sends unredacted personal
# data off the machine. Do not special-case quotations.
_ENGLISH_RUN_GAP = 4
_MAX_ENGLISH_RUN = 5


def _sorted_spans(entity_spans: Iterable[tuple[int, int]]) -> tuple[list[int], list[int]]:
    ordered = sorted((start, end) for start, end in entity_spans if end > start)
    merged: list[tuple[int, int]] = []
    for start, end in ordered:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return [s for s, _ in merged], [e for _, e in merged]


def _marker_evidence(text: str, entity_spans: Iterable[tuple[int, int]] = ()) -> tuple[int, int, int]:
    """Turkish hits, English hits, and the longest clustered run of English.

    Turkish evidence is a Turkish WORD, outside any detected entity. The same
    defect arrived three times through the same door: a Turkish PERSON name
    supplied ğ/ı/ş/İ/Ş, then a Turkish PLACE name did, and when the letter path
    was removed a Turkish COMPANY name supplied the marker words themselves --
    "Demir Tekstil Sanayi **ve** Ticaret Anonim **Sirketi**" scores two, so
    naming one employer twice cleared the floor on a wholly English data sheet
    that reached "Allowed for external LLM use" over a London address, a UK
    mobile, a date of birth and a national insurance number.

    A Turkish entity name contains Turkish words because that is what an entity
    name is. Those words are evidence of an ENTITY, not of the document's
    language, so the spans the detector already found are excluded here. The
    caller passes them; this module must never import privacy.py, which imports
    it. A caller that cannot supply spans gets a MORE permissive answer, so a
    caller without them must not clear a document on this signal.

    The exclusion is deliberately ASYMMETRIC: it guards the Turkish arm alone,
    and English markers are counted wherever they fall, inside a detected
    entity or not. The two directions are not mirror images. Dropping Turkish
    evidence can only REFUSE more documents; dropping English evidence can only
    CLEAR more of them. Applying the span test to both arms reopened the
    clustered-English route through the back door, because the context sweeps
    are wide -- `privileged_or_confidential` covers 160 characters and triggers
    on the English word "privileged" -- so one span blanketed
    an English annex, erased 14 of its 17 English markers, cut the run from 14
    to 2, and a Turkish filing carrying an unredacted London address, UK mobile
    and national insurance number reached "Allowed for external LLM use".

    The cost is real and accepted: a Turkish document whose Turkish content is
    mostly entity names, or one that is simply short, no longer clears. Five of
    the twenty-three Turkish gold fixtures are refused for that reason. See the
    known limitations in docs/language-gate.md -- the gate is conservative, not
    complete.
    """
    starts, ends = _sorted_spans(entity_spans)
    turkish = 0
    english = 0
    longest_run = 0
    run = 0
    previous_english = None
    for index, match in enumerate(_WORD_RE.finditer(text or "")):
        token = match.group(0).lower().replace(_COMBINING_DOT_ABOVE, "")
        if token in _TURKISH_MARKERS:
            slot = bisect.bisect_right(starts, match.start()) - 1
            if slot >= 0 and match.start() < ends[slot]:
                continue
            turkish += 1
            run = 0
            previous_english = None
        elif token in _ENGLISH_MARKERS:
            english += 1
            chained = previous_english is not None and index - previous_english <= _ENGLISH_RUN_GAP
            run = run + 1 if chained else 1
            previous_english = index
            longest_run = max(longest_run, run)
    return turkish, english, longest_run


def entity_spans_of(findings: Iterable[dict]) -> list[tuple[int, int]]:
    """The (start, end) spans of detected entities, for `detect_language`.

    A finding without offsets contributes nothing, which makes the answer more
    permissive rather than less -- so a caller holding findings whose offsets
    are missing must not read a TURKISH answer as clearance.
    """
    spans = []
    for finding in findings or ():
        start, end = finding.get("start"), finding.get("end")
        if isinstance(start, int) and isinstance(end, int):
            spans.append((start, end))
    return spans


def detect_language(text: str, entity_spans: Iterable[tuple[int, int]] = ()) -> str:
    """Return TURKISH, ENGLISH, or UNKNOWN_LANGUAGE for extracted document text.

    UNKNOWN_LANGUAGE is the answer for an empty extraction, a failed OCR, a very
    short document, any text whose evidence is too thin to call, and a Turkish
    document carrying a clustered English section however long its Turkish half
    is. It is not an error state; it is the honest answer, and callers must not
    read it as Turkish.

    `entity_spans` are the detector's own findings (see `entity_spans_of`).
    Turkish words inside them do not count, because an entity name contains
    Turkish words by definition. English markers are counted inside them too --
    see `_marker_evidence` for why that asymmetry is load-bearing. Omitting the
    spans only ever counts MORE Turkish and never less English, so a caller
    that cannot supply them must not clear a document on the answer it gets
    back.
    """
    if len(_WORD_RE.findall(text or "")) < _MIN_WORDS:
        return UNKNOWN_LANGUAGE

    turkish, english, longest_english_run = _marker_evidence(text, entity_spans)

    if turkish >= _MIN_SIGNAL and longest_english_run < _MAX_ENGLISH_RUN:
        return TURKISH
    if english >= _MIN_SIGNAL and english > turkish:
        return ENGLISH
    return UNKNOWN_LANGUAGE


def detect_version(filename: str) -> str | None:
    patterns = [r"[vV]\.?\s*(\d+\.?\d*)", r"VL\s*(\d+\.?\d*)", r"_v(\d+)", r"version\s*(\d+\.?\d*)"]
    for pattern in patterns:
        match = re.search(pattern, filename, re.IGNORECASE)
        if match:
            return f"v{match.group(1)}"
    return None


def detect_date(filename: str) -> str | None:
    patterns = [
        r"(\d{4})(\d{2})(\d{2})",
        r"(\d{4})[._-](\d{2})[._-](\d{2})",
        r"(\d{2})[._-](\d{2})[._-](\d{4})",
        r"(\d{2})(\d{2})(\d{4})",
    ]
    for index, pattern in enumerate(patterns):
        match = re.search(pattern, filename)
        if not match:
            continue
        groups = match.groups()
        if index <= 1:
            year, month, day = groups
        else:
            day, month, year = groups
        try:
            y, m, d = int(year), int(month), int(day)
        except ValueError:
            continue
        if 2020 <= y <= 2035 and 1 <= m <= 12 and 1 <= d <= 31:
            return f"{y:04d}-{m:02d}-{d:02d}"
    return None


def generate_title(filename: str) -> str:
    title = Path(filename).stem
    title = title.replace("_", " ")
    return re.sub(r"\s+", " ", title).strip()


def classify_document(filename: str, text: str = "") -> dict:
    haystack = f"{filename}\n{text[:4000]}".lower()
    for keywords, category, subcategory, tags in CLASSIFICATION_RULES:
        if any(keyword.lower() in haystack for keyword in keywords):
            return {"category": category, "subcategory": subcategory, "tags": tags}
    return {"category": "other", "subcategory": "miscellaneous", "tags": ["sınıflandırılmamış"]}

