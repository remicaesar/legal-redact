"""What a piece of legal text IS -- the classification layer.

The redaction pipeline used to go straight from a regex hit to a deletion: a
rule's category fixed its risk, its recommended action and its placeholder, so
"the word `gizli` appeared" became "this clause is privileged" became
"[PRIVILEGED_CONTENT_3_REDACTED]". On the synthetic Turkish NDA that deleted
the definition of confidential information, its exceptions and the receiving
party's obligations -- the provisions a reviewing lawyer needs most -- and
called the penalty clause a criminal allegation because it says "cezai şart".

This module answers only "what is this?". It never decides what happens to a
span; that is ``redaction_policy``'s job, and the transformation is
``redaction_plan`` / ``docx_redactor`` / ``pdf_redactor``. Four questions are
answered here, each for a different consumer in ``privacy.analyze_privacy``:

* **Which clauses are legally operative**, and of what kind
  (``classify_provisions``). A provision is never a redaction candidate; it is
  the thing redaction must leave readable. The privacy profile stores a count
  per type (``provision_summary``) -- never text -- and the
  semantic-preservation measurement (``semantic_preservation``) checks that
  their operative terms survive the rendering.
* **What role a value plays inside its clause** (``amount_role``,
  ``date_role``), so a penalty amount is typed ``[PENALTY_AMOUNT_1]`` and a
  birth date ``[BIRTH_DATE_1]`` rather than one generic placeholder each.
* **What kind of institution a court/authority finding names**
  (``institution_type``): a tax office is not a court, and the placeholder says
  which it is even after the specific office is pseudonymised.
* **Whether a counsel/client word is evidence of privilege**
  (``privilege_evidence``). Confidentiality is not privilege: an NDA's
  "gizli bilgi" is a defined term of contract law, not attorney-client
  communication.

Everything here is lexical and heuristic, and says so: it is a Turkish- and
English-term dictionary plus clause co-occurrence, not a parser. It is allowed
to be incomplete in one direction only. A clause it fails to recognise is
simply not listed as a provision -- detection and review proceed exactly as
before for anything inside it. Nothing in this module can remove a finding for
a direct identifier; ``privacy`` consults it only to type values, to type
institutions, and to decide whether two context triggers (``cezai`` and the
counsel/client words) are legal vocabulary rather than sensitive content.

This module must not import ``privacy`` (which imports it).
"""

from __future__ import annotations

import bisect
import re
from dataclasses import dataclass

from legal_analyzer.docx_redactor import TURKISH_FOLD_BY_CHAR, expand_to_word_bounds
from legal_analyzer.turkish_names import tr_fold

_FLAGS = re.IGNORECASE | re.UNICODE

# --- The ontology ----------------------------------------------------------
#
# Three families, kept apart because they get opposite default treatments.
# The finding CATEGORIES in privacy.py (and in the database, the gold set, the
# held-out eval and the UI) are unchanged; these are the semantic types a
# finding or a clause is classified into, and they are what redaction_policy
# keys its treatments on.

# Identity and identifier types: normally pseudonymised or removed.
IDENTITY_TYPES_BY_CATEGORY = {
    "natural_person_name": "PERSON_NAME",
    "turkish_national_id": "NATIONAL_ID",
    "passport_number": "PASSPORT_NUMBER",
    "phone_number": "PHONE_NUMBER",
    "email_address": "EMAIL_ADDRESS",
    "address": "STREET_ADDRESS",
    "iban": "IBAN",
    "account_number": "BANK_ACCOUNT",
    "company_name": "COMPANY_NAME",
    "tax_number": "TAX_NUMBER",
    "mersis_number": "MERSIS_NUMBER",
    "trade_registry_number": "TRADE_REGISTRY_NUMBER",
    "bar_registration_number": "BAR_REGISTRATION_NUMBER",
    "case_or_investigation_number": "CASE_NUMBER",
    "social_media_handle": "SOCIAL_MEDIA_HANDLE",
    "vehicle_plate": "VEHICLE_PLATE",
    "land_registry_parcel": "LAND_PARCEL",
    "manual_sensitive_text": "REVIEWER_MARKED_TEXT",
}

# Sensitive contextual information: handled by policy, not deleted by default.
# TRADE_SECRET, NON_PUBLIC_FINANCIAL_INFORMATION and BUSINESS_STRATEGY are part
# of the vocabulary so a policy can name them, but NO automated rule emits them:
# a regex can recognise the phrase "ticari sır", which is a category word in a
# definition clause, and cannot recognise an actual trade secret. Detecting the
# phrase and deleting the clause around it is exactly the failure this layer
# was introduced to stop.
SENSITIVE_CONTEXT_TYPES = (
    "ATTORNEY_CLIENT_COMMUNICATION",
    "ATTORNEY_WORK_PRODUCT",
    "LEGAL_ADVICE",
    "LITIGATION_STRATEGY",
    "ATTORNEY_CLIENT_PRIVILEGE",
    "SPECIAL_CATEGORY_HEALTH_DATA",
    "CRIMINAL_ALLEGATION",
    "TRADE_SECRET",
    "NON_PUBLIC_FINANCIAL_INFORMATION",
    "BUSINESS_STRATEGY",
    "PERSONAL_DATA",
)

SENSITIVE_TYPES_BY_CATEGORY = {
    "health_data": "SPECIAL_CATEGORY_HEALTH_DATA",
    "criminal_allegation": "CRIMINAL_ALLEGATION",
    # Refined per finding by privilege_evidence(); this is the fallback.
    "privileged_or_confidential": "ATTORNEY_CLIENT_PRIVILEGE",
}

# Quasi-identifiers whose treatment depends on the task: a contract review needs
# the penalty amount and the notice deadline, a strict privacy pass does not.
CONTEXTUAL_VALUE_TYPES = (
    "AMOUNT",
    "PENALTY_AMOUNT",
    "LIABILITY_CAP",
    "DATE",
    "BIRTH_DATE",
    "SIGNATURE_DATE",
    "DEADLINE",
    "PARTY_ROLE",
)

# Legally operative language: preserved by default in every profile. A
# provision may CONTAIN a sensitive value (a party's name, an amount) and that
# value is still its own finding -- redact the span, not the proposition.
LEGAL_PROVISION_TYPES = (
    "CONTRACTUAL_CONFIDENTIALITY",
    "CONFIDENTIALITY_DEFINITION",
    "CONFIDENTIALITY_EXCEPTION",
    "CONTRACTUAL_OBLIGATION",
    "CONTRACTUAL_RIGHT",
    "CONTRACTUAL_PENALTY",
    "PURPOSE_LIMITATION",
    "NEED_TO_KNOW",
    "REPRESENTATION",
    "WARRANTY",
    "INDEMNITY",
    "LIABILITY",
    "LIMITATION_OF_LIABILITY",
    "TERMINATION",
    "GOVERNING_LAW",
    "JURISDICTION",
    "DISPUTE_RESOLUTION",
    "IP_OWNERSHIP",
    "LICENSE_SCOPE",
    "PAYMENT_TERM",
    "NOTICE",
    "NOTICE_PERIOD",
    "CONTRACT_DURATION",
    "SURVIVAL_CLAUSE",
    "EXECUTION",
    "PARTIES",
    "TIME_PERIOD",
)

# --- Institution types -----------------------------------------------------
#
# Every court_or_authority finding used to carry the one placeholder prefix
# COURT_AUTHORITY, so "Çankaya Vergi Dairesi" in a party block read as a court
# in the delivered document. The type is decided on the FULL institution
# expression, most specific first, and never on a generic head word: "Dairesi"
# is a Yargıtay chamber in "9. Hukuk Dairesi", an enforcement office in
# "İcra Dairesi" and a tax office in "Vergi Dairesi".
COURT = "COURT"
COMMERCIAL_COURT = "COMMERCIAL_COURT"
ENFORCEMENT_OFFICE = "ENFORCEMENT_OFFICE"
ARBITRAL_INSTITUTION = "ARBITRAL_INSTITUTION"
REGULATOR = "REGULATOR"
TAX_AUTHORITY = "TAX_AUTHORITY"
ADMINISTRATIVE_AUTHORITY = "ADMINISTRATIVE_AUTHORITY"
LAW_ENFORCEMENT_AUTHORITY = "LAW_ENFORCEMENT_AUTHORITY"
NOTARY = "NOTARY"
PROSECUTOR_OFFICE = "PROSECUTOR_OFFICE"

INSTITUTION_TYPES = (
    COURT,
    COMMERCIAL_COURT,
    ENFORCEMENT_OFFICE,
    ARBITRAL_INSTITUTION,
    REGULATOR,
    TAX_AUTHORITY,
    ADMINISTRATIVE_AUTHORITY,
    LAW_ENFORCEMENT_AUTHORITY,
    NOTARY,
    PROSECUTOR_OFFICE,
)

# Ordered: the first matching rule wins. Each is matched against the
# Turkish-folded, diacritic-stripped institution text, so "MAHKEMESİ",
# "Mahkemesi" and the OCR spelling "MAHKEMESI" are one word.
_INSTITUTION_RULES: tuple[tuple[str, str], ...] = (
    (ARBITRAL_INSTITUTION, r"\btahkim\b|\barbitra|\bistac\b|\bicc\b"),
    # A court or a chamber of one. Checked BEFORE the offices because an
    # enforcement court ("İcra Hukuk Mahkemesi") and a tax court ("Vergi
    # Mahkemesi") are courts, not offices, and a Danıştay "Vergi Dava Dairesi"
    # is a chamber, not a tax office.
    (COMMERCIAL_COURT, r"\bticaret\s+mahkeme|\bcommercial\s+court\b"),
    # The English venues privacy.ENGLISH_COURT_RULE names: a Court or
    # Tribunal, and the High Court's divisions and court groupings.
    (
        COURT,
        r"mahkeme|hakimli|\byargitay\b|\bdanistay\b|\bbolge\s+adliye\b"
        r"|\b(?:hukuk|ceza|dava)\s+dairesi|\bdava\s+daireleri"
        r"|\bcourts?\b|\btribunals?\b|\bbench\s+division\b|\b(?:chancery|family)\s+division\b",
    ),
    (ENFORCEMENT_OFFICE, r"\bicra\b|\biflas\b"),
    (NOTARY, r"\bnoter"),
    (PROSECUTOR_OFFICE, r"savcilig"),
    (TAX_AUTHORITY, r"\bvergi\s+dairesi|\bgelir\s+idaresi|\bvergi\s+(?:mudurlugu|baskanligi)"),
    (LAW_ENFORCEMENT_AUTHORITY, r"\bemniyet\b|\bjandarma\b|\bpolis\b"),
    (
        REGULATOR,
        r"\brekabet\s+kurumu|\bkisisel\s+verileri\s+koruma|\bkvkk?\b|\bbddk\b|\bbankacilik\s+duzenleme"
        r"|\bsermaye\s+piyasasi|\bspk\b|\bepdk\b|\benerji\s+piyasasi|\bbtk\b|\bbilgi\s+teknolojileri"
        r"|\brtuk\b|\bradyo\s+ve\s+televizyon\s+ust\s+kurulu",
    ),
)

_DIACRITIC_FOLD = str.maketrans("çğıöşüâîû", "cgiosuaiu")


def fold(value: str) -> str:
    """Turkish-aware lower case with the diacritics stripped, for lexicon lookups."""
    return re.sub(r"\s+", " ", tr_fold(value).translate(_DIACRITIC_FOLD).replace("\u0307", ""))



def institution_type(institution: str) -> str:
    """The kind of court or authority ``institution`` names.

    ``institution`` is the institution expression itself -- the venue's branch
    and institution words, not a trailing sweep of the sentence after it (a
    sweep can carry another institution's name). Anything the rules do not
    recognise is ADMINISTRATIVE_AUTHORITY: a governorship, a land registry, a
    trade registry, the social-security institution. That default is the
    least specific claim available, never COURT.
    """
    folded = fold(institution)
    for kind, pattern in _INSTITUTION_RULES:
        if re.search(pattern, folded):
            return kind
    return ADMINISTRATIVE_AUTHORITY


# --- Legally operative terms ----------------------------------------------
#
# Each provision type is recognised by the operative TERMS that make a clause
# that kind of clause. A term is the evidence; the provision is the clause the
# term sits in. Patterns are matched with IGNORECASE and spell the common
# diacritic-free variant beside the diacritic one, the way the detection rules
# do, because Turkish is routinely typed without them.
#
# The penalty-clause head is named separately because privacy.py builds its
# "cezai" trigger exclusion from it: "cezai" before one of these words is the
# contract-law term "cezai şart" / "cezai koşul", not criminal content. One
# definition, so the trigger and the provision classifier cannot drift apart.
PENALTY_CLAUSE_HEADS = r"(?:şart|sart|koşul|kosul)"

# A gap inside one sentence: anything but a line break or a sentence-ending
# period. A period between two digits is a thousands separator ("1.000.000 TL")
# and does not end anything -- without that exception the liability cap in
# "sorumluluğu 1.000.000 TL ile sınırlıdır" was never recognised.
_GAP = r"(?:[^.\n]|(?<=\d)\.(?=\d))"

_TERMS: dict[str, str] = {
    "CONTRACTUAL_PENALTY": (
        r"\bcezai\s+" + PENALTY_CLAUSE_HEADS + r"\w*"
        r"|\bceza\s+(?:koşul|kosul|şart|sart)\w*"
        r"|\bpenalty[ \t]+clauses?\b|\bcontractual[ \t]+penalt(?:y|ies)\b|\bliquidated[ \t]+damages\b"
    ),
    "CONTRACTUAL_CONFIDENTIALITY": (
        r"\bgizli[ \t]+bilgi\w*|\bgizlilik[ \t]+(?:yükümlülü|yukumlulu|taahhüd|taahhut|sözleşme|sozlesme"
        r"|anlaşma|anlasma|ilke|esas)\w*|\bgizli(?:lik)?\b|\bticari[ \t]+s[ıi]r\w*"
        r"|\bconfidential(?:ity)?\b|\bnon-disclosure\b|\bNDA\b|\btrade[ \t]+secrets?\b"
    ),
    "PURPOSE_LIMITATION": (
        r"\b(?:yalnızca|yalnizca|sadece|münhasıran|munhasiran)\b" + _GAP + r"{0,80}?\bamac[ıi]yla\b"
        r"|\bsolely[ \t]+for[ \t]+the[ \t]+purpose\b|\bfor[ \t]+no[ \t]+other[ \t]+purpose\b"
    ),
    "NEED_TO_KNOW": r"\bbilmesi[ \t]+(?:gereken|zorunlu)\w*|\bneed[ -]to[ -]know\b",
    "CONTRACT_DURATION": (
        r"\bsüreyle[ \t]+yürürlükte\b|\bsureyle[ \t]+yururlukte\b|\byürürlükte[ \t]+kal\w*|\byururlukte[ \t]+kal\w*"
        r"|\bsözleşme(?:nin)?[ \t]+süresi\w*|\bsozlesme(?:nin)?[ \t]+suresi\w*"
        r"|\bfor[ \t]+a[ \t]+(?:period|term)[ \t]+of\b|\bshall[ \t]+remain[ \t]+in[ \t]+(?:full[ \t]+)?(?:force|effect)\b"
    ),
    "SURVIVAL_CLAUSE": (
        r"\b(?:sona[ \t]+ermesinden|feshinden|bitiminden)[ \t]+(?:sonra|itibaren)\b"
        r"|\bshall[ \t]+survive\b|\bsurviv(?:e|es|al)\b"
    ),
    "TERMINATION": r"\bfesih\w*|\bfeshed\w*|\bfeshi\b|\bsona[ \t]+er\w*|\bterminat\w*",
    "GOVERNING_LAW": (
        r"\b\w+[ \t]+hukuku[ \t]+uygulan\w*|\bhukukuna[ \t]+tabi\w*|\buygulanacak[ \t]+hukuk\b"
        r"|\bgoverned[ \t]+by[ \t]+(?:the[ \t]+)?laws?[ \t]+of\b|\bgoverning[ \t]+law\b"
    ),
    "JURISDICTION": (
        r"\bmahkemeleri" + _GAP + r"{0,60}?\byetkili\w*|\bicra[ \t]+daireleri" + _GAP + r"{0,40}?\byetkili\w*"
        r"|\byetkili[ \t]+(?:mahkeme|icra)\w*|\bjurisdiction\b"
    ),
    "DISPUTE_RESOLUTION": (
        r"\buyuşmazlı\w*|\buyusmazli\w*|\btahkim\w*|\barabulucu\w*|\bdisputes?\b|\barbitrat\w*|\bmediation\b"
    ),
    "NOTICE": r"\bbildirim\w*|\btebligat\w*|\bihtar\w*|\bihbar\w*|\bnotices?\b|\bnotification\b",
    "NOTICE_PERIOD": (
        r"\b\w+[ \t]+(?:gün|gun|hafta|ay)[ \t]+önceden\b|\bönceden[ \t]+yazılı\b"
        r"|\bdays'?[ \t]+(?:prior[ \t]+)?(?:written[ \t]+)?notice\b"
    ),
    "PAYMENT_TERM": (
        r"\bödeme\w*|\bodeme\w*|\bödenecek\w*|\bodenecek\w*|\bödenir\b|\bbedel\w*|\bücret\w*"
        r"|\bpayment\w*|\bpayable\b|\bfees?\b"
    ),
    "LIABILITY": r"\bsorumlu\w*|\bliab(?:le|ility|ilities)\b",
    "LIMITATION_OF_LIABILITY": (
        r"\bsorumluluğu?" + _GAP + r"{0,60}?\bsınırl\w*|\bsorumlulugu?" + _GAP + r"{0,60}?\bsinirl\w*"
        r"|\bazami[ \t]+sorumlulu\w*|\blimitation[ \t]+of[ \t]+liability\b|\bshall[ \t]+not[ \t]+exceed\b"
        r"|\bliability[ \t]+cap\b|\bcap[ \t]+on[ \t]+liability\b"
    ),
    "INDEMNITY": r"\btazmin\w*|\bindemnif\w*|\bhold[ \t]+harmless\b",
    "WARRANTY": r"\bgaranti\w*|\bwarrant(?:y|ies|s)\b",
    "REPRESENTATION": r"\bbeyan[ \t]+(?:ve[ \t]+taahhüt|eder)\w*|\brepresents?\b|\brepresentations?\b",
    "IP_OWNERSHIP": (
        r"\bfikri[ \t]+(?:mülkiyet|mulkiyet|ve[ \t]+sınai|ve[ \t]+sinai)\w*|\btelif\w*"
        r"|\bintellectual[ \t]+property\b|\bownership\b"
    ),
    "LICENSE_SCOPE": r"\blisans\w*|\blicen[cs]e\w*",
    "EXECUTION": r"\bimzalan\w*|\bimza[ \t]+tarih\w*|\bsigned\b|\bexecuted\b",
    "PARTIES": (
        r"\btaraf(?:lar|ların|larin|ın|in|a|ı|i)?\b|\balıcı[ \t]+taraf\w*|\balici[ \t]+taraf\w*"
        r"|\baçıklayan[ \t]+taraf\w*|\bparty\b|\bparties\b|\breceiving[ \t]+party\b|\bdisclosing[ \t]+party\b"
    ),
    "TIME_PERIOD": (
        r"\b(?:\d+|bir|iki|üç|uc|dört|dort|beş|bes|altı|alti|yedi|sekiz|dokuz|on|on[ \t]+beş|yirmi|otuz"
        r"|kırk|kirk|altmış|altmis|doksan)[ \t]+(?:\(\d+\)[ \t]+)?(?:yıl|yil|ay|hafta|gün|gun|iş[ \t]+günü)\w*"
        r"|\b(?:\d+|one|two|three|four|five|six|seven|ten|thirty|sixty|ninety)[ \t]+(?:years?|months?|weeks?|days?)\b"
    ),
}

# Obligation and right markers. They are clause-level evidence on their own
# rather than terms of a single provision type: almost every contractual
# sentence carries one, and a clause that carries one is a legal proposition
# whatever else it is about. The future suffixes are the Turkish drafting
# convention for an obligation ("kullanacak", "açıklamayacak", "koruyacaktır");
# "-ebilir" / "-abilir" for a permission ("erişebilir").
_OBLIGATION = (
    r"\b\w{2,}(?:acak|ecek)(?:t[ıi]r)?\b|\byükümlü\w*|\byukumlu\w*|\btaahhüt[ \t]+eder\w*|\btaahhut[ \t]+eder\w*"
    r"|\bkabul[ \t]+eder\w*|\bzorunda\w*|\bshall\b|\bmust\b|\bagrees?[ \t]+to\b|\bundertakes?[ \t]+to\b"
)
_RIGHT = r"\b\w{2,}(?:ebilir|abilir)\b|\bhakk[ıi]na[ \t]+sahip\w*|\bhakk[ıi][ \t]+vard[ıi]r\b|\bmay\b|\bis[ \t]+entitled[ \t]+to\b"
_DEFINITION = (
    r"\bkapsar\b|\bkapsamaktad[ıi]r\b|\bbelgedir\b|\bbilgidir\b|\bdemektir\b|\bifade[ \t]+eder\b"
    r"|\banlam[ıi]na[ \t]+gelir\b|\bmeans\b|\bincludes?\b|\bshall[ \t]+include\b"
)
_EXCEPTION = (
    r"\bsay[ıi]lmaz\b|\bkapsam[ıi][ \t]+d[ıi]ş[ıi]nda\w*|\bkapsami[ \t]+disinda\w*|\bhariç\b|\bharic\b|\bistisna\w*"
    r"|\bkamuya[ \t]+açık\w*|\bkamuya[ \t]+acik\w*|\bbağımsız[ \t]+olarak\b|\bbagimsiz[ \t]+olarak\b"
    r"|\bshall[ \t]+not[ \t]+include\b|\bdoes[ \t]+not[ \t]+include\b|\bexcept\b|\bexclud\w*|\bpublicly[ \t]+available\b"
    r"|\bindependently[ \t]+developed\b"
)

_OBLIGATION_RE = re.compile(_OBLIGATION, _FLAGS)
_RIGHT_RE = re.compile(_RIGHT, _FLAGS)
_DEFINITION_RE = re.compile(_DEFINITION, _FLAGS)
_EXCEPTION_RE = re.compile(_EXCEPTION, _FLAGS)

# The markers are terms too: an obligation's verb, a right's "-ebilir", a
# definition's "kapsar", an exception's "sayılmaz". They are what makes a clause
# an obligation or an exception, so the semantic-preservation measure has to see
# them disappear -- counting only the topical terms let a pass that deleted
# "kullanacak", "saklıdır" and "doğacaktır" score a perfect 1.000. DEFINITION
# and EXCEPTION are marker kinds, not provision types; they type a clause only
# beside a confidentiality term (CONFIDENTIALITY_DEFINITION / _EXCEPTION).
DEFINITION_MARKER = "DEFINITION"
EXCEPTION_MARKER = "EXCEPTION"
_TERM_RES = {
    **{kind: re.compile(pattern, _FLAGS) for kind, pattern in _TERMS.items()},
    "CONTRACTUAL_OBLIGATION": _OBLIGATION_RE,
    "CONTRACTUAL_RIGHT": _RIGHT_RE,
    DEFINITION_MARKER: _DEFINITION_RE,
    EXCEPTION_MARKER: _EXCEPTION_RE,
}


@dataclass(frozen=True)
class LegalTerm:
    """One operative term: the evidence a clause is a provision of ``kind``."""

    kind: str
    start: int
    end: int
    text: str


@dataclass(frozen=True)
class Provision:
    """A legally operative clause: offsets, and the provision types it carries."""

    start: int
    end: int
    types: tuple[str, ...]
    terms: tuple[LegalTerm, ...]


# --- Clauses ---------------------------------------------------------------
#
# A clause is a line, split further at a sentence end. A period ends a sentence
# only when whitespace follows it and it does not close an abbreviation or a
# one-letter initial ("A.Ş. ile", "Ltd. Şti.", "No. 5"), so a clause carrying a
# company suffix is not cut in half and its amount stays in its penalty clause.
# A semicolon does NOT end a clause: Turkish drafting uses it to open a
# definition ("Gizli bilgi; ticari sırlar, ... belgedir"), and splitting there
# separated the defined term from the verb that makes it a definition.
_ABBREVIATIONS = frozenset(
    fold(word)
    for word in (
        "Ltd", "Şti", "Sti", "No", "Av", "Dr", "Prof", "Doç", "vb", "vs", "Sok", "Cad", "Mah",
        "Apt", "Bkz", "md", "mad", "Md", "Inc", "Co", "Mr", "Ms", "St",
    )
)
_SENTENCE_END_RE = re.compile(r"[.!?](?=\s)")
_TOKEN_WINDOW = 48


_FIELD_LINE_RE = re.compile(
    r"[ \t]*[A-ZÇĞİÖŞÜÂÎÛ][\w.'’-]*[ \t]*(?:[^\W\d_][\w.'’-]*[ \t]*){0,3}:[ \t]*\S"
)


def _ends_sentence(text: str, floor: int, mark: int) -> bool:
    """Whether the [.!?] at ``mark`` ends a sentence rather than an abbreviation."""
    if text[mark] != ".":
        return True
    # Only the token in front of the period matters. Searching back to the
    # clause start instead went quadratic on a long line of abbreviations that
    # never end a sentence.
    token = re.search(r"(\S+)$", text[max(floor, mark - _TOKEN_WINDOW):mark])
    word = token.group(1) if token else ""
    bare = word.rsplit(".", 1)[-1]
    return not (len(bare) == 1 or fold(bare) in _ABBREVIATIONS)


def _clause_spans(text: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    line_start = 0
    for line in text.splitlines(keepends=True):
        line_end = line_start + len(line.rstrip("\r\n"))
        cursor = line_start
        for match in _SENTENCE_END_RE.finditer(text, line_start, line_end):
            if not _ends_sentence(text, cursor, match.start()):
                continue
            spans.append((cursor, match.end()))
            cursor = match.end()
        if cursor < line_end:
            spans.append((cursor, line_end))
        line_start += len(line)
    return [_trim(text, start, end) for start, end in spans if text[start:end].strip()]


def _trim(text: str, start: int, end: int) -> tuple[int, int]:
    segment = text[start:end]
    return start + len(segment) - len(segment.lstrip()), end - (len(segment) - len(segment.rstrip()))


def legal_terms(text: str) -> list[LegalTerm]:
    """Every operative term in ``text``, in document order."""
    # A term never runs across a line. A penalty term wrapped at a line break
    # ("cezai\nşart", as PDF extraction delivers it) is the same term on each
    # line it touches, so each line's clause still reads as a penalty clause.
    terms = [
        LegalTerm(kind, match.start() + piece.start(), match.start() + piece.end(), piece.group())
        for kind, pattern in _TERM_RES.items()
        for match in pattern.finditer(text)
        for piece in re.finditer(r"\S(?:[^\n]*\S)?", match.group())
    ]
    return sorted(terms, key=lambda term: (term.start, term.end, term.kind))


def classify_provisions(text: str, clauses: "ClauseIndex | None" = None) -> list[Provision]:
    """The legally operative clauses of ``text``, with their provision types.

    A clause is operative when it carries an operative term, an obligation
    marker or a right marker. Two types are DERIVED rather than matched,
    because each is a confidentiality term in a particular role: a
    CONFIDENTIALITY_DEFINITION also carries a definition marker ("kapsar",
    "belgedir", "means"), a CONFIDENTIALITY_EXCEPTION an exception marker
    ("sayılmaz", "kamuya açık", "shall not include").
    """
    terms = legal_terms(text)
    term_starts = [term.start for term in terms]
    provisions: list[Provision] = []
    for start, end in (clauses.spans if clauses is not None else _clause_spans(text)):
        first = bisect.bisect_left(term_starts, start)
        last = bisect.bisect_left(term_starts, end)
        inside = tuple(term for term in terms[first:last] if term.end <= end)
        kinds = {term.kind for term in inside}
        if "CONTRACTUAL_CONFIDENTIALITY" in kinds:
            if DEFINITION_MARKER in kinds:
                kinds.add("CONFIDENTIALITY_DEFINITION")
            if EXCEPTION_MARKER in kinds:
                kinds.add("CONFIDENTIALITY_EXCEPTION")
        kinds &= set(LEGAL_PROVISION_TYPES)
        if kinds:
            ordered = tuple(kind for kind in LEGAL_PROVISION_TYPES if kind in kinds)
            provisions.append(Provision(start, end, ordered, inside))
    return provisions


def provision_summary(provisions: list[Provision]) -> dict:
    """What the privacy profile stores about a document's provisions: counts only.

    Counts, not offsets or text. The profile is parsed for every row the
    dashboard lists, a long contract has a provision in nearly every sentence,
    and the classification is deterministic -- anything that needs the clauses
    themselves recomputes them from the text with classify_provisions().
    """
    by_type: dict[str, int] = {}
    for provision in provisions:
        for kind in provision.types:
            by_type[kind] = by_type.get(kind, 0) + 1
    return {
        "clauses": len(provisions),
        "by_type": {kind: by_type[kind] for kind in LEGAL_PROVISION_TYPES if kind in by_type},
        "treatment": "preserve",
    }


class ProvisionIndex:
    """classify_provisions() output, looked up by bisection.

    The provisions are disjoint and in order, so the clause around a value is
    found in O(log n); building the start list once per document rather than
    once per amount and date is the point of the class.
    """

    def __init__(self, provisions: list[Provision]) -> None:
        self.provisions = provisions
        self._starts = [provision.start for provision in provisions]

    def types_at(self, start: int, end: int) -> frozenset[str]:
        """Provision types of the clause that contains ``[start, end)``."""
        index = bisect.bisect_right(self._starts, start) - 1
        if index >= 0 and end <= self.provisions[index].end:
            return frozenset(self.provisions[index].types)
        return frozenset()


def provision_types_at(provisions: list[Provision], start: int, end: int) -> frozenset[str]:
    """One-off lookup; a caller with many values builds a ProvisionIndex."""
    return ProvisionIndex(provisions).types_at(start, end)


# --- Value roles -----------------------------------------------------------

PENALTY_AMOUNT = "PENALTY_AMOUNT"
LIABILITY_CAP = "LIABILITY_CAP"
AMOUNT = "AMOUNT"


def amount_role(provisions: ProvisionIndex, start: int, end: int) -> str:
    """What an amount is FOR, read off the clause it sits in.

    A penalty amount and a liability cap are what a contract review needs the
    amount for, and the placeholder says so -- "[PENALTY_AMOUNT_1] cezai şart"
    still tells the reader there is a penalty and what it attaches to.
    """
    kinds = provisions.types_at(start, end)
    if "CONTRACTUAL_PENALTY" in kinds:
        return PENALTY_AMOUNT
    if "LIMITATION_OF_LIABILITY" in kinds:
        return LIABILITY_CAP
    return AMOUNT


BIRTH_DATE = "BIRTH_DATE"
SIGNATURE_DATE = "SIGNATURE_DATE"
DEADLINE = "DEADLINE"
DATE = "DATE"

# A birth-date label sits immediately in front of the date on the same line.
# "doğumlu" follows it instead ("01.02.1980 doğumlu").
_BIRTH_BEFORE_RE = re.compile(
    r"(?:doğum[ \t]+tarihi|dogum[ \t]+tarihi|d\.[ \t]*t\.|doğ\.[ \t]*tar\.|date[ \t]+of[ \t]+birth|d\.o\.b\.?|dob|born[ \t]+on)"
    r"[ \t]*[:\-]?[ \t]*$",
    _FLAGS,
)
_BIRTH_AFTER_RE = re.compile(r"^[ \t]*(?:doğumlu|dogumlu)\b", _FLAGS)
_DEADLINE_AFTER_RE = re.compile(
    r"^[ \t]*(?:tarihine[ \t]+kadar|tarihinden[ \t]+itibaren|tarihinden[ \t]+önce|tarihinden[ \t]+once"
    r"|tarihinde[ \t]+sona)",
    _FLAGS,
)
_DEADLINE_BEFORE_RE = re.compile(
    r"(?:en[ \t]+geç|en[ \t]+gec|son[ \t]+gün|son[ \t]+gun|no[ \t]+later[ \t]+than|until|by|deadline[:]?|before)[ \t]*$",
    _FLAGS,
)


def date_role(text: str, provisions: ProvisionIndex, start: int, end: int) -> str:
    """What a calendar date is in its sentence: a birth date, a signature date,
    a deadline, or just a date.

    The birth date is the one that is always sensitive and never needed for the
    legal analysis; the other three are what limitation periods, notice
    deadlines and the contract's own chronology are computed from.
    """
    line_start = text.rfind("\n", 0, start) + 1
    line_end = text.find("\n", end)
    line_end = len(text) if line_end == -1 else line_end
    before = text[max(line_start, start - 40):start]
    after = text[end:min(line_end, end + 40)]
    if _BIRTH_BEFORE_RE.search(before) or _BIRTH_AFTER_RE.search(after):
        return BIRTH_DATE
    if _DEADLINE_AFTER_RE.search(after) or _DEADLINE_BEFORE_RE.search(before):
        return DEADLINE
    if "EXECUTION" in provisions.types_at(start, end):
        return SIGNATURE_DATE
    return DATE


# --- Privilege evidence ----------------------------------------------------
#
# "gizli", "gizlilik", "ticari sır", "confidential", "trade secret" and "NDA"
# are NOT evidence of attorney-client privilege. They are contract-law
# vocabulary: every NDA is made of them. They used to be privilege triggers,
# and on the synthetic NDA they deleted clauses 2-4 as
# [PRIVILEGED_CONTENT_n_REDACTED].
#
# What remains is split in two:
#
#   * STRONG markers are privilege evidence on their own: an explicit privilege
#     label ("privileged", "attorney-client", "avukat-müvekkil"), work product,
#     litigation or defence strategy, and "müvekkil" -- the word counsel uses
#     for their own client, which puts the text on counsel's side of the
#     relationship (a memo, a letter to the client, a draft).
#   * WEAK markers only say a lawyer is NAMED: "vekil", "Av.", "avukat".
#     Party blocks, signature lines, powers of attorney and contract notice
#     clauses all name lawyers without carrying any privileged communication,
#     so these need a communication / advice / strategy word in the same clause
#     before they count.
#
# "Legal advice" and "legal opinion" ("hukuki tavsiye", "hukuki görüş") are
# corroboration, deliberately not strong markers. On their own they are most
# often a DISCLAIMER: the bundled practice petition says "Hukuki tavsiye veya
# kullanılabilir bir dilekçe değildir" -- this is not legal advice -- and as a
# strong marker that sentence became a CRITICAL privilege finding.
PRIVILEGE_STRONG_MARKERS = (
    r"müvekkil|muvekkil|attorney-client|attorney[ \t]+client|privileged"
    r"|avukat-müvekkil\w*|avukat-muvekkil\w*|work[ \t]+product"
    r"|(?:dava|savunma)[ \t]+stratejisi\w*|litigation[ \t]+strateg(?:y|ies)"
)
# Two kinds of weak marker, corroborated differently:
#
#   * COUNSEL words name a lawyer: "vekil", "Av.", "avukat". Counsel's own
#     assessment corroborates them -- "Avukat olarak kanaatimiz şudur: ... mahkum
#     edilme ihtimali yüksektir" is a legal opinion however it is worded.
#   * CLIENT words are the INFLECTED client word: "müvekkilimizin talimatı",
#     "müvekkiline verilen görüş". The trigger closes on \b, so the bare stem
#     never matched an inflected form and they were simply missed. They are weak,
#     not strong, and assessment wording does NOT corroborate them, because a
#     filed petition is made of exactly that: "müvekkilimiz aleyhine icra takibi
#     başlatmış", "müvekkilimizin haklı olduğu kanaatindeyiz". A filing is not
#     privileged; only communication, advice or strategy wording beside a client
#     word is evidence that the passage is counsel speaking TO or ABOUT the client
#     in confidence.
PRIVILEGE_COUNSEL_MARKERS = r"vekil|av\.|avukat"
PRIVILEGE_CLIENT_MARKERS = r"müvekkil\w+|muvekkil\w+"
PRIVILEGE_WEAK_MARKERS = PRIVILEGE_CLIENT_MARKERS + "|" + PRIVILEGE_COUNSEL_MARKERS

# Corroboration, by what it says the passage is, in subtype priority order.
# Matched on the raw text with both spellings written out, once per document
# (PrivilegeIndex), so a clause full of lawyer words is not rescanned per word.
_CORROBORATION_KINDS: tuple[tuple[str, str, str], ...] = (
    ("strategy", "LITIGATION_STRATEGY", r"strateji\w*|strateg\w*"),
    ("work_product", "ATTORNEY_WORK_PRODUCT", r"work[ \t]+product"),
    ("advice", "LEGAL_ADVICE",
     r"tavsiye\w*|mütalaa\w*|mutalaa\w*|hukuki[ \t]+(?:görüş|gorus)\w*|legal[ \t]+(?:advice|opinions?)"
     r"|advice|advis\w*|opinions?"),
    ("assessment", "LEGAL_ADVICE", r"kanaat\w*|ihtimal\w*|değerlendirme\w*|degerlendirme\w*"),
    ("communication", "ATTORNEY_CLIENT_COMMUNICATION",
     r"görüş\w*|gorus\w*|talimat\w*|yazışma\w*|yazisma\w*|danış\w*|danis\w*|instruct\w*|correspondence"),
    # An explicit label: corroborates any marker, and types the passage only
    # when nothing more specific does.
    ("label", "ATTORNEY_CLIENT_PRIVILEGE", r"privileged|attorney-client"),
)
_CORROBORATION_RES = {kind: re.compile(r"\b(?:" + pattern + r")\b", _FLAGS) for kind, _subtype, pattern in _CORROBORATION_KINDS}
_COUNSEL_CORROBORATION = frozenset(kind for kind, _subtype, _pattern in _CORROBORATION_KINDS)
_CLIENT_CORROBORATION = _COUNSEL_CORROBORATION - {"assessment"}
# Matched against the whole trigger: "müvekkilimiz" starts with the strong
# "müvekkil" and is still a weak marker.
_PRIVILEGE_STRONG_RE = re.compile(r"(?:" + PRIVILEGE_STRONG_MARKERS + r")", _FLAGS)
_PRIVILEGE_COUNSEL_RE = re.compile(r"(?:" + PRIVILEGE_COUNSEL_MARKERS + r")", _FLAGS)


class PrivilegeIndex:
    """Where a document's privilege corroboration sits, found once.

    ``evidence(trigger, start, end)`` answers for the clause ``[start, end)`` by
    bisection. Scanning each clause per trigger went quadratic on a long line of
    lawyer words -- 35 s for 120 KB.
    """

    def __init__(self, text: str) -> None:
        self._hits = {
            kind: [match.start() for match in pattern.finditer(text or "")]
            for kind, pattern in _CORROBORATION_RES.items()
        }

    def _present(self, kind: str, start: int, end: int) -> bool:
        hits = self._hits[kind]
        index = bisect.bisect_left(hits, start)
        return index < len(hits) and hits[index] < end

    def evidence(self, trigger: str, start: int, end: int) -> str | None:
        """The privilege subtype ``trigger`` is evidence of in ``[start, end)``, or None.

        None means "a lawyer or a client is named here and nothing in the clause
        says this is privileged communication": the privilege rule then reports
        nothing for this trigger.
        """
        present = [kind for kind, _subtype, _pattern in _CORROBORATION_KINDS if self._present(kind, start, end)]
        if not _PRIVILEGE_STRONG_RE.fullmatch(trigger):
            accepted = _COUNSEL_CORROBORATION if _PRIVILEGE_COUNSEL_RE.fullmatch(trigger) else _CLIENT_CORROBORATION
            if not any(kind in accepted for kind in present):
                return None
        subtypes = {kind: subtype for kind, subtype, _pattern in _CORROBORATION_KINDS}
        return subtypes[present[0]] if present else "ATTORNEY_CLIENT_PRIVILEGE"


SENTENCE_LOOK_BACK = 400


class ClauseIndex:
    """The clauses of one document, split once and looked up by bisection."""

    def __init__(self, text: str) -> None:
        self.spans = _clause_spans(text)
        self._starts = [span_start for span_start, _ in self.spans]

    def containing(self, start: int, end: int) -> tuple[int, int]:
        """The clause holding ``start``, stretched to ``end`` when a match runs
        on past its sentence end (a context sweep crosses an abbreviation's
        period that this splitter may still treat as one)."""
        index = bisect.bisect_right(self._starts, start) - 1
        if index >= 0 and start < self.spans[index][1]:
            return self.spans[index][0], max(self.spans[index][1], end)
        return start, end

    def sentence_start(self, text: str, position: int) -> int:
        """Where the sentence holding ``position`` starts, across wrapped lines.

        A clause ends at every line break, but a PDF's text layer (or a
        hard-wrapped .txt) breaks a sentence wherever the visual line ended.
        This walks back over earlier clauses while the one before ends without
        sentence-ending punctuation and only a single line break separates
        them; a blank line or a [.!?] that ends a sentence stops it. When the
        line holding ``position`` is itself a "Label : value" field
        ("Soruşturma No : ..."), it is its own statement, not a wrapped line of
        prose, and nothing above it is read. A label starts with a capital: a
        wrapped continuation of prose starts lowercase ("...iddiasıyla açılan\n
        soruşturma No: ...") and is still read across. Earlier lines are read
        only as far back as ``SENTENCE_LOOK_BACK`` characters before
        ``position``: a line that starts further back is clipped there, so a
        long run of unpunctuated lines costs each caller a bounded walk, not one
        back to the top of the run. The clause holding ``position`` is never
        clipped.
        """
        index = bisect.bisect_right(self._starts, position) - 1
        if index < 0 or position >= self.spans[index][1]:
            return position
        if _FIELD_LINE_RE.match(text, text.rfind("\n", 0, position) + 1):
            return self.spans[index][0]
        own_start = self.spans[index][0]
        floor = position - SENTENCE_LOOK_BACK
        while index > 0:
            previous_start, previous_end = self.spans[index - 1]
            gap = text[previous_end:self.spans[index][0]]
            if gap.count("\n") != 1:
                break
            if text[previous_end - 1] in ".!?" and _ends_sentence(text, previous_start, previous_end - 1):
                break
            if previous_start < floor:
                return min(own_start, floor)
            index -= 1
        return self.spans[index][0]


def privilege_evidence(trigger: str, clause: str) -> str | None:
    """One-off form of PrivilegeIndex.evidence for a single clause."""
    return PrivilegeIndex(clause).evidence(trigger, 0, len(clause))


# --- Defined-term aliases (coreference) ------------------------------------
#
# A Turkish contract introduces a party once by its full name and then by a
# defined short name: 'Kestane Yazılım Anonim Şirketi (bundan sonra "Kestane"
# olarak anılacaktır)'. The full name was pseudonymised everywhere and the
# short name nowhere, so "Kestane adına irtibat kişisi" shipped the company in
# cleartext beside its own [COMPANY_1].
#
# Only an alias drawn FROM the entity's own name is an identity. A defined
# ROLE ('(bundan sonra "Satıcı" olarak anılacaktır)', "the Receiving Party")
# is the legal relationship itself, carries no identity, and must stay
# readable -- pseudonymising "Satıcı" would delete exactly the information a
# contract review runs on.
# The parenthetical may wrap -- a PDF's text layer breaks "(bundan\nsonra
# "Kestane" olarak anılacaktır)" wherever the line ended, and with [ \t] here the
# synthetic NDA's PDF shipped both short names in cleartext while its DOCX did
# not. Line breaks are safe to cross because the definition is never a finding:
# only the quoted alias is, and the quote class still excludes "\n", so no
# alias sample can span a line.
_ALIAS_DEFINITION_RE = re.compile(
    r"\s*\(\s*(?:(?:bundan\s+(?:sonra|böyle|boyle)|kısaca|kisaca|hereinafter(?:\s+referred\s+to\s+as)?|the)[\s,]*)?"
    r"[\"“”'‘’«]([^\"“”'‘’»\n()]{2,40})[\"“”'‘’»]"
    r"[^)]{0,60}\)",
    _FLAGS,
)
_LEGAL_FORM_WORDS = frozenset(
    fold(word)
    for word in ("anonim", "şirketi", "sirketi", "limited", "ltd", "şti", "sti", "a.ş", "aş", "inc", "plc", "llc", "holding")
)
_ALIAS_WORD_RE = re.compile(r"[^\W\d_]+(?:['’][^\W\d_]+)?", re.UNICODE)


@dataclass(frozen=True)
class AliasMention:
    entity_start: int
    entity_end: int
    alias: str
    start: int
    end: int


# Party roles a company match can swallow in front of the name -- "Kiracı Defne
# Lojistik Limited Şirketi (bundan sonra "Kiracı" ...)" -- so that a defined
# ROLE is never mistaken for a word of the name.
_ROLE_WORDS = frozenset(
    fold(word)
    for word in (
        "kiracı", "kiraya", "veren", "satıcı", "alıcı", "davacı", "davalı", "borçlu", "alacaklı",
        "işveren", "işçi", "yüklenici", "müteahhit", "müşteri", "tedarikçi", "distribütör", "bayi",
        "lisans", "alan", "taraf", "kefil", "vekil", "müvekkil", "hissedar", "ortak", "şirket",
        "sahibi", "seller", "buyer", "lessor", "lessee", "licensor", "licensee", "supplier",
        "customer", "contractor", "employer", "employee", "party", "company", "client",
    )
)


def _name_words(name: str) -> set[str]:
    return {fold(word) for word in _ALIAS_WORD_RE.findall(name)} - _LEGAL_FORM_WORDS - _ROLE_WORDS


def defined_aliases(text: str, entity_spans: list[tuple[int, int]]) -> list[tuple[int, int, str]]:
    """``(entity_start, entity_end, alias)`` for every name-derived defined term.

    The definition must follow the entity directly ("X A.Ş. (bundan sonra
    "Y" ...)"), and every word of the alias must be a word of the entity's own
    name, legal-form words aside. That second condition is what keeps a role
    alias out.
    """
    aliases = []
    for entity_start, entity_end in entity_spans:
        match = _ALIAS_DEFINITION_RE.match(text, entity_end)
        if not match:
            continue
        alias = match.group(1).strip()
        alias_words = {fold(word) for word in _ALIAS_WORD_RE.findall(alias)}
        if not alias_words or not alias[:1].isupper():
            continue
        if alias_words <= _name_words(text[entity_start:entity_end]):
            aliases.append((entity_start, entity_end, alias))
    return aliases


def export_fold(value: str) -> str:
    """Fold ``value`` the way the reviewed export matches an approved sample.

    docx_redactor.case_insensitive_pattern matches every character against its
    case and Turkish-diacritic variants; this maps each character to one
    representative of that class instead, one character for one, so offsets in
    the folded text are offsets in ``value``. Used to ask "where would the export
    replace this sample?" WITHOUT compiling the sample into a regular
    expression: a compiled pattern leaves the sample in ``re``'s process-wide
    cache, which is why the export purges it, and detection should not put
    identifiers there in the first place.
    """
    out = []
    for char in value:
        group = TURKISH_FOLD_BY_CHAR.get(char)
        if group is not None:
            out.append(min(group))
        else:
            lower = char.lower()
            out.append(lower if len(lower) == 1 else char)
    return "".join(out)


_WORD_CHAR_RE = re.compile(r"\w", re.UNICODE)


def _is_word_char(text: str, index: int) -> bool:
    return 0 <= index < len(text) and _WORD_CHAR_RE.match(text, index) is not None


def _occurrences(haystack: str, needle: str) -> list[int]:
    starts, position = [], haystack.find(needle)
    while needle and position != -1:
        starts.append(position)
        position = haystack.find(needle, position + len(needle))
    return starts


def alias_mentions(text: str, aliases: list[tuple[int, int, str]], covered: list[tuple[int, int]]) -> list[AliasMention]:
    """Every whole-word mention of each alias that no ``covered`` span holds.

    A mention is the alias in its own capitalisation or in Turkish upper case,
    as a whole word; a lower-case "kestane" is the word chestnut.

    The reviewed export replaces an approved sample wherever it occurs, case-
    and diacritic-insensitively, widened to the whole word. Approving an alias
    therefore also replaces its COLLATERAL occurrences: places that are neither
    a mention nor inside a ``covered`` span (the company's full name, an e-mail
    address). An alias is offered unless its collateral occurrences are at least
    as many as its mentions. "Ak" (from 'Ak Enerji A.Ş. (bundan sonra "Ak"
    ...)') would replace "olarak", "anılacaktır", "kullanacak" and every other
    word containing "ak"; "Yapı" every "yapılacaktır"; "Ay" the month. Those are
    not offered, and their mentions stay as they were before short names were
    read. "Kestane" in a contract that also mentions chestnuts once is still
    offered: one ordinary word lost is the price of the company's identity, and
    refusing on any collateral at all silently dropped the coreference the
    moment the word appeared anywhere else.
    """
    first_definition: dict[str, tuple[int, int]] = {}
    for entity_start, entity_end, alias in aliases:
        first_definition.setdefault(alias, (entity_start, entity_end))
    if not first_definition:
        return []
    spans = sorted(covered)
    starts = [span_start for span_start, _ in spans]
    reach: list[int] = []
    for _span_start, span_end in spans:
        reach.append(max(span_end, reach[-1]) if reach else span_end)

    def is_covered(start: int, end: int) -> bool:
        # Some covered span starts at or before `start`; the furthest any of
        # them reaches decides containment.
        index = bisect.bisect_right(starts, start) - 1
        return index >= 0 and reach[index] >= end

    folded_text = export_fold(text)
    mentions: dict[tuple[int, int], AliasMention] = {}
    for alias, (entity_start, entity_end) in first_definition.items():
        accepted: set[tuple[int, int]] = set()
        for form in {alias, alias.replace("i", "İ").replace("ı", "I").upper()}:
            if len(form) != len(alias):
                continue
            for start in _occurrences(text, form):
                end = start + len(form)
                if _is_word_char(text, start - 1) or _is_word_char(text, end):
                    continue
                if not is_covered(start, end):
                    accepted.add((start, end))
        collateral = sum(
            1
            for span in (
                expand_to_word_bounds(text, start, start + len(alias))
                for start in _occurrences(folded_text, export_fold(alias))
            )
            if span not in accepted and not is_covered(*span)
        )
        if not accepted or collateral >= len(accepted):
            continue
        for start, end in accepted:
            mentions.setdefault((start, end), AliasMention(entity_start, entity_end, alias, start, end))
    return [mentions[key] for key in sorted(mentions)]
