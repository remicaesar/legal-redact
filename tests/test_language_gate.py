"""The product must refuse to clear a document its rules cannot read.

Measured on 40 held-out synthetic documents, Turkish entity recall was 0.934 and
English 0.246 -- English person names 0 of 76, English addresses 0 of 26, and
English phone numbers, money, dates, courts, claim numbers and bank accounts all
zero. The consequence was not a wrong answer but a confident one: an ordinary
English witness statement carrying two names, a postal address, a mobile number,
a date of birth, a national insurance number and a health disclosure produced
zero findings, residual risk "Low", ``human_review_required: False`` and all
seven gate conditions green. The gate was not broken; it had nothing to gate on.

Two things had to change together, and both are pinned here:

* ``classifier.detect_language`` read the FILENAME. A signal driving a safety
  decision was not derived from the document at all, so 18 of 20 held-out
  English documents were recorded as Turkish and the English-coverage notice
  built for exactly this case was hidden on them.
* The refusal now lives in the gate's own decision -- ``language_supported`` in
  ``external_llm_gate_policy`` and the Unknown level returned by
  ``residual_risk_assessment`` -- not in the presentation layer. A notice does
  not refuse anything.

Every document here is synthetic, and every person, address and identifier in
them is invented.
"""

import json
import sqlite3
import unittest
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

import tests.env_setup  # noqa: F401  -- sets LEGAL_ANALYZER_SECRET_KEY before app is imported
import app as app_module
from legal_analyzer.classifier import ENGLISH, TURKISH, UNKNOWN_LANGUAGE, detect_language
from legal_analyzer.privacy import (
    EXTERNAL_LLM_ALLOWED,
    analyze_privacy,
    external_llm_gate_policy,
    human_review_required,
    language_coverage,
    refresh_release_state,
)
from legal_analyzer.status import STAGE_BLOCKED, compute_pipeline_status
from legal_analyzer.taxonomy import CATEGORIES, SUBCATEGORIES
from werkzeug.security import generate_password_hash

# Section 3.6 of the accuracy report, reconstructed verbatim. This document is
# the acceptance criterion: it must not clear the gate.
WITNESS_STATEMENT = """WITNESS STATEMENT

I, Marigold Quillfeather, of 179 Cobblewell Street, London EC2A 4NE, will say as follows.

1. I was employed as a site supervisor from 4 March 2021 until 18 November 2024.
2. On the afternoon of 12 June 2024 I attended a meeting with Desmond Crowhurst and
   Rosalind Blackwood at the Harrowgate site office.
3. Desmond Crowhurst told me that the retention of 42,500 pounds would not be released.
4. My mobile number is 07700 900318 and my date of birth is 3 February 1979.
5. My national insurance number is QQ 12 34 56 C.
6. I was diagnosed with a stress-related illness by my GP in January 2025.

I believe that the facts stated in this witness statement are true.

Signed: Marigold Quillfeather
"""

# A Turkish petition of the same shape: a court, an attorney, a date and a
# checksum-valid TCKN. It is the control -- everything about it must behave
# exactly as it did before the language became a gate condition.
TURKISH_PETITION = (
    "Istanbul 5. Asliye Ceza Mahkemesi dosyasinda Av. Ayse Demir 01.01.2026 "
    "tarihli dilekce sundu. T.C. Kimlik No: 10000000146"
    " Isbu dilekce ile taraflar arasinda imzalanan sozlesme hukumleri geregi "
    "alacagin tahsili talep edilmektedir. Bu nedenle ilgili tum bolumler "
    "yukarida aciklanmistir."
)

# A non-Turkish, non-English document. Nothing in it is Turkish, but
# "übernimmt", "Gewährleistung", "Mängel", "während", "Köhler", "München" and
# "Für" carry ç/ö/ü -- letters Turkish shares with German, French and
# Portuguese -- and those letters alone once counted as Turkish evidence, which
# cleared this contract through a gate that cannot read a word of it.
GERMAN_CONTRACT = """LIEFERVERTRAG

Der Lieferant übernimmt die Gewährleistung für alle Mängel, die während der
Vertragslaufzeit auftreten. Herr Köhler mit Wohnsitz in München ist für die
Abwicklung zuständig. Für Streitigkeiten aus diesem Vertrag ist ausschließlich
das Landgericht München zuständig. Die Vergütung wird monatlich gezahlt und
enthält sämtliche Nebenkosten. Änderungen dieses Vertrages bedürfen der
Schriftform.
"""


# An English settlement agreement whose personal data sits where the rules do
# not reach: two parties named by bare surname, with no title, label or known
# given name, and a national insurance number, which no rule detects. An
# earlier version of this fixture named its parties in full, with their street
# addresses and phones, and produced zero findings only because no rule read
# English then; the English rules find all of that now, so it could no longer
# express "zero findings". The point it carries is unchanged: before the
# release checklist said so, a document like this produced "No undecided
# critical findings: clear" and "No direct identifiers remain: none remain" --
# two green ticks beside the gate's own refusal, each a statement about
# personal data the rules did not find.
ENGLISH_SETTLEMENT_AGREEMENT = """SETTLEMENT AGREEMENT

This settlement agreement is made between Quillfeather (the Claimant) and Crowhurst (the Respondent).

1. The Respondent will pay the Claimant the agreed sum within fourteen days of the date of this agreement.
2. The Claimant accepts that payment in full and final settlement of all claims arising from her work for the Respondent.
3. The national insurance number of the Claimant is QQ 12 34 56 C and must be quoted on the payment.
4. Quillfeather and Crowhurst will each bear their own costs of the dispute and of this agreement.
"""

# A monolingual Turkish supply contract with no English in it whatsoever, built
# from the ordinary Turkish words that are spelled like English function words:
# "her" (each), "on" (ten), "an" (moment), "not" (note), "at", "in", "as".
# The 19 fixtures cannot stand in for this -- measured, not one of them contains
# a single English marker, so they cannot express the collision at all.
TURKISH_WORDS_THAT_LOOK_ENGLISH = """TEDARIK SOZLESMESI

Her iki taraf isbu sozlesme hukumleri geregi asagida belirtilen yukumlulukleri
kabul eder. Satici her turlu malzemeyi on gun icinde teslim etmekle yukumludur.
Alici bedeli her ay duzenli olarak oder. Taraflar arasinda cikacak
uyusmazliklarda on is gunu icinde yazili bildirim yapilir. Teslimat sirasinda
duzenlenen not dosyaya eklenir ve her bir madde ayri ayri gecerlidir. Bu
sozlesme imzalandigi an yururluge girer, ilgili kanun hukumleri saklidir.
"""


# A monolingual Turkish share-purchase agreement. Nothing in it is English
# except a bilingual title line and a foreign bank's name -- the kind of
# incidental English an ordinary Turkish filing carries. It scores four English
# hits: "AGREEMENT" from its own title, and "The", "of", "and" from the
# counterparty and escrow names.
TURKISH_SPA_WITH_INCIDENTAL_ENGLISH = """HISSE DEVIR SOZLESMESI (SHARE PURCHASE AGREEMENT)

Isbu sozlesme asagida bilgileri yazili taraflar arasinda imzalanmistir. Satici,
sirket hisselerinin tamamini alici sirkete devretmeyi kabul ve beyan eder.
Devir bedeli, taraflarin mutabik kaldigi tarihte odenir ve ilgili kayitlara
islenir. Alici sirketin hesaplari The Bank of New York Mellon nezdinde
tutulmaktadir; emanet hesabi ise Escrow and Transfer Deed kapsaminda acilir.
Taraflar arasinda imzalanan bu sozlesme hukumleri geregi devir islemi ilgili
kanun hukumlerine gore gerceklestirilir. Sozlesmenin herhangi bir maddesinin
gecersiz olmasi diger maddelerin gecerliligini etkilemez. Isbu sozlesme
imzalandigi tarihte yururluge girer ve taraflar acisindan baglayici olur.
Ayrica devir islemine iliskin tum bildirimler yazili olarak yapilir.
"""

# A Turkish petition carrying an English witness-statement annex: the body is
# Turkish, the annex is not, and the rules cannot read the half that holds the
# names, the address, the mobile number and the date of birth.
TURKISH_PETITION_BODY = """Istanbul 5. Asliye Hukuk Mahkemesine sunulmak uzere hazirlanan isbu dilekce
ile taraflar arasinda imzalanan sozlesme hukumleri geregi alacagin tahsili
talep edilmektedir. Ilgili tum bolumler asagida aciklanmistir.
"""


# A short Turkish cover note with an English schedule attached. Its English
# marker count stays under the absolute cap, so only the share ceiling can
# refuse it -- the mirror of the padded annex below, which only the cap can
# refuse. Neither ceiling covers both documents.
TURKISH_NOTE_WITH_ENGLISH_SCHEDULE = """SOZLESME EKI

Isbu ek, taraflar arasinda imzalanan sozlesme hukumleri geregi duzenlenmistir
ve ilgili kayitlara islenmistir.

SCHEDULE 2 - NOTICES

Any notice given under this schedule shall be delivered to the registered
office of the counterparty.
"""

# A privileged English annex: the same witness material, headed by the phrase
# the `privileged_or_confidential` rule sweeps 160 characters from. That sweep
# is what makes this the red-proof for the asymmetry in `_marker_evidence` --
# one detected span blankets the annex's English prose, so counting English
# only outside detected entities drops 14 of its 17 markers and collapses the
# clustered run from 14 to 2, under the ceiling of 5.
TURKISH_FILING_PRIVILEGED_ENGLISH_ANNEX = """EK-3 / ANNEX 3

PRIVILEGED AND CONFIDENTIAL - this is the statement of the claimant and it has been prepared for the purpose of these proceedings by her solicitors

Marigold Quillfeather
179 Cobblewell Street, London EC2A 4NE
Mobile 07700 900318
Date of birth 3 February 1979
National insurance number QQ 12 34 56 C
Second witness Desmond Crowhurst
"""


# One ordinary Turkish contract clause with no personal data in it. Repeating it
# is how a Turkish body is made long enough to drive the English share of a
# bilingual document below the ceiling, which is the shape that defeated a
# share-only rule.
TURKISH_BOILERPLATE_CLAUSE = (
    "Isbu sozlesme hukumleri geregi taraflar arasinda imzalanan bu belge ilgili "
    "kanun hukumlerine gore duzenlenmistir ve asagida aciklanan tum bolumler "
    "baglayicidir. "
)


# An ordinary Turkish party roster typed in ASCII, as an extracted XLSX sheet or
# an ID-card transcription arrives. Every person and id in it is invented. It is
# Turkish in content and almost free of function words, so the language signal
# cannot read it and REFUSES it -- a documented limitation, not a leak. See
# `ATurkishDocumentWithoutFunctionWordsIsRefusedTests`.
TURKISH_ROSTER_TYPED_IN_ASCII = """TARAF LISTESI
Ahmet Yilmaz 10000000146 Kadikoy Istanbul
Ayse Demir 10000000146 Besiktas Istanbul
Mustafa Sahin 10000000146 Cankaya Ankara
Fatma Kaya 10000000146 Konak Izmir
Mehmet Dogan 10000000146 Nilufer Bursa
Zeynep Arslan 10000000146 Selcuklu Konya
"""


# An English witness-details form that happens to name Turkish people. Every
# person, address and identifier in it is invented. Its only Turkish content is
# inside the names -- which is the point: a name is not a language.
ENGLISH_FORM_NAMING_TURKISH_PARTIES = """WITNESS DETAILS SHEET
Full name: Ayşe Yılmaz
Address line: 179 Cobblewell Street, London EC2A 4NE
Mobile: 07700 900318
National insurance number: QQ 12 34 56 C
Diagnosis: stress-related illness
Second witness: Desmond Crowhurst
Supervisor: Mehmet Şahin
Case handler: Elif Işık
"""

# The mirror: a Turkish party roster whose Turkish letters sit in PLACE names
# and Turkish headings as well as in person names. It must keep reading as
# Turkish, or the fix above has thrown out more than it was aimed at.
TURKISH_ROSTER_WITH_DIACRITICS = """TARAF LİSTESİ
Ahmet Yılmaz 10000000146 Kadıköy İstanbul
Ayşe Demir 10000000146 Beşiktaş İstanbul
Mustafa Şahin 10000000146 Üsküdar İstanbul
Fatma Kaya 10000000146 Kadıköy İstanbul
Mehmet Doğan 10000000146 Beşiktaş İstanbul
Zeynep Arslan 10000000146 Üsküdar İstanbul
"""


# A wholly English witness form that mentions three Turkish PLACES. Nothing in
# it is a Turkish sentence; every person, address and identifier is invented.
# Its Turkish letters are in Şişli, İstanbul, Kadıköy and Üsküdar -- proper
# nouns, not language.
ENGLISH_FORM_NAMING_TURKISH_PLACES = """Witness details form
Full name: Marigold Quillfeather
Occupation: site supervisor
Employer office: Şişli, İstanbul
Correspondence: 179 Cobblewell Street, London EC2A 4NE
Mobile: 07700 900318
Date of birth: 3 May 1981
National insurance number: QQ 12 34 56 C
Second witness: Desmond Crowhurst
Meeting place: Kadıköy
Branch office: Üsküdar
Reference: WS-4482
"""


# A wholly English employee data sheet whose only Turkish is the employer's
# legal-form name, written twice. Every person, address and identifier in it is
# invented. "Sanayi ve Ticaret Anonim Sirketi" contributes two marker words per
# mention, which is how a document with no Turkish sentence in it cleared the
# evidence floor.
ENGLISH_SHEET_NAMING_A_TURKISH_COMPANY = """Employee data sheet
Full name: Marigold Quillfeather
Job title: site supervisor
Employer: Demir Tekstil Sanayi ve Ticaret Anonim Sirketi
Home address: 179 Cobblewell Street, London EC2A 4NE
Mobile: 07700 900318
Date of birth: 3 May 1981
National insurance number: QQ 12 34 56 C
Emergency contact: Desmond Crowhurst
Payroll provider: Demir Tekstil Sanayi ve Ticaret Anonim Sirketi
Health note: stress-related illness, certified
Reference: HR-4482
"""


def english_marker_hits(text: str) -> list[str]:
    """The English tokens `detect_language` actually counts in `text`."""
    from legal_analyzer.classifier import _ENGLISH_MARKERS, _WORD_RE

    return [word for word in _WORD_RE.findall(text) if word.lower() in _ENGLISH_MARKERS]


def marker_evidence(text: str) -> tuple[int, int, int]:
    """(Turkish hits, English hits, longest clustered English run) for `text`.

    Measured the way the product measures it: with the detector's own entity
    spans excluded. Calling the classifier without them counts Turkish words
    inside entity names and so reports a MORE permissive answer than anything
    shipping ever sees.
    """
    from legal_analyzer.classifier import _marker_evidence

    return _marker_evidence(text, detected_entity_spans(text))


def detected_entity_spans(text: str) -> list[tuple[int, int]]:
    """The spans `analyze_privacy` hands to `detect_language` for this text."""
    from legal_analyzer.privacy import language_evidence_spans

    return language_evidence_spans(text, analyze_privacy("fixture.txt", text)["risk_map"])


def detected_language(text: str) -> str:
    """`detect_language` as the product calls it -- with entity spans."""
    return detect_language(text, detected_entity_spans(text))


def turkish_corpus() -> str:
    """The 19 synthetic Turkish fixtures, joined -- one sizeable Turkish filing."""
    fixtures = sorted(Path("tests/fixtures").glob("*.txt"))
    assert fixtures, "fixture corpus is missing"
    return "\n\n".join(fixture.read_text(encoding="utf-8") for fixture in fixtures)


LANGUAGE_CONDITION = (
    "The detection rules are written for Turkish; coverage outside Turkish is "
    "materially lower, so this document cannot be cleared automatically."
)


NOT_ASSESSABLE = "not assessable — language not covered"
CRITICAL_CONDITION = "Critical findings must be zero."
DIRECT_IDENTIFIER_CONDITION = "No direct identifiers may remain in detected findings."
CRITICAL_NOT_ASSESSABLE = "Critical findings cannot be assessed: document language is not covered."
DIRECT_IDENTIFIER_NOT_ASSESSABLE = "Direct identifiers cannot be assessed: document language is not covered."


class DetectLanguageReadsTheDocumentTests(unittest.TestCase):
    """The signal comes from the text. The filename cannot reach it."""

    def test_the_english_witness_statement_reads_as_english(self) -> None:
        self.assertEqual(detect_language(WITNESS_STATEMENT), ENGLISH)

    def test_the_turkish_petition_reads_as_turkish(self) -> None:
        self.assertEqual(detect_language(TURKISH_PETITION), TURKISH)

    def test_turkish_typed_without_diacritics_still_reads_as_turkish(self) -> None:
        """Turkish legal text is routinely typed in ASCII; the function words carry it."""
        self.assertNotIn("ş", TURKISH_PETITION)
        self.assertEqual(detect_language(TURKISH_PETITION), TURKISH)

    def test_a_turkish_filename_cannot_make_an_english_document_turkish(self) -> None:
        """The defect, stated directly: the old signal was the filename.

        Every one of these names would have produced "TR" from the filename
        rule, including by falling through to its default.
        """
        for filename in ("dilekce.docx", "sozlesme_2026.docx", "witness_statement.docx"):
            with self.subTest(filename=filename):
                profile = analyze_privacy(filename, WITNESS_STATEMENT)
                self.assertEqual(profile["language_coverage"]["language"], ENGLISH)
                self.assertFalse(profile["language_coverage"]["supported"])

    def test_an_english_filename_cannot_make_a_turkish_document_english(self) -> None:
        profile = analyze_privacy("Consulting Agreement ENG.docx", TURKISH_PETITION)
        self.assertEqual(profile["language_coverage"]["language"], TURKISH)
        self.assertTrue(profile["language_coverage"]["supported"])

    def test_silence_is_not_turkish(self) -> None:
        """Empty, whitespace-only and fragment texts are Unknown, never TR."""
        for text in ("", "   \n\t ", "Dilekce", "Sozlesme taslagi", "WITNESS STATEMENT"):
            with self.subTest(text=text):
                self.assertEqual(detect_language(text), UNKNOWN_LANGUAGE)

    def test_a_long_document_with_no_marker_evidence_is_also_unknown(self) -> None:
        """The other way a document is unidentifiable: long enough, but blank.

        The short-document guard and this one are separate returns, and a
        document of invented proper nouns -- a bare exhibit index, a name list,
        an OCR pass that recovered only fragments -- clears the word count while
        saying nothing about its language. It must not fall through to Turkish.
        """
        nonsense = (
            "Zyrtha Quolem Prandik Vosmir Talune Krevak Simond Perlua Vantok Mirena "
            "Solbak Trevin Okalur Pentiv Yarmok Belune Croxan Dilvern Naszo Prilok "
            "Vantek Morliq Sendra Quilom Torvash Belquin Rasmoth Velkir"
        )
        self.assertGreaterEqual(len(nonsense.split()), 25, "fixture must clear the word-count guard")
        self.assertEqual(detect_language(nonsense), UNKNOWN_LANGUAGE)

    # The five gold fixtures the signal cannot read, measured. Their Turkish is
    # largely inside entity names -- a court, a company, a party -- which is
    # evidence of an entity and not of the language, so it does not count.
    # Naming them here rather than loosening the floor is the point: this is
    # the documented short/name-heavy limitation, and a SIXTH falling is a
    # failure rather than a drift.
    FIXTURES_WITHOUT_ENOUGH_TURKISH_WORDS = {
        "criminal_investigation.txt",
        "medical_case.txt",
        "ocr_scan_noisy.txt",
        "power_of_attorney.txt",
        "sample_commercial_case.txt",
    }

    def test_every_turkish_gold_fixture_is_identified_as_turkish(self) -> None:
        """Calibration, at corpus scale: the control must not drift.

        Measured through `detected_language`, which passes the entity spans the
        product passes. Calling the classifier bare would count entity-internal
        markers and report a corpus that clears more widely than anything
        shipping does -- a calibration test that cannot see the cost is worse
        than none.
        """
        fixtures = sorted(Path("tests/fixtures").glob("*.txt"))
        self.assertGreaterEqual(len(fixtures), 10, "fixture corpus is missing")
        refused = set()
        for fixture in fixtures:
            if detected_language(fixture.read_text(encoding="utf-8")) != TURKISH:
                refused.add(fixture.name)
        self.assertEqual(refused, self.FIXTURES_WITHOUT_ENOUGH_TURKISH_WORDS)

    def test_the_refused_fixtures_are_refused_for_want_of_turkish_words(self) -> None:
        """Not a leak: they are held back, and the reason is the documented one."""
        for name in sorted(self.FIXTURES_WITHOUT_ENOUGH_TURKISH_WORDS):
            with self.subTest(fixture=name):
                text = (Path("tests/fixtures") / name).read_text(encoding="utf-8")
                turkish, english, _ = marker_evidence(text)
                self.assertLess(turkish, 3, "it must fall on the Turkish floor, not on English")
                self.assertEqual(english, 0)
                profile = analyze_privacy(name, text)
                self.assertFalse(profile["language_coverage"]["supported"])
                self.assertIn(LANGUAGE_CONDITION, profile["external_llm_gate"]["failed_conditions"])

    def test_this_calibration_has_no_headroom(self) -> None:
        """Three clearing fixtures sit EXACTLY on the floor, and that is the record.

        There were four. `sample_pleading.txt` left the floor (3 -> 4 Turkish
        markers) when the prosecutor's office stopped carrying the generic
        court rule's 120-character sweep: "'na sunulmak üzere hazırlanmıştır"
        was inside that finding, so its marker word was excluded as entity text.
        It moved away from the refusal, not toward it.

        Not an assertion that the numbers are good -- an assertion that they
        are known. The next change to this signal needs re-measurement rather
        than judgement, and this is what makes a one-hit drop visible instead
        of silent.
        """
        from legal_analyzer.classifier import _MIN_SIGNAL

        at_the_floor = {
            fixture.name
            for fixture in sorted(Path("tests/fixtures").glob("*.txt"))
            if marker_evidence(fixture.read_text(encoding="utf-8"))[0] == _MIN_SIGNAL
        }
        self.assertEqual(
            at_the_floor,
            {
                "court_judgment.txt",
                "employment_dispute.txt",
                "lease_agreement.txt",
            },
        )


class OrdinaryTurkishIsNeverEnglishEvidenceTests(unittest.TestCase):
    """The marker lists must not overlap, or the refusal lands on Turkish itself.

    "her", "on", "not", "an", "at", "in", "as" and "it" are ordinary Turkish
    words and were also listed as English evidence. Since Turkish is claimed
    only when the English count stays below the evidence floor, a monolingual
    Turkish contract using "her iki taraf", "her türlü", "on gün" and "on iş
    günü" scored six English hits, came back unidentifiable, and was refused --
    the non-Turkish refusal applied to the one language the rules are for. A
    safeguard that blocks ordinary Turkish filings is the one that gets
    switched off.
    """

    def test_a_monolingual_turkish_contract_reads_as_turkish(self) -> None:
        self.assertEqual(detect_language(TURKISH_WORDS_THAT_LOOK_ENGLISH), TURKISH)

    def test_it_is_measured_and_clears_the_language_condition(self) -> None:
        profile = analyze_privacy("tedarik.docx", TURKISH_WORDS_THAT_LOOK_ENGLISH)
        self.assertTrue(profile["language_coverage"]["supported"])
        self.assertNotEqual(profile["residual_risk"]["level"], "Unknown")
        self.assertNotIn(LANGUAGE_CONDITION, profile["external_llm_gate"]["failed_conditions"])

    def test_the_fixture_corpus_cannot_stand_in_for_this(self) -> None:
        """Why the case above had to be written: the calibration set is silent.

        `test_every_turkish_gold_fixture_is_identified_as_turkish` is necessary
        and stays, but not one of the 19 fixtures contains an English marker,
        so it cannot fail when a Turkish word is listed as English evidence.
        """
        from legal_analyzer.classifier import _ENGLISH_MARKERS, _WORD_RE

        for fixture in sorted(Path("tests/fixtures").glob("*.txt")):
            hits = [w for w in _WORD_RE.findall(fixture.read_text(encoding="utf-8")) if w.lower() in _ENGLISH_MARKERS]
            self.assertEqual(hits, [], fixture.name)

        contract_hits = [
            w for w in _WORD_RE.findall(TURKISH_WORDS_THAT_LOOK_ENGLISH)
            if w.lower() in {"her", "on", "not", "an", "at", "in", "as", "it", "may"}
        ]
        self.assertGreaterEqual(len(contract_hits), 6, "this fixture must exercise the collision")


class IncidentalEnglishDoesNotRefuseATurkishFilingTests(unittest.TestCase):
    """Scattered English never refuses a Turkish filing, at any length.

    The case is a monolingual Turkish share-purchase agreement whose only
    English is its own bilingual title line and the names of a foreign bank and
    an escrow deed. Every quantity-based ceiling tried here refused it once the
    document got long enough: at a fixed English density, an absolute cap of
    ten cleared it at 100 words and refused it at 300 -- residual risk Unknown,
    the language condition failed, and a studio notice reading "could not be
    identified from its text" on an ordinary Turkish filing.

    Length invariance is the property being bought, so it is asserted at five
    lengths. A single-size test cannot express it; that is exactly how the cap
    shipped.
    """

    LENGTHS = (1, 2, 3, 5, 10)

    def test_the_document_carries_scattered_english_at_every_length(self) -> None:
        """Otherwise the cases below prove nothing about what is being measured.

        The English has to be present and has to stay scattered: if a repetition
        ever chained its markers into a run this fixture would be testing the
        refusal path instead.
        """
        for factor in self.LENGTHS:
            with self.subTest(factor=factor):
                turkish, english, run = marker_evidence(TURKISH_SPA_WITH_INCIDENTAL_ENGLISH * factor)
                self.assertEqual(english, 4 * factor, "the fixture must carry English at every length")
                self.assertGreater(turkish, 0)
                self.assertLess(run, 5)

    def test_the_clustered_run_does_not_grow_with_the_document(self) -> None:
        """A 1000-word filing must measure exactly what the 100-word one does."""
        runs = {marker_evidence(TURKISH_SPA_WITH_INCIDENTAL_ENGLISH * f)[2] for f in self.LENGTHS}
        self.assertEqual(len(runs), 1, runs)

    def test_it_reads_as_turkish_at_every_length(self) -> None:
        for factor in self.LENGTHS:
            with self.subTest(factor=factor):
                self.assertEqual(detect_language(TURKISH_SPA_WITH_INCIDENTAL_ENGLISH * factor), TURKISH)

    def test_it_is_measured_rather_than_refused_at_every_length(self) -> None:
        for factor in self.LENGTHS:
            with self.subTest(factor=factor):
                profile = analyze_privacy("hisse_devri.docx", TURKISH_SPA_WITH_INCIDENTAL_ENGLISH * factor)
                self.assertTrue(profile["language_coverage"]["supported"])
                self.assertNotEqual(profile["residual_risk"]["level"], "Unknown")
                self.assertNotIn(LANGUAGE_CONDITION, profile["external_llm_gate"]["failed_conditions"])
                self.assertNotIn("written for Turkish", profile["external_llm_readiness"])

    def test_a_fully_reviewed_one_still_reaches_allowed_at_every_length(self) -> None:
        """The whole consequence: the gate opens on it, as it did before."""
        for factor in self.LENGTHS:
            with self.subTest(factor=factor):
                refreshed = refresh_release_state(
                    analyze_privacy("hisse_devri.docx", TURKISH_SPA_WITH_INCIDENTAL_ENGLISH * factor),
                    redaction_completed=True,
                    human_review_approved=True,
                    unresolved_critical_count=0,
                    direct_identifiers_remaining=False,
                    remaining_findings=[],
                )
                self.assertTrue(refreshed["external_llm_gate"]["allowed"], refreshed["external_llm_gate"]["failed_conditions"])
                self.assertEqual(refreshed["external_llm_readiness"], EXTERNAL_LLM_ALLOWED)


class ATurkishDocumentWithoutFunctionWordsIsRefusedTests(unittest.TestCase):
    """The documented limitation, pinned so it cannot be mistaken for a leak.

    The signal reads function words, and a roster, an enforcement schedule or an
    extracted XLSX sheet barely has any -- so an ASCII-typed Turkish roster is
    refused as unidentifiable even though the Turkish rules read it perfectly.
    Measured: eleven findings, gazetteer names plus checksum-valid national ids,
    and still refused.

    A previous round topped the Turkish floor up from those same findings when
    no English function word was present. That was reverted, because an English
    form has no English function words either: an English employee data sheet
    listing Turkish parties satisfied the condition and reached "Allowed for
    external LLM use" -- the false clearance this whole change exists to
    remove. `test_an_english_form_listing_turkish_parties_is_refused` is the
    document that proved it.

    Refusing costs a manual review. Clearing sends unredacted personal data off
    the machine. This class asserts the product stays on the first side.
    """

    def test_the_turkish_rules_read_the_roster(self) -> None:
        """The premise: the refusal is not for want of detection."""
        findings = analyze_privacy("taraf_listesi.txt", TURKISH_ROSTER_TYPED_IN_ASCII)["risk_map"]
        categories = {finding["category"] for finding in findings}
        self.assertIn("natural_person_name", categories)
        self.assertIn("turkish_national_id", categories)

    def test_the_roster_is_refused_anyway(self) -> None:
        self.assertEqual(detect_language(TURKISH_ROSTER_TYPED_IN_ASCII), UNKNOWN_LANGUAGE)
        profile = analyze_privacy("taraf_listesi.txt", TURKISH_ROSTER_TYPED_IN_ASCII)
        self.assertFalse(profile["language_coverage"]["supported"])
        self.assertEqual(profile["residual_risk"]["level"], "Unknown")
        self.assertIn(LANGUAGE_CONDITION, profile["external_llm_gate"]["failed_conditions"])

    def test_an_english_form_listing_turkish_parties_is_refused(self) -> None:
        """Why the top-up had to go, as a document rather than as an argument.

        Labelled-field English carries no English function words, so any rule
        that keys on their absence is open exactly here -- on the shape that
        holds the most personal data per word. This sheet reached `allowed:
        True` and "Allowed for external LLM use after completed redaction and
        approval" with a full review pass behind it.
        """
        sheet = """EMPLOYEE DATA SHEET
Full name: Ahmet Yilmaz
Identity: 10000000146
Residence: Kadikoy Istanbul
Mobile: 0522 111 22 33
Full name: Ayse Demir
Identity: 10000000146
Residence: Besiktas Istanbul
Diagnosis: chronic back pain
Previous employer: Harrowgate Construction
Reference: HQ 2024 001234
Case handler: Marigold Quillfeather
Contact: 179 Cobblewell Street, London EC2A 4NE
"""
        self.assertEqual(english_marker_hits(sheet), [], "the fixture must carry no English function words")
        self.assertNotEqual(detect_language(sheet), TURKISH)

        refreshed = refresh_release_state(
            analyze_privacy("data_sheet.docx", sheet),
            redaction_completed=True,
            human_review_approved=True,
            unresolved_critical_count=0,
            direct_identifiers_remaining=False,
            remaining_findings=[],
        )
        self.assertFalse(refreshed["external_llm_gate"]["allowed"])
        self.assertIn(LANGUAGE_CONDITION, refreshed["external_llm_gate"]["failed_conditions"])
        self.assertNotEqual(refreshed["external_llm_readiness"], EXTERNAL_LLM_ALLOWED)


class AProperNounIsNotALanguageTests(unittest.TestCase):
    """A Turkish NAME, PLACE or COMPANY is not evidence of the language.

    The same defect arrived three times through the same door. A Turkish
    person's name supplies ğ/ı/ş/İ/Ş, so an English witness-details form naming
    three Turkish people read as TR -- coverage supported, the notice hidden on
    exactly the document that needed it, and after an ordinary review pass,
    `allowed: True` and "Allowed for external LLM use" over a London address, a
    UK mobile, a national insurance number and a health disclosure. Excluding
    person-name spans left PLACE names open. Removing the letter path entirely
    left COMPANY names open, because a Turkish legal form is literally made of
    marker words ("… Sanayi **ve** Ticaret Anonim **Sirketi**").

    So Turkish evidence is now a Turkish word OUTSIDE any detected entity. The
    tests below cover all three routes and the Turkish documents it costs.
    """

    def test_an_english_sheet_naming_a_turkish_company_is_refused(self) -> None:
        """The third route, and the one that outlived the letter path.

        Measured before the fix: `_marker_evidence` returned (4, 2, 1) -- all
        four Turkish hits inside the two copies of the employer's name, and the
        labelled-field shape keeping the English run at 1 -- so this wholly
        English sheet read as TURKISH with coverage supported. The whole
        consequence is asserted after a full review pass because that is the
        path it cleared on.
        """
        self.assertEqual(
            marker_evidence(ENGLISH_SHEET_NAMING_A_TURKISH_COMPANY)[0],
            0,
            "marker words inside a company name are evidence of a company, not a language",
        )
        self.assertNotEqual(detected_language(ENGLISH_SHEET_NAMING_A_TURKISH_COMPANY), TURKISH)

        profile = analyze_privacy("sheet.docx", ENGLISH_SHEET_NAMING_A_TURKISH_COMPANY)
        self.assertFalse(profile["language_coverage"]["supported"])
        self.assertIn(LANGUAGE_CONDITION, profile["external_llm_gate"]["failed_conditions"])

        refreshed = refresh_release_state(
            profile,
            redaction_completed=True,
            human_review_approved=True,
            unresolved_critical_count=0,
            direct_identifiers_remaining=False,
            remaining_findings=[],
        )
        self.assertNotEqual(refreshed["post_review_residual_risk"]["level"], "Low")
        self.assertFalse(refreshed["external_llm_gate"]["allowed"])
        self.assertNotEqual(refreshed["external_llm_readiness"], EXTERNAL_LLM_ALLOWED)
        self.assertTrue(refreshed["human_review_required"])

    def test_an_english_form_naming_turkish_places_is_refused(self) -> None:
        """The half the person-name exclusion left open.

        Measured before the fix: `_marker_evidence` returned (3, 4, 2) -- the
        three Turkish hits being Şişli, İstanbul and Kadıköy, and the
        labelled-field shape keeping the English run under the ceiling -- so
        this wholly English form read as TURKISH, coverage supported, with ZERO
        findings and residual risk Low. The whole consequence is asserted here
        because that is the path it cleared on.
        """
        turkish, _, _ = marker_evidence(ENGLISH_FORM_NAMING_TURKISH_PLACES)
        self.assertEqual(turkish, 0, "a place name is not Turkish language evidence")
        self.assertNotEqual(detect_language(ENGLISH_FORM_NAMING_TURKISH_PLACES), TURKISH)

        profile = analyze_privacy("witness_form.docx", ENGLISH_FORM_NAMING_TURKISH_PLACES)
        self.assertFalse(profile["language_coverage"]["supported"])
        self.assertIn(LANGUAGE_CONDITION, profile["external_llm_gate"]["failed_conditions"])

        refreshed = refresh_release_state(
            profile,
            redaction_completed=True,
            human_review_approved=True,
            unresolved_critical_count=0,
            direct_identifiers_remaining=False,
            remaining_findings=[],
        )
        self.assertNotEqual(refreshed["post_review_residual_risk"]["level"], "Low")
        self.assertFalse(refreshed["external_llm_gate"]["allowed"])
        self.assertNotEqual(refreshed["external_llm_readiness"], EXTERNAL_LLM_ALLOWED)
        self.assertTrue(refreshed["human_review_required"])

    def test_the_form_carries_no_turkish_outside_its_names(self) -> None:
        """Otherwise the refusal below could be for some other reason."""
        turkish, _, _ = marker_evidence(ENGLISH_FORM_NAMING_TURKISH_PARTIES)
        self.assertEqual(turkish, 0, "no Turkish language evidence should survive the names")

    def test_it_does_not_read_as_turkish(self) -> None:
        self.assertNotEqual(detect_language(ENGLISH_FORM_NAMING_TURKISH_PARTIES), TURKISH)

    def test_more_turkish_names_do_not_buy_it_coverage(self) -> None:
        """The fix is not "needs more names" -- naming seven still refuses."""
        with_more_names = ENGLISH_FORM_NAMING_TURKISH_PARTIES + (
            "Auditor: Zeynep Arslan\nAdjuster: Mustafa Doğan\n"
            "Referee: Fatma Kılıç\nClerk: Hüseyin Çetin\n"
        )
        self.assertNotEqual(detect_language(with_more_names), TURKISH)

    def test_the_whole_consequence_is_refusal(self) -> None:
        """Asserted after a full review pass, because that is how it cleared."""
        profile = analyze_privacy("witness_details.docx", ENGLISH_FORM_NAMING_TURKISH_PARTIES)
        self.assertFalse(profile["language_coverage"]["supported"])
        self.assertIn(LANGUAGE_CONDITION, profile["external_llm_gate"]["failed_conditions"])

        refreshed = refresh_release_state(
            profile,
            redaction_completed=True,
            human_review_approved=True,
            unresolved_critical_count=0,
            direct_identifiers_remaining=False,
            remaining_findings=[],
        )
        self.assertNotEqual(refreshed["post_review_residual_risk"]["level"], "Low")
        self.assertFalse(refreshed["external_llm_gate"]["allowed"])
        self.assertNotEqual(refreshed["external_llm_readiness"], EXTERNAL_LLM_ALLOWED)
        self.assertIn(LANGUAGE_CONDITION, refreshed["external_llm_gate"]["failed_conditions"])

    def test_a_turkish_roster_of_names_and_places_is_refused_too(self) -> None:
        """The cost of the rule, asserted rather than assumed.

        This roster was pinned as must-clear for several rounds. It was never
        clearing because it is Turkish: it carries ONE Turkish marker word and
        nine Turkish-spelled proper nouns, so it was clearing through the same
        defect as the English form above, in larger quantity. It is refused
        now, which is the documented short/name-heavy limitation and a refusal
        rather than a leak.
        """
        turkish, english, _ = marker_evidence(TURKISH_ROSTER_WITH_DIACRITICS)
        self.assertLess(turkish, 3, "its Turkish is in the proper nouns, not in its words")
        self.assertEqual(english, 0)

        self.assertNotEqual(detect_language(TURKISH_ROSTER_WITH_DIACRITICS), TURKISH)
        profile = analyze_privacy("taraf_listesi.docx", TURKISH_ROSTER_WITH_DIACRITICS)
        self.assertFalse(profile["language_coverage"]["supported"])
        self.assertIn(LANGUAGE_CONDITION, profile["external_llm_gate"]["failed_conditions"])


class ALanguageTheRulesCannotReadIsNotTurkishTests(unittest.TestCase):
    """A document with no Turkish words in it is not claimed as Turkish.

    The German supply contract is the plain case: it carries ç, ö and ü, which
    a letter-based signal once read as Turkish -- residual risk came back as a
    measured level, `language_supported` passed, and the document could clear
    the gate on rules that match nothing in it. Turkish is claimed on marker
    words now, and this contract has none, so it is refused for the ordinary
    reason rather than by a special case.
    """

    def test_a_german_contract_does_not_read_as_turkish(self) -> None:
        self.assertEqual(detect_language(GERMAN_CONTRACT), UNKNOWN_LANGUAGE)

    def test_the_german_contract_is_refused_rather_than_measured(self) -> None:
        profile = analyze_privacy("liefervertrag.docx", GERMAN_CONTRACT)
        self.assertFalse(profile["language_coverage"]["supported"])
        self.assertEqual(profile["residual_risk"]["level"], "Unknown")
        self.assertIn(LANGUAGE_CONDITION, profile["external_llm_gate"]["failed_conditions"])

    def test_a_fully_reviewed_german_contract_still_cannot_clear(self) -> None:
        refreshed = refresh_release_state(
            analyze_privacy("liefervertrag.docx", GERMAN_CONTRACT),
            redaction_completed=True,
            human_review_approved=True,
            unresolved_critical_count=0,
            direct_identifiers_remaining=False,
            remaining_findings=[],
        )
        self.assertFalse(refreshed["external_llm_gate"]["allowed"])
        self.assertIn(LANGUAGE_CONDITION, refreshed["external_llm_gate"]["failed_conditions"])


class TurkishCarryingEnglishIsNotClearedTests(unittest.TestCase):
    """An English annex inside a Turkish filing is not covered, and is not cleared.

    English exhibits are ordinary in Turkish legal filings, and the Turkish body
    is usually the larger half -- so the whole document was measured by rules
    with 0.246 recall on English and 0 of 76 on English person names, and the
    annex's two names, postal address, mobile number, date of birth and
    national insurance number produced no findings at all.

    The Turkish body is varied from one clause to twenty-eight because every
    quantity-based rule this replaced was defeated by lengthening it: a share of
    the marker evidence collapsed as the body grew, and at nine clauses the same
    annex came back Turkish with residual risk Low and the gate allowed. A
    clustered run cannot be diluted that way, and asserting it at one body
    length could not tell the two rules apart.
    """

    BODY_LENGTHS = (1, 9, 28)

    def bilingual(self, clauses: int = 9) -> str:
        return (TURKISH_BOILERPLATE_CLAUSE * clauses) + "\n\nEK-3 / ANNEX 3\n\n" + WITNESS_STATEMENT

    def test_padding_the_turkish_body_cannot_dilute_the_annex(self) -> None:
        """The property, stated directly: the measure does not move."""
        runs = {marker_evidence(self.bilingual(n))[2] for n in self.BODY_LENGTHS}
        self.assertEqual(len(runs), 1, runs)
        self.assertGreaterEqual(runs.pop(), 5)

    def test_the_turkish_body_on_its_own_is_covered(self) -> None:
        """Otherwise the refusals below could be the body's fault, not the annex's."""
        for clauses in self.BODY_LENGTHS:
            with self.subTest(clauses=clauses):
                self.assertEqual(detect_language(TURKISH_BOILERPLATE_CLAUSE * max(clauses, 2)), TURKISH)

    def test_the_turkish_half_does_not_speak_for_the_english_half(self) -> None:
        for clauses in self.BODY_LENGTHS:
            with self.subTest(clauses=clauses):
                self.assertNotEqual(detected_language(self.bilingual(clauses)), TURKISH)

    def test_the_bilingual_filing_is_refused_rather_than_measured(self) -> None:
        for clauses in self.BODY_LENGTHS:
            with self.subTest(clauses=clauses):
                profile = analyze_privacy("dilekce_ek3.docx", self.bilingual(clauses))
                self.assertFalse(profile["language_coverage"]["supported"])
                self.assertEqual(profile["residual_risk"]["level"], "Unknown")
                self.assertIn(LANGUAGE_CONDITION, profile["external_llm_gate"]["failed_conditions"])
                self.assertFalse(profile["external_llm_gate"]["allowed"])
                self.assertNotEqual(profile["external_llm_readiness"], EXTERNAL_LLM_ALLOWED)

    def test_a_fully_reviewed_bilingual_filing_still_cannot_clear(self) -> None:
        for clauses in self.BODY_LENGTHS:
            with self.subTest(clauses=clauses):
                refreshed = refresh_release_state(
                    analyze_privacy("dilekce_ek3.docx", self.bilingual(clauses)),
                    redaction_completed=True,
                    human_review_approved=True,
                    unresolved_critical_count=0,
                    direct_identifiers_remaining=False,
                    remaining_findings=[],
                )
                self.assertFalse(refreshed["external_llm_gate"]["allowed"])
                self.assertIn(LANGUAGE_CONDITION, refreshed["external_llm_gate"]["failed_conditions"])
                self.assertNotEqual(refreshed["external_llm_readiness"], EXTERNAL_LLM_ALLOWED)
                self.assertNotEqual(refreshed["post_review_residual_risk"]["level"], "Low")
                self.assertTrue(refreshed["human_review_required"])

    def test_a_short_note_with_an_english_schedule_is_refused_too(self) -> None:
        """A clustered English section refuses however few markers it holds.

        This one carries nine English markers -- fewer than the annex, and
        fewer than an ordinary long Turkish filing accumulates in scattered
        form -- but they are contiguous, which is the whole question.
        """
        self.assertGreaterEqual(marker_evidence(TURKISH_NOTE_WITH_ENGLISH_SCHEDULE)[2], 5)
        self.assertNotEqual(detected_language(TURKISH_NOTE_WITH_ENGLISH_SCHEDULE), TURKISH)

        profile = analyze_privacy("ek.docx", TURKISH_NOTE_WITH_ENGLISH_SCHEDULE)
        self.assertFalse(profile["language_coverage"]["supported"])
        self.assertIn(LANGUAGE_CONDITION, profile["external_llm_gate"]["failed_conditions"])


class EnglishInsideADetectedEntityStillCountsTests(unittest.TestCase):
    """English markers are counted inside detected entity spans. Turkish are not.

    The exclusion that stops a Turkish COMPANY name from speaking for the
    document's language was first applied to both arms of `_marker_evidence`,
    and that quietly reopened the clustered-English route. The context sweeps
    are wide and trigger on English words -- `privileged_or_confidential`
    covers 160 characters from "privileged" -- so a single
    detected span blankets an English annex and deletes the evidence that
    refuses it.

    Measured on the annex below: excluding English inside spans took the
    evidence from (12, 17, 14) to (12, 3, 2), under the ceiling of 5, and the
    filing came back TURKISH with coverage supported and the gate ALLOWED over
    an unredacted London address, a UK mobile, a date of birth and a national
    insurance number. The direction is the whole point: dropping Turkish
    evidence can only refuse more documents, dropping English evidence can only
    clear more.
    """

    BODY_LENGTHS = (1, 9, 28)

    def filing(self, clauses: int) -> str:
        return (
            (TURKISH_BOILERPLATE_CLAUSE * clauses)
            + "\n\n"
            + TURKISH_FILING_PRIVILEGED_ENGLISH_ANNEX
        )

    def test_a_detected_span_really_does_cover_the_annexes_english(self) -> None:
        """Otherwise the refusal below would prove nothing about the asymmetry."""
        from legal_analyzer.classifier import _ENGLISH_MARKERS, _WORD_RE

        for clauses in self.BODY_LENGTHS:
            with self.subTest(clauses=clauses):
                text = self.filing(clauses)
                spans = detected_entity_spans(text)
                covered = [
                    word
                    for word in _WORD_RE.finditer(text)
                    if word.group(0).lower() in _ENGLISH_MARKERS
                    and any(start <= word.start() < end for start, end in spans)
                ]
                self.assertGreaterEqual(len(covered), 5, covered)

    def test_the_span_covered_english_still_refuses_the_filing(self) -> None:
        """The red-proof: guard the English arm with the spans and this goes TURKISH."""
        from legal_analyzer.classifier import _MAX_ENGLISH_RUN

        for clauses in self.BODY_LENGTHS:
            with self.subTest(clauses=clauses):
                text = self.filing(clauses)
                self.assertGreaterEqual(marker_evidence(text)[2], _MAX_ENGLISH_RUN)
                self.assertNotEqual(detected_language(text), TURKISH)

    def test_the_whole_consequence_is_refusal(self) -> None:
        """Asserted after a full review pass, because that is how it cleared."""
        for clauses in self.BODY_LENGTHS:
            with self.subTest(clauses=clauses):
                refreshed = refresh_release_state(
                    analyze_privacy("dilekce_ek3.docx", self.filing(clauses)),
                    redaction_completed=True,
                    human_review_approved=True,
                    unresolved_critical_count=0,
                    direct_identifiers_remaining=False,
                    remaining_findings=[],
                )
                self.assertFalse(refreshed["external_llm_gate"]["allowed"])
                self.assertIn(LANGUAGE_CONDITION, refreshed["external_llm_gate"]["failed_conditions"])
                self.assertNotEqual(refreshed["external_llm_readiness"], EXTERNAL_LLM_ALLOWED)
                self.assertNotEqual(refreshed["post_review_residual_risk"]["level"], "Low")
                self.assertTrue(refreshed["human_review_required"])

    def test_supplying_spans_is_never_the_permissive_direction(self) -> None:
        """The docstring's property, asserted directly.

        `entity_spans_of` may return nothing when offsets are missing, so both
        callers exist -- and the one that omits them must be the permissive
        one. Turkish may only fall, English may only hold or rise.
        """
        from legal_analyzer.classifier import _marker_evidence

        texts = [self.filing(n) for n in self.BODY_LENGTHS] + [
            WITNESS_STATEMENT,
            TURKISH_PETITION,
            GERMAN_CONTRACT,
            ENGLISH_SHEET_NAMING_A_TURKISH_COMPANY,
            ENGLISH_FORM_NAMING_TURKISH_PLACES,
            ENGLISH_FORM_NAMING_TURKISH_PARTIES,
            TURKISH_NOTE_WITH_ENGLISH_SCHEDULE,
            TURKISH_ROSTER_WITH_DIACRITICS,
            turkish_corpus(),
        ]
        for index, text in enumerate(texts):
            with self.subTest(text=index):
                bare_tr, bare_en, bare_run = _marker_evidence(text)
                spanned_tr, spanned_en, spanned_run = _marker_evidence(
                    text, detected_entity_spans(text)
                )
                self.assertLessEqual(spanned_tr, bare_tr, "spans must not add Turkish evidence")
                self.assertGreaterEqual(spanned_en, bare_en, "spans must not remove English evidence")
                self.assertGreaterEqual(spanned_run, bare_run, "spans must not shorten the English run")


class EnglishDocumentIsNotClearedTests(unittest.TestCase):
    """Section 3.6's reproduction, run again with the reviewer signing off."""

    def profile(self) -> dict:
        # The report's own call: both reviewer controls set by hand, which is
        # what produced seven green conditions.
        return analyze_privacy(
            "witness_statement.docx",
            WITNESS_STATEMENT,
            redaction_completed=True,
            human_review_approved=True,
        )

    def test_english_detection_finds_what_it_can_read_and_the_gate_still_refuses(self) -> None:
        """The deliberate change this class was written to make visible.

        This test asserted zero findings while no rule read English. English
        detection now reports the name, address, mobile, dates and amount --
        and every other test in this class still holds, because finding them
        is not reading the document: the national insurance number and the
        diagnosis below are still in no finding, so the gate keeps refusing
        on the language rather than on a count.
        """
        profile = self.profile()
        found = {(f["category"], f["sample"]) for f in profile["risk_map"]}
        for expected in (
            ("natural_person_name", "Marigold Quillfeather"),
            ("natural_person_name", "Desmond Crowhurst"),
            ("address", "179 Cobblewell Street, London EC2A 4NE"),
            ("phone_number", "07700 900318"),
            ("date", "12 June 2024"),
            ("money_amount", "42,500 pounds"),
        ):
            with self.subTest(expected=expected):
                self.assertIn(expected, found)
        for unread in ("QQ 12 34 56 C", "stress-related illness"):
            start = WITNESS_STATEMENT.index(unread)
            with self.subTest(unread=unread):
                self.assertFalse(
                    any(f["start"] < start + len(unread) and start < f["end"] for f in profile["risk_map"])
                )
        self.assertFalse(profile["external_llm_gate"]["allowed"])

    def test_residual_risk_is_not_low(self) -> None:
        profile = self.profile()
        self.assertEqual(profile["residual_risk"]["level"], "Unknown")
        self.assertIn(LANGUAGE_CONDITION, profile["external_llm_gate"]["failed_conditions"])

    def test_the_external_llm_gate_refuses(self) -> None:
        gate = self.profile()["external_llm_gate"]
        self.assertFalse(gate["allowed"])
        self.assertIn(LANGUAGE_CONDITION, gate["failed_conditions"])

    def test_the_readiness_line_no_longer_says_allowed(self) -> None:
        profile = self.profile()
        self.assertNotEqual(profile["external_llm_readiness"], EXTERNAL_LLM_ALLOWED)
        self.assertIn("cannot be cleared automatically", profile["external_llm_readiness"])

    def test_human_review_is_required(self) -> None:
        self.assertTrue(self.profile()["human_review_required"])

    def test_human_review_is_required_on_the_language_alone(self) -> None:
        """Isolates the arm, because the assertion above cannot.

        On the real profile the gate is already refusing, so
        human_review_required() returns True through `not gate["allowed"]`
        whatever the language arm does. Here every other input is handed in
        satisfied, so only the language can decide -- and the Turkish control
        proves the arm is not simply always True.
        """
        cleared_gate = {"allowed": True}
        low = {"level": "Low"}
        complete = {"status": "Complete", "blocks_external_llm": False}
        neutral = {"appears_privileged_or_confidential": False}

        self.assertTrue(
            human_review_required(cleared_gate, low, [], complete, neutral, language_coverage("EN"))
        )
        self.assertFalse(
            human_review_required(cleared_gate, low, [], complete, neutral, language_coverage("TR"))
        )

    def test_the_reviewer_is_told_what_the_rules_did_not_reach(self) -> None:
        checklist = " ".join(self.profile()["human_review_checklist"])
        self.assertIn("written for Turkish", checklist)
        self.assertIn("Read this document yourself", checklist)

    def test_the_limit_is_stated_once_not_twice(self) -> None:
        """It was said twice, back to back, in near-identical words.

        The gate-condition line already carries the limit; a second paragraph
        restating it in longer form made the reviewer read ~100 words of the
        same sentence, which is how a checklist stops being read at all.
        """
        checklist = self.profile()["human_review_checklist"]
        stating_the_limit = [line for line in checklist if "written for Turkish" in line]
        self.assertEqual(len(stating_the_limit), 1, checklist)
        self.assertIn("Read this document yourself", stating_the_limit[0])

    def test_the_refusal_states_a_limit_rather_than_an_error(self) -> None:
        """Terminology discipline: a limit the user cannot work around.

        The wording must not read as a failure, a defect or a missing feature,
        and it must not overclaim in the other direction either -- the rules are
        less complete outside Turkish, not useless there.
        """
        readiness = self.profile()["external_llm_readiness"].lower()
        for overstatement in ("error", "failed", "unsupported", "cannot detect", "no coverage"):
            self.assertNotIn(overstatement, readiness)
        self.assertIn("written for turkish", readiness)

    def test_a_review_pass_cannot_open_the_gate_either(self) -> None:
        """The refusal survives the one thing a reviewer could do about it.

        refresh_release_state() recomputes residual risk from what remains, so
        an English document with no findings has nothing remaining -- which is
        exactly the input that used to come back Low.
        """
        refreshed = refresh_release_state(
            analyze_privacy("witness_statement.docx", WITNESS_STATEMENT),
            redaction_completed=True,
            human_review_approved=True,
            unresolved_critical_count=0,
            direct_identifiers_remaining=False,
            remaining_findings=[],
        )
        self.assertFalse(refreshed["external_llm_gate"]["allowed"])
        self.assertIn(LANGUAGE_CONDITION, refreshed["external_llm_gate"]["failed_conditions"])
        self.assertEqual(refreshed["post_review_residual_risk"]["level"], "Unknown")
        self.assertTrue(refreshed["human_review_required"])


class UnknownLanguageRefusesTests(unittest.TestCase):
    """Silence fails toward refusal, not toward clearance."""

    def test_an_empty_extraction_is_not_read_as_turkish(self) -> None:
        profile = analyze_privacy(
            "scan.pdf", "", "PDF text extraction returned no text; OCR may be required."
        )
        self.assertEqual(profile["language_coverage"]["language"], UNKNOWN_LANGUAGE)
        self.assertFalse(profile["language_coverage"]["supported"])
        gate = profile["external_llm_gate"]
        self.assertFalse(gate["allowed"])
        self.assertIn(LANGUAGE_CONDITION, gate["failed_conditions"])

    def test_an_unidentifiable_fragment_cannot_reach_allowed(self) -> None:
        refreshed = refresh_release_state(
            analyze_privacy("note.txt", "Dilekce ekte sunulmustur."),
            redaction_completed=True,
            human_review_approved=True,
            unresolved_critical_count=0,
            direct_identifiers_remaining=False,
            remaining_findings=[],
        )
        self.assertFalse(refreshed["external_llm_gate"]["allowed"])
        self.assertIn(LANGUAGE_CONDITION, refreshed["external_llm_gate"]["failed_conditions"])

    def test_a_profile_written_before_this_existed_is_not_assumed_turkish(self) -> None:
        """A stored profile with no language answer is silence, not Turkish.

        Such a document stays blocked until detection is re-run over it, which
        is what re-establishes the fact. Assuming Turkish here would reopen the
        whole defect for every already-indexed document.
        """
        profile = analyze_privacy("dilekce.docx", TURKISH_PETITION)
        legacy = json.loads(json.dumps(profile))
        legacy.pop("language_coverage")

        refreshed = refresh_release_state(
            legacy,
            redaction_completed=True,
            human_review_approved=True,
            unresolved_critical_count=0,
            direct_identifiers_remaining=False,
            remaining_findings=[],
        )
        self.assertFalse(refreshed["external_llm_gate"]["allowed"])
        self.assertIn(LANGUAGE_CONDITION, refreshed["external_llm_gate"]["failed_conditions"])

    def test_only_the_exact_code_the_detector_emits_counts_as_turkish(self) -> None:
        """`detect_language` emits exactly TR, EN or Unknown, and nothing else.

        A stored 'tr' or 'Turkish' is a value no writer in this codebase
        produces, so on a safety gate it is an unrecognised answer rather than a
        spelling to be forgiven -- matching it loosely only widens what can
        silently read as covered.
        """
        for value in ("tr", "Tr", "TURKISH", " tr "):
            with self.subTest(value=value):
                self.assertFalse(language_coverage(value)["supported"])
        self.assertTrue(language_coverage(TURKISH)["supported"])

    def test_the_gate_defaults_an_absent_language_to_refusal(self) -> None:
        """Every other condition met, and no coverage handed in."""
        gate = external_llm_gate_policy(
            {"level": "Low"},
            [],
            {"status": "Complete", "blocks_external_llm": False},
            redaction_completed=True,
            human_review_approved=True,
            unresolved_critical_count=0,
            direct_identifiers_remaining=False,
        )
        self.assertFalse(gate["allowed"])
        # Three, not one: with no language answer the rules' zero counts are not
        # a finding either, so the two count conditions refuse beside it.
        self.assertEqual(
            gate["failed_conditions"],
            [LANGUAGE_CONDITION, CRITICAL_NOT_ASSESSABLE, DIRECT_IDENTIFIER_NOT_ASSESSABLE],
        )


class ZeroFindingsFromUnreadRulesIsNotAClearResultTests(unittest.TestCase):
    """The release checklist does not tick what the rules could not read.

    The gate already refused an English or unidentified document; the checklist
    beside that refusal still reported "No undecided critical findings: clear"
    and "No direct identifiers remain: none remain" -- two green ticks the
    studio renders verbatim, each asserting something about a document the
    rules found nothing in because they cannot read it. Both now fail on an
    uncovered document and say why, the way residual risk says "currently
    Unknown". Every assertion here also pins that the rules really did find
    nothing, because a detected finding would fail these rows for the old
    reason and prove nothing about the new one.
    """

    def assert_counts_not_assessable(self, gate: dict) -> None:
        rows = {c["id"]: c for c in gate["conditions"]}
        self.assertEqual(len(gate["conditions"]), 8, gate["conditions"])
        for condition_id in ("no_critical_findings", "no_direct_identifiers"):
            with self.subTest(condition=condition_id):
                self.assertFalse(rows[condition_id]["passed"], rows[condition_id])
                self.assertEqual(rows[condition_id]["detail"], NOT_ASSESSABLE)
        self.assertEqual(
            gate["failed_conditions"],
            [LANGUAGE_CONDITION, "Residual risk must be Low.", CRITICAL_NOT_ASSESSABLE, DIRECT_IDENTIFIER_NOT_ASSESSABLE],
        )
        self.assertFalse(gate["allowed"])

    def test_an_english_settlement_agreement_is_not_reported_clear(self) -> None:
        # The fixture's personal data is the kind the English rules do not
        # reach; see its note.
        profile = analyze_privacy(
            "settlement_agreement.txt",
            ENGLISH_SETTLEMENT_AGREEMENT,
            redaction_completed=True,
            human_review_approved=True,
        )
        self.assertEqual(profile["language_coverage"]["language"], ENGLISH)
        self.assertEqual(profile["risk_map"], [])
        self.assert_counts_not_assessable(profile["external_llm_gate"])

    def test_a_review_pass_does_not_turn_them_green(self) -> None:
        """The state the studio actually renders after review.

        refresh_release_state() is handed explicit zero counts by the review
        path, which is the input most likely to read as "clear".
        """
        refreshed = refresh_release_state(
            analyze_privacy("settlement_agreement.txt", ENGLISH_SETTLEMENT_AGREEMENT),
            redaction_completed=True,
            human_review_approved=True,
            unresolved_critical_count=0,
            direct_identifiers_remaining=False,
            remaining_findings=[],
        )
        self.assert_counts_not_assessable(refreshed["external_llm_gate"])

    def test_an_unidentified_language_is_not_reported_clear_either(self) -> None:
        refreshed = refresh_release_state(
            analyze_privacy("liefervertrag.docx", GERMAN_CONTRACT),
            redaction_completed=True,
            human_review_approved=True,
            unresolved_critical_count=0,
            direct_identifiers_remaining=False,
            remaining_findings=[],
        )
        self.assertEqual(refreshed["language_coverage"]["language"], UNKNOWN_LANGUAGE)
        self.assertEqual(refreshed["risk_map"], [])
        self.assert_counts_not_assessable(refreshed["external_llm_gate"])

    def test_a_count_the_rules_did_reach_is_still_reported(self) -> None:
        """A detected count on an uncovered document is a floor, and it stays.

        Replacing "2 outstanding" with "not assessable" would hide a finding the
        reviewer can still act on.
        """
        gate = external_llm_gate_policy(
            {"level": "Unknown"},
            [],
            {"status": "Complete", "blocks_external_llm": False},
            redaction_completed=True,
            human_review_approved=True,
            unresolved_critical_count=2,
            direct_identifiers_remaining=True,
            coverage=language_coverage(ENGLISH),
        )
        rows = {c["id"]: c for c in gate["conditions"]}
        self.assertEqual(rows["no_critical_findings"]["detail"], "2 outstanding")
        self.assertEqual(rows["no_direct_identifiers"]["detail"], "at least one remains")
        self.assertFalse(rows["no_critical_findings"]["passed"])
        self.assertFalse(rows["no_direct_identifiers"]["passed"])
        self.assertEqual(
            gate["failed_conditions"],
            [LANGUAGE_CONDITION, "Residual risk must be Low.", CRITICAL_CONDITION, DIRECT_IDENTIFIER_CONDITION],
        )

    def test_the_reviewer_is_not_told_to_resolve_a_count_of_zero(self) -> None:
        """The checklist and pipeline line are built from the failed messages.

        "Resolve gate condition: Critical findings must be zero." on a document
        with zero findings is the same false statement as the green tick, and
        nothing the reviewer does can satisfy it.
        """
        profile = analyze_privacy(
            "settlement_agreement.txt",
            ENGLISH_SETTLEMENT_AGREEMENT,
            redaction_completed=True,
            human_review_approved=True,
        )
        self.assertEqual(profile["risk_map"], [])
        checklist = profile["human_review_checklist"]
        self.assertIn(f"Resolve gate condition: {CRITICAL_NOT_ASSESSABLE}", checklist)
        self.assertIn(f"Resolve gate condition: {DIRECT_IDENTIFIER_NOT_ASSESSABLE}", checklist)
        for line in checklist:
            self.assertNotIn(CRITICAL_CONDITION, line)
            self.assertNotIn(DIRECT_IDENTIFIER_CONDITION, line)

        status = compute_pipeline_status(
            {
                "extraction_status": "Complete",
                "ocr_status": "not_required",
                "redaction_completed": 1,
                "human_review_approved": 1,
                "privacy_profile": json.dumps(profile),
            }
        )
        self.assertEqual(status["pipeline_stage"], STAGE_BLOCKED)
        self.assertEqual(
            status["pipeline_message"],
            f"Blocked — {LANGUAGE_CONDITION.rstrip('.')}. (3 more conditions unmet.)",
        )


class TurkishIsUnchangedTests(unittest.TestCase):
    """The refusal is targeted. A Turkish document clears exactly as before."""

    def test_the_turkish_petition_is_measured_not_refused(self) -> None:
        profile = analyze_privacy("dilekce.docx", TURKISH_PETITION)
        self.assertTrue(profile["language_coverage"]["supported"])
        self.assertEqual(profile["residual_risk"]["level"], "High")
        self.assertNotIn(LANGUAGE_CONDITION, profile["external_llm_gate"]["failed_conditions"])

    def test_a_fully_reviewed_turkish_document_still_reaches_allowed(self) -> None:
        refreshed = refresh_release_state(
            analyze_privacy("dilekce.docx", TURKISH_PETITION),
            redaction_completed=True,
            human_review_approved=True,
            unresolved_critical_count=0,
            direct_identifiers_remaining=False,
            remaining_findings=[],
        )
        self.assertEqual(refreshed["external_llm_gate"]["failed_conditions"], [])
        self.assertTrue(refreshed["external_llm_gate"]["allowed"])
        self.assertEqual(refreshed["external_llm_readiness"], EXTERNAL_LLM_ALLOWED)

    def test_a_fully_reviewed_turkish_document_reports_its_counts_as_before(self) -> None:
        """The control for the not-assessable rows: covered documents are untouched."""
        refreshed = refresh_release_state(
            analyze_privacy("dilekce.docx", TURKISH_PETITION),
            redaction_completed=True,
            human_review_approved=True,
            unresolved_critical_count=0,
            direct_identifiers_remaining=False,
            remaining_findings=[],
        )
        # Whole rows, not (passed, detail): a passing row's `message` is served
        # on /api/document/<id> too, and it must not pick up the uncovered
        # wording on a covered document.
        rows = {c["id"]: c for c in refreshed["external_llm_gate"]["conditions"]}
        self.assertEqual(
            rows["no_critical_findings"],
            {
                "id": "no_critical_findings",
                "label": "No undecided critical findings",
                "policy": "Critical findings = 0",
                "message": CRITICAL_CONDITION,
                "passed": True,
                "detail": "clear",
            },
        )
        self.assertEqual(
            rows["no_direct_identifiers"],
            {
                "id": "no_direct_identifiers",
                "label": "No direct identifiers remain",
                "policy": "No direct identifiers remain",
                "message": DIRECT_IDENTIFIER_CONDITION,
                "passed": True,
                "detail": "none remain",
            },
        )

    def test_an_unreviewed_turkish_document_reports_its_counts_as_before(self) -> None:
        profile = analyze_privacy(
            "dilekce.docx", TURKISH_PETITION, redaction_completed=True, human_review_approved=True
        )
        rows = {c["id"]: (c["passed"], c["detail"]) for c in profile["external_llm_gate"]["conditions"]}
        self.assertEqual(rows["no_critical_findings"], (False, "1 outstanding"))
        self.assertEqual(rows["no_direct_identifiers"], (False, "at least one remains"))
        self.assertEqual(
            profile["external_llm_gate"]["failed_conditions"],
            ["Residual risk must be Low.", CRITICAL_CONDITION, DIRECT_IDENTIFIER_CONDITION],
        )

    def test_the_language_condition_passes_on_a_turkish_document(self) -> None:
        conditions = analyze_privacy("dilekce.docx", TURKISH_PETITION)["external_llm_gate"]["conditions"]
        language = [c for c in conditions if c["id"] == "language_supported"]
        self.assertEqual(len(language), 1, conditions)
        self.assertTrue(language[0]["passed"])
        self.assertEqual(language[0]["detail"], "Turkish")


class StoredLanguageComesFromTheDocumentTests(unittest.TestCase):
    """End to end, over the upload path a user actually takes."""

    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tmp_path = Path(self.tmp.name)
        self.db_path = self.tmp_path / "test.db"
        self.original_db_path = app_module.DB_PATH
        app_module.DB_PATH = self.db_path
        app_module.READY_DB_PATHS.discard(self.db_path.resolve())
        self.addCleanup(self.restore)

        conn = sqlite3.connect(self.db_path)
        conn.executescript(Path("db/schema.sql").read_text(encoding="utf-8"))
        for name, name_tr, icon in CATEGORIES:
            conn.execute(
                "INSERT OR IGNORE INTO categories (name, name_tr, icon) VALUES (?, ?, ?)",
                (name, name_tr, icon),
            )
            category_id = conn.execute("SELECT id FROM categories WHERE name = ?", (name,)).fetchone()[0]
            for sub_name, sub_name_tr in SUBCATEGORIES[name]:
                conn.execute(
                    "INSERT OR IGNORE INTO subcategories (category_id, name, name_tr) VALUES (?, ?, ?)",
                    (category_id, sub_name, sub_name_tr),
                )
        conn.execute(
            "INSERT INTO users (username, password_hash, role) VALUES (?, ?, 'reviewer')",
            ("reviewer", generate_password_hash("secret")),
        )
        conn.commit()
        conn.close()

        self.client = app_module.app.test_client()
        self.client.post("/login", json={"username": "reviewer", "password": "secret"})

    def restore(self) -> None:
        app_module.DB_PATH = self.original_db_path
        app_module.READY_DB_PATHS.discard(self.db_path.resolve())

    def upload(self, filename: str, text: str) -> dict:
        with mock.patch.object(app_module, "UPLOAD_DIR", self.tmp_path / "uploads"):
            response = self.client.post(
                "/api/upload",
                data={"file": (BytesIO(text.encode("utf-8")), filename)},
                content_type="multipart/form-data",
            )
        # /api/upload returns the document detail payload the studio renders.
        self.assertEqual(response.status_code, 200, response.json)
        return response.json

    def test_an_english_upload_is_stored_as_english_and_refused(self) -> None:
        # The filename is Turkish on purpose: under the filename rule this
        # document was stored as TR and the coverage notice stayed hidden.
        detail = self.upload("dilekce_2026.txt", WITNESS_STATEMENT)

        self.assertEqual(detail["language"], ENGLISH)
        self.assertEqual(detail["residual_risk"], "Unknown")
        self.assertFalse(detail["privacy_profile"]["external_llm_gate"]["allowed"])
        self.assertIn(
            LANGUAGE_CONDITION,
            detail["privacy_profile"]["external_llm_gate"]["failed_conditions"],
        )

    def test_a_turkish_upload_is_stored_as_turkish_and_measured(self) -> None:
        detail = self.upload("belge.txt", TURKISH_PETITION)

        self.assertEqual(detail["language"], TURKISH)
        self.assertNotIn(
            LANGUAGE_CONDITION,
            detail["privacy_profile"]["external_llm_gate"]["failed_conditions"],
        )

    def test_a_refresh_keeps_the_column_and_the_gate_in_step(self) -> None:
        """Every write site, not just the ones that run detection.

        `refresh_document_state` persists a profile whose `language_coverage`
        may have just been resolved for the first time -- a profile stored
        before the condition existed resolves to Unknown -- and it used to
        leave the column holding the retired filename rule's answer, so the
        payload carried `language: 'TR'` beside a coverage answer of Unknown.
        """
        detail = self.upload("belge.txt", TURKISH_PETITION)
        doc_id = detail["id"]
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        legacy = json.loads(conn.execute(
            "SELECT privacy_profile FROM documents WHERE id = ?", (doc_id,)
        ).fetchone()["privacy_profile"])
        legacy.pop("language_coverage")
        conn.execute(
            "UPDATE documents SET privacy_profile = ?, language = 'TR' WHERE id = ?",
            (json.dumps(legacy, ensure_ascii=False), doc_id),
        )
        app_module.refresh_document_state(conn, doc_id)
        row = conn.execute("SELECT language, privacy_profile FROM documents WHERE id = ?", (doc_id,)).fetchone()
        conn.commit()
        conn.close()

        profile = json.loads(row["privacy_profile"])
        coverage = profile["external_llm_gate"]["language_coverage"]
        self.assertEqual(coverage["language"], UNKNOWN_LANGUAGE)
        self.assertEqual(row["language"], coverage["language"])
        self.assertNotIn(
            "language_coverage",
            profile,
            "a refresh must not fabricate a detection-time answer it never read the text for",
        )

    def test_the_stored_language_and_the_gate_cannot_disagree(self) -> None:
        """One answer, written once. The notice reads the column; the gate reads
        the profile, and a document where those two differ would show a green
        release panel under an English-coverage caveat, or the reverse."""
        for filename, text in (("dilekce_2026.txt", WITNESS_STATEMENT), ("belge.txt", TURKISH_PETITION)):
            with self.subTest(filename=filename):
                detail = self.upload(filename, text)
                self.assertEqual(
                    detail["language"],
                    detail["privacy_profile"]["language_coverage"]["language"],
                )


class ALanguageRefusalIsVisibleWhereRefusalsAreCountedTests(unittest.TestCase):
    """A document the language condition refuses must read as Blocked.

    Every surface that counts or filters refusals selects on the "Blocked"
    prefix. The dashboard's Blocked chip does it in Python over the *derived*
    readiness (`blueprints/documents.py`, case-insensitively, because the SQL
    `LIKE` it replaced was case-insensitive for ASCII and the retired rail URLs
    are bookmarks); `benchmark.py` and `classify.py` count the persisted
    `external_llm_readiness` column the same way. The language refusal was
    phrased "Not cleared for external LLM use: ...", so it matched none of
    them: a reviewer filtering for blocked documents was shown every refusal
    except the new one.
    """

    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tmp_path = Path(self.tmp.name)
        self.db_path = self.tmp_path / "test.db"
        self.original_db_path = app_module.DB_PATH
        app_module.DB_PATH = self.db_path
        app_module.READY_DB_PATHS.discard(self.db_path.resolve())
        self.addCleanup(self.restore)

        conn = sqlite3.connect(self.db_path)
        conn.executescript(Path("db/schema.sql").read_text(encoding="utf-8"))
        for name, name_tr, icon in CATEGORIES:
            conn.execute(
                "INSERT OR IGNORE INTO categories (name, name_tr, icon) VALUES (?, ?, ?)",
                (name, name_tr, icon),
            )
            category_id = conn.execute("SELECT id FROM categories WHERE name = ?", (name,)).fetchone()[0]
            for sub_name, sub_name_tr in SUBCATEGORIES[name]:
                conn.execute(
                    "INSERT OR IGNORE INTO subcategories (category_id, name, name_tr) VALUES (?, ?, ?)",
                    (category_id, sub_name, sub_name_tr),
                )
        conn.execute(
            "INSERT INTO users (username, password_hash, role) VALUES (?, ?, 'reviewer')",
            ("reviewer", generate_password_hash("secret")),
        )
        conn.commit()
        conn.close()

        self.client = app_module.app.test_client()
        self.client.post("/login", json={"username": "reviewer", "password": "secret"})
        with mock.patch.object(app_module, "UPLOAD_DIR", self.tmp_path / "uploads"):
            response = self.client.post(
                "/api/upload",
                data={"file": (BytesIO(WITNESS_STATEMENT.encode("utf-8")), "dilekce.txt")},
                content_type="multipart/form-data",
            )
        self.assertEqual(response.status_code, 200, response.json)
        self.doc_id = response.json["id"]

    def restore(self) -> None:
        app_module.DB_PATH = self.original_db_path
        app_module.READY_DB_PATHS.discard(self.db_path.resolve())

    def test_the_language_condition_is_what_phrases_the_refusal(self) -> None:
        """Without this the assertions below could pass for another reason."""
        gate = self.client.get(f"/api/document/{self.doc_id}").json["privacy_profile"]["external_llm_gate"]
        self.assertFalse(gate["allowed"])
        self.assertIn(LANGUAGE_CONDITION, gate["failed_conditions"])
        self.assertIn("written for Turkish", gate["decision"])

    def test_the_blocked_chip_lists_it(self) -> None:
        """The chip's Python prefix filter over the derived readiness selects it."""
        listed = self.client.get("/api/documents?readiness=Blocked").json
        self.assertEqual([doc["id"] for doc in listed["documents"]], [self.doc_id])

    def test_the_persisted_readiness_column_reads_as_blocked(self) -> None:
        """The column `benchmark.py` and `classify.py` count carries the prefix.

        Those two still report with `LIKE 'Blocked%'` against the stored
        column; the dashboard KPI that once did the same was deleted, so this
        asserts the persisted value, not anything the dashboard queries.
        """
        conn = sqlite3.connect(self.db_path)
        blocked = conn.execute(
            "SELECT COUNT(*) FROM documents WHERE external_llm_readiness LIKE 'Blocked%'"
        ).fetchone()[0]
        conn.close()
        self.assertEqual(blocked, 1)

    def test_the_refusal_still_states_the_limit_rather_than_an_error(self) -> None:
        readiness = self.client.get(f"/api/document/{self.doc_id}").json["external_llm_readiness"].lower()
        self.assertIn("cannot be cleared automatically", readiness)
        self.assertIn("written for turkish", readiness)
        for overstatement in ("error", "failed", "unsupported", "cannot detect", "no coverage"):
            self.assertNotIn(overstatement, readiness)


class AcceptedOcrCarriesItsOwnLanguageAnswerTests(unittest.TestCase):
    """A reviewer's accepted OCR is the document's text, so it answers the language.

    For a document whose text came from reviewer-accepted OCR, re-running
    detection is unreachable -- /reextract refuses while OCR is accepted,
    because the file on disk is not where that document's text came from -- so
    an already-indexed Turkish scan would be permanently refused, and the only
    documented way out would be to reject the OCR the reviewer had accepted.
    The accepted text is still stored, and the backfill reads it.

    The backfill is the ONE mechanism. A second copy of this recovery ran on
    every read and could flip a document from refused to cleared without
    writing anything; two paths deriving the same fact from the same source
    drift. Until the backfill is run, such a document reads as uncovered, which
    is the direction silence is supposed to fail in.
    """

    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = Path(self.tmp.name) / "test.db"
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(Path("db/schema.sql").read_text(encoding="utf-8"))
        self.addCleanup(self.conn.close)

    def legacy_profile(self, text: str) -> dict:
        """What a pre-change profile looked like: everything but the language."""
        profile = json.loads(json.dumps(analyze_privacy("tarama.pdf", text)))
        profile.pop("language_coverage")
        return profile

    def seed(self, text: str, ocr_status: str, store_pages: bool = True) -> int:
        self.conn.execute(
            """
            INSERT INTO documents (
                filename, filepath, file_extension, language, extraction_status,
                privacy_profile, redaction_completed, human_review_approved, ocr_status
            ) VALUES (?, ?, '.pdf', 'TR', 'Complete', ?, 1, 1, ?)
            """,
            (
                "tarama.pdf",
                f"/tmp/tarama-{ocr_status}-{store_pages}.pdf",
                json.dumps(self.legacy_profile(text), ensure_ascii=False),
                ocr_status,
            ),
        )
        doc_id = self.conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        if store_pages:
            self.conn.execute(
                "INSERT INTO ocr_pages (document_id, page_number, text, status) VALUES (?, 1, ?, 'accepted')",
                (doc_id, text),
            )
        return doc_id

    def gate(self, doc_id: int) -> dict:
        app_module.refresh_document_state(self.conn, doc_id)
        row = self.conn.execute("SELECT privacy_profile FROM documents WHERE id = ?", (doc_id,)).fetchone()
        return json.loads(row["privacy_profile"])["external_llm_gate"]

    def test_such_a_document_is_refused_until_the_backfill_runs(self) -> None:
        """Reading it never recovers the answer. Only the backfill writes one."""
        gate = self.gate(self.seed(TURKISH_PETITION, "accepted"))
        self.assertIn(LANGUAGE_CONDITION, gate["failed_conditions"])

    def test_the_backfill_reads_the_accepted_text_rather_than_stranding_it(self) -> None:
        doc_id = self.seed(TURKISH_PETITION, "accepted")
        app_module.backfill_language_coverage(self.conn)

        gate = self.gate(doc_id)
        self.assertNotIn(LANGUAGE_CONDITION, gate["failed_conditions"])
        self.assertTrue(gate["allowed"], gate["failed_conditions"])

    def test_the_reviewers_ocr_decision_survives_it(self) -> None:
        """The workaround this replaces was "reject the accepted OCR and redo it"."""
        doc_id = self.seed(TURKISH_PETITION, "accepted")
        app_module.backfill_language_coverage(self.conn)

        row = self.conn.execute("SELECT ocr_status FROM documents WHERE id = ?", (doc_id,)).fetchone()
        self.assertEqual(row["ocr_status"], "accepted")
        pages = self.conn.execute(
            "SELECT status, text FROM ocr_pages WHERE document_id = ?", (doc_id,)
        ).fetchall()
        self.assertEqual([(p["status"], p["text"]) for p in pages], [("accepted", TURKISH_PETITION)])

    def test_the_answer_is_the_accepted_texts_own(self) -> None:
        """It re-reads the text; it does not assume the answer is Turkish."""
        doc_id = self.seed(WITNESS_STATEMENT, "accepted")
        app_module.backfill_language_coverage(self.conn)

        gate = self.gate(doc_id)
        self.assertIn(LANGUAGE_CONDITION, gate["failed_conditions"])
        self.assertFalse(gate["allowed"])

    def test_a_document_with_no_accepted_text_stays_refused(self) -> None:
        """Nothing is invented: with no accepted OCR there is nothing to read."""
        doc_id = self.seed(TURKISH_PETITION, "accepted", store_pages=False)
        app_module.backfill_language_coverage(self.conn)

        self.assertIn(LANGUAGE_CONDITION, self.gate(doc_id)["failed_conditions"])


class AStoredClearanceIsNotServedAsClearanceTests(unittest.TestCase):
    """The gate on the screen is computed, not deserialized.

    The stored profile is a cache of a decision, and a cache written under the
    old seven-condition gate has `allowed: true` and seven green ticks frozen
    into it. `/api/document/<id>` used to hand that straight to the studio, so
    an English document approved before the language became a condition went on
    showing "Allowed for external LLM use" -- the exact false green light this
    change exists to remove -- until some unrelated write happened to refresh
    it. Serving a stale clearance is clearing the document.
    """

    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = Path(self.tmp.name) / "test.db"
        self.original_db_path = app_module.DB_PATH
        app_module.DB_PATH = self.db_path
        app_module.READY_DB_PATHS.discard(self.db_path.resolve())
        self.addCleanup(self.restore)

        conn = sqlite3.connect(self.db_path)
        conn.executescript(Path("db/schema.sql").read_text(encoding="utf-8"))
        conn.execute(
            "INSERT INTO users (username, password_hash, role) VALUES (?, ?, 'reviewer')",
            ("reviewer", generate_password_hash("secret")),
        )
        conn.execute(
            """
            INSERT INTO documents (
                id, filename, filepath, file_extension, language, extraction_status,
                privacy_profile, residual_risk, external_llm_readiness, human_review_required,
                redaction_status, redaction_completed, human_review_approved,
                review_status, ocr_status
            ) VALUES (1, 'witness_statement.docx', '/tmp/witness_statement.docx', '.docx', 'TR',
                      'Complete', ?, 'Low', ?, 0, 'Redaction completed', 1, 1,
                      'approved_for_external_llm', 'not_required')
            """,
            (json.dumps(self.legacy_profile(), ensure_ascii=False), EXTERNAL_LLM_ALLOWED),
        )
        conn.execute("INSERT INTO matters (id, name) VALUES (1, 'Quillfeather v Harrowgate')")
        conn.execute("INSERT INTO document_matters (document_id, matter_id) VALUES (1, 1)")
        conn.commit()
        conn.close()

        self.client = app_module.app.test_client()
        self.client.post("/login", json={"username": "reviewer", "password": "secret"})

    def restore(self) -> None:
        app_module.DB_PATH = self.original_db_path
        app_module.READY_DB_PATHS.discard(self.db_path.resolve())

    def legacy_profile(self) -> dict:
        """An English document as it was stored under the old gate: cleared."""
        profile = json.loads(json.dumps(analyze_privacy("witness_statement.docx", WITNESS_STATEMENT)))
        profile.pop("language_coverage")
        conditions = [
            {"id": condition["id"], "label": condition["label"], "detail": condition["detail"], "passed": True}
            for condition in profile["external_llm_gate"]["conditions"]
            if condition["id"] != "language_supported"
        ]
        profile["external_llm_gate"] = {
            "allowed": True,
            "decision": EXTERNAL_LLM_ALLOWED,
            "conditions": conditions,
            "failed_conditions": [],
            "policy": [],
        }
        profile["external_llm_readiness"] = EXTERNAL_LLM_ALLOWED
        return profile

    def test_the_stored_profile_really_does_claim_clearance(self) -> None:
        """Without this the assertions below could pass on an empty fixture."""
        stored = self.legacy_profile()
        self.assertTrue(stored["external_llm_gate"]["allowed"])
        self.assertEqual(stored["external_llm_readiness"], EXTERNAL_LLM_ALLOWED)
        self.assertNotIn("language_supported", [c["id"] for c in stored["external_llm_gate"]["conditions"]])

    def test_the_studio_payload_refuses_the_document(self) -> None:
        payload = self.client.get("/api/document/1").json
        gate = payload["privacy_profile"]["external_llm_gate"]
        self.assertFalse(gate["allowed"])
        self.assertIn(LANGUAGE_CONDITION, gate["failed_conditions"])
        self.assertIn("language_supported", [c["id"] for c in gate["conditions"]])

    def test_the_readiness_line_beside_it_agrees(self) -> None:
        payload = self.client.get("/api/document/1").json
        self.assertNotEqual(payload["external_llm_readiness"], EXTERNAL_LLM_ALLOWED)
        self.assertNotEqual(payload["privacy_profile"]["external_llm_readiness"], EXTERNAL_LLM_ALLOWED)

    def test_the_dashboard_list_refuses_it_too(self) -> None:
        """The list is the surface a reviewer scans first.

        `/api/document/<id>` computed the release state while `/api/documents`
        served the stored columns, so the same document read "Ready - download
        the reviewed redacted version" in the table and refused the moment it
        was opened.
        """
        listed = self.client.get("/api/documents").json["documents"]
        row = next(doc for doc in listed if doc["id"] == 1)
        self.assertNotEqual(row["external_llm_readiness"], EXTERNAL_LLM_ALLOWED)
        self.assertTrue(row["external_llm_readiness"].startswith("Blocked"))
        self.assertNotEqual(row["pipeline_stage"], "ready")

    def test_the_blocked_chip_finds_it(self) -> None:
        """The chip selects on the derived answer, not on the cached one."""
        listed = self.client.get("/api/documents?readiness=Blocked").json
        self.assertEqual([doc["id"] for doc in listed["documents"]], [1])
        self.assertEqual(listed["total"], 1)
        self.assertEqual(self.client.get("/api/documents?readiness=Allowed").json["documents"], [])

    def test_the_blocked_chip_is_not_case_sensitive(self) -> None:
        """`/?readiness=blocked` is a bookmark someone may hold.

        The filter moved out of SQL, where LIKE is case-insensitive for ASCII,
        into Python -- where `startswith` is not. A hand-typed or bookmarked
        lowercase URL came back empty with total 0: a wrong answer, silently.
        """
        for spelling in ("Blocked", "blocked", "BLOCKED"):
            with self.subTest(spelling=spelling):
                listed = self.client.get(f"/api/documents?readiness={spelling}").json
                self.assertEqual([doc["id"] for doc in listed["documents"]], [1], spelling)
                self.assertEqual(listed["total"], 1)

    def test_the_matter_workspace_refuses_it_as_well(self) -> None:
        """Matters are the product's organizing surface; it reads the same answer.

        Both endpoints are asserted in the same session on purpose. Asserting
        only the matter view would pass if the deriver broke everywhere at
        once; asserting both pins that the two agree, which is the property the
        stored column cannot give.
        """
        stored = sqlite3.connect(self.db_path).execute(
            "SELECT external_llm_readiness FROM documents WHERE id = 1"
        ).fetchone()[0]
        self.assertEqual(stored, EXTERNAL_LLM_ALLOWED, "the fixture must start from a stale clearance")

        listed = self.client.get("/api/matter/1").json["documents"]
        self.assertEqual([doc["id"] for doc in listed], [1])
        detail = self.client.get("/api/document/1").json

        self.assertEqual(listed[0]["external_llm_readiness"], detail["external_llm_readiness"])
        self.assertNotEqual(listed[0]["external_llm_readiness"], EXTERNAL_LLM_ALLOWED)
        self.assertTrue(listed[0]["external_llm_readiness"].startswith("Blocked"))

    def test_the_matter_workspace_keeps_the_fields_it_always_returned(self) -> None:
        """Deriving the value must not quietly change the response shape."""
        listed = self.client.get("/api/matter/1").json["documents"][0]
        self.assertEqual(
            set(listed),
            {
                "id", "filename", "title", "file_extension", "residual_risk",
                "review_status", "ocr_status", "extraction_status", "external_llm_readiness",
                "finding_count", "critical_count", "artifact_count",
            },
        )

    def test_reading_the_document_does_not_rewrite_it(self) -> None:
        """Computed for the response; the row is only written by write paths."""
        conn = sqlite3.connect(self.db_path)
        before = conn.execute("SELECT privacy_profile, updated_at FROM documents WHERE id = 1").fetchone()
        conn.close()

        self.client.get("/api/document/1")

        conn = sqlite3.connect(self.db_path)
        after = conn.execute("SELECT privacy_profile, updated_at FROM documents WHERE id = 1").fetchone()
        conn.close()
        self.assertEqual(before, after)


class TheBackfillSettlesStoredRecordsTests(unittest.TestCase):
    """The one-off pass that gives already-indexed documents a language answer.

    `/api/document/<id>` computes the release state on read, but the dashboard
    list serves the stored columns, so an English document approved under the
    old gate went on listing as "Ready - download the reviewed redacted
    version". The backfill reads each document's language and nothing else: the
    reviewer's decisions have to come through it untouched, which is why it is
    not `apply_reextraction()`.
    """

    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tmp_path = Path(self.tmp.name)
        self.db_path = self.tmp_path / "test.db"
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(Path("db/schema.sql").read_text(encoding="utf-8"))
        self.addCleanup(self.conn.close)

    def seed(self, filename: str, text: str, write_file: bool = True) -> int:
        """A record as it was stored before the language became a condition."""
        profile = json.loads(json.dumps(analyze_privacy(filename, text)))
        profile.pop("language_coverage")
        profile["external_llm_gate"]["allowed"] = True
        profile["external_llm_readiness"] = EXTERNAL_LLM_ALLOWED
        path = self.tmp_path / filename
        if write_file:
            path.write_text(text, encoding="utf-8")
        self.conn.execute(
            """
            INSERT INTO documents (
                filename, filepath, file_extension, language, extraction_status,
                privacy_profile, residual_risk, external_llm_readiness, human_review_required,
                redaction_status, redaction_completed, human_review_approved,
                review_status, ocr_status
            ) VALUES (?, ?, '.txt', 'TR', 'Complete', ?, 'Low', ?, 0,
                      'Redaction completed', 1, 1, 'approved_for_external_llm', 'not_required')
            """,
            (filename, str(path), json.dumps(profile, ensure_ascii=False), EXTERNAL_LLM_ALLOWED),
        )
        doc_id = self.conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        for category, sample, status in (
            ("date", "01.01.2026", "approved"),
            ("party_role", "Davaci", "dismissed"),
        ):
            self.conn.execute(
                """
                INSERT INTO privacy_findings (
                    document_id, category, sample, risk, recommended_action, placeholder,
                    review_status, source, fingerprint
                ) VALUES (?, ?, ?, 'MEDIUM', 'Redact', '[REDACTED]', ?, 'detector', ?)
                """,
                (doc_id, category, sample, status, f"{doc_id}{category}"[:12]),
            )
        return doc_id

    def row(self, doc_id: int) -> sqlite3.Row:
        return self.conn.execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone()

    def profile(self, doc_id: int) -> dict:
        return json.loads(self.row(doc_id)["privacy_profile"])

    def test_the_seeded_record_really_is_a_stale_clearance(self) -> None:
        doc_id = self.seed("dilekce.txt", WITNESS_STATEMENT)
        self.assertNotIn("language_coverage", self.profile(doc_id))
        self.assertEqual(self.row(doc_id)["external_llm_readiness"], EXTERNAL_LLM_ALLOWED)
        self.assertEqual(self.row(doc_id)["residual_risk"], "Low")

    def test_an_english_record_is_settled_as_refused(self) -> None:
        doc_id = self.seed("dilekce.txt", WITNESS_STATEMENT)
        app_module.backfill_language_coverage(self.conn)

        self.assertEqual(self.profile(doc_id)["language_coverage"]["language"], ENGLISH)
        self.assertEqual(self.row(doc_id)["language"], ENGLISH)
        self.assertNotEqual(self.row(doc_id)["external_llm_readiness"], EXTERNAL_LLM_ALLOWED)
        self.assertTrue(self.row(doc_id)["external_llm_readiness"].startswith("Blocked"))
        self.assertIn(
            LANGUAGE_CONDITION,
            self.profile(doc_id)["external_llm_gate"]["failed_conditions"],
        )

    def test_the_risk_badge_stops_saying_low(self) -> None:
        """A Low badge on a document just marked uncoverable is the same lie.

        The dashboard renders `documents.residual_risk` directly and the `?risk=`
        chip selects on it, so a backfill that settles the gate and leaves the
        column alone has moved the false green light rather than removed it.
        Re-analysed under the new coverage the same text returns Unknown, so the
        stored Low is a value the current rules would not produce.
        """
        doc_id = self.seed("dilekce.txt", WITNESS_STATEMENT)
        app_module.backfill_language_coverage(self.conn)

        self.assertEqual(self.row(doc_id)["residual_risk"], "Unknown")
        self.assertEqual(self.profile(doc_id)["residual_risk"]["level"], "Unknown")
        self.assertEqual(self.row(doc_id)["risk_summary"], self.profile(doc_id)["residual_risk"]["summary"])

    def test_a_covered_records_measured_level_is_left_exactly_as_indexed(self) -> None:
        """The rewrite is targeted at documents the rules cannot read.

        `residual_risk` is the detection-time figure the accuracy audit is
        calibrated on. Recomputing it here would silently re-measure the
        document against whatever its source file says today -- a file replaced
        on disk, or one that now truncates differently under the extraction
        cap, would move a stored level with no error and no audit entry. The
        seeded level deliberately differs from what a fresh analysis returns,
        so a rewrite of any kind is visible.
        """
        doc_id = self.seed("belge.txt", TURKISH_PETITION)
        stored_column = self.row(doc_id)["residual_risk"]
        stored_profile_level = self.profile(doc_id)["residual_risk"]["level"]
        self.assertNotEqual(
            stored_column,
            stored_profile_level,
            "the fixture must differ from a fresh analysis or this cannot detect a rewrite",
        )

        app_module.backfill_language_coverage(self.conn)

        self.assertTrue(self.profile(doc_id)["language_coverage"]["supported"])
        self.assertEqual(self.row(doc_id)["residual_risk"], stored_column)
        self.assertEqual(self.profile(doc_id)["residual_risk"]["level"], stored_profile_level)

    def test_a_turkish_record_is_settled_as_covered(self) -> None:
        doc_id = self.seed("belge.txt", TURKISH_PETITION)
        app_module.backfill_language_coverage(self.conn)

        self.assertTrue(self.profile(doc_id)["language_coverage"]["supported"])
        self.assertNotIn(
            LANGUAGE_CONDITION,
            self.profile(doc_id)["external_llm_gate"]["failed_conditions"],
        )

    def test_it_discards_no_reviewer_decision(self) -> None:
        """The whole reason it is not apply_reextraction()."""
        doc_id = self.seed("dilekce.txt", WITNESS_STATEMENT)
        before = self.conn.execute(
            "SELECT id, category, sample, review_status FROM privacy_findings WHERE document_id = ? ORDER BY id",
            (doc_id,),
        ).fetchall()
        self.assertTrue(before, "the fixture must record reviewer decisions to prove they survive")

        app_module.backfill_language_coverage(self.conn)

        after = self.conn.execute(
            "SELECT id, category, sample, review_status FROM privacy_findings WHERE document_id = ? ORDER BY id",
            (doc_id,),
        ).fetchall()
        self.assertEqual([tuple(row) for row in before], [tuple(row) for row in after])
        row = self.row(doc_id)
        self.assertEqual(row["redaction_completed"], 1)
        self.assertEqual(row["human_review_approved"], 1)
        self.assertEqual(row["review_status"], "approved_for_external_llm")

    def test_a_record_already_touched_by_a_write_path_is_still_reachable(self) -> None:
        """The backfill is a manual step, so ordinary use runs ahead of it.

        Any review, OCR or PDF action refreshes the release state, and that
        refresh used to stamp its own un-derived Unknown into the profile. The
        backfill skips a profile that already carries an answer, so a Turkish
        document the rules read perfectly was refused permanently, with no
        recovery short of `apply_reextraction()` -- which discards exactly the
        reviewer decisions this pass exists to preserve.
        """
        doc_id = self.seed("belge.txt", TURKISH_PETITION)
        app_module.refresh_document_state(self.conn, doc_id)
        self.assertIn(
            LANGUAGE_CONDITION,
            self.profile(doc_id)["external_llm_gate"]["failed_conditions"],
            "the refresh must still refuse it; only the backfill may clear it",
        )

        summary = app_module.backfill_language_coverage(self.conn)

        self.assertEqual(summary["examined"], 1)
        self.assertTrue(self.profile(doc_id)["language_coverage"]["supported"])
        self.assertNotIn(
            LANGUAGE_CONDITION,
            self.profile(doc_id)["external_llm_gate"]["failed_conditions"],
        )
        self.assertEqual(self.row(doc_id)["language"], TURKISH)

    def test_a_record_whose_file_is_gone_is_refused_and_left_alone(self) -> None:
        """Silence still fails toward refusal; nothing is guessed from the name.

        Refusing it costs nothing, so the pass writes nothing: the gate reads a
        missing coverage answer as uncovered by itself.
        """
        doc_id = self.seed("belge.txt", TURKISH_PETITION, write_file=False)
        before = dict(self.row(doc_id))
        summary = app_module.backfill_language_coverage(self.conn)

        self.assertEqual(summary["unreadable"], 1)
        self.assertIn(
            LANGUAGE_CONDITION,
            self.profile(doc_id)["external_llm_gate"]["failed_conditions"],
        )
        self.assertEqual(self.row(doc_id)["residual_risk"], before["residual_risk"])
        self.assertEqual(self.row(doc_id)["risk_summary"], before["risk_summary"])

    def test_it_answers_the_turkish_company_sheet_the_way_detection_does(self) -> None:
        """The backfill must not be more permissive than the detection path.

        It calls `detect_language` itself, so if it passes no entity spans it
        counts the marker words inside the employer's name and settles a wholly
        English sheet as Turkish -- reopening the defect on exactly the
        documents this pass exists to settle, and writing the wrong answer to
        disk where no later run will revisit it.
        """
        doc_id = self.seed("sheet.txt", ENGLISH_SHEET_NAMING_A_TURKISH_COMPANY)
        app_module.backfill_language_coverage(self.conn)

        detected = analyze_privacy("sheet.txt", ENGLISH_SHEET_NAMING_A_TURKISH_COMPANY)
        self.assertEqual(
            self.profile(doc_id)["language_coverage"]["language"],
            detected["language_coverage"]["language"],
        )
        self.assertFalse(self.profile(doc_id)["language_coverage"]["supported"])
        self.assertIn(
            LANGUAGE_CONDITION,
            self.profile(doc_id)["external_llm_gate"]["failed_conditions"],
        )

    def test_a_profile_with_no_findings_recorded_is_left_unanswered(self) -> None:
        """No spans means a more permissive answer, so it must not be settled.

        A profile that carries no `risk_map` at all cannot say where its
        entities are. Answering it anyway would count entity-internal markers;
        leaving it unanswered keeps it refused and lets a later run settle it.
        """
        doc_id = self.seed("sheet.txt", ENGLISH_SHEET_NAMING_A_TURKISH_COMPANY)
        stored = self.profile(doc_id)
        stored.pop("risk_map")
        self.conn.execute(
            "UPDATE documents SET privacy_profile = ? WHERE id = ?",
            (json.dumps(stored, ensure_ascii=False), doc_id),
        )

        summary = app_module.backfill_language_coverage(self.conn)

        self.assertEqual(summary["settled"], 0)
        self.assertNotIn("language_coverage", self.profile(doc_id))
        self.assertIn(
            LANGUAGE_CONDITION,
            self.profile(doc_id)["external_llm_gate"]["failed_conditions"],
        )

    def test_a_record_it_could_not_read_is_answerable_by_a_later_run(self) -> None:
        """An unreadable pass must not become a permanent answer.

        A corpus backfilled from a container that cannot see the source paths,
        or before a move is repaired, reads no text for any document. Recording
        Unknown there looks harmless -- the gate refuses either way -- but the
        loop skips any profile that already carries an answer, so every Turkish
        document in that corpus was frozen uncovered, recoverable only by
        `apply_reextraction()`, which discards the reviewer decisions this pass
        exists to preserve. Leaving the key absent keeps the refusal AND keeps
        the document reachable.

        Run one then run two is the whole point: the single-run state is
        identical either way.
        """
        doc_id = self.seed("belge.txt", TURKISH_PETITION, write_file=False)
        measured = self.row(doc_id)["residual_risk"]
        measured_summary = self.row(doc_id)["risk_summary"]

        first = app_module.backfill_language_coverage(self.conn)
        self.assertEqual(first["unreadable"], 1)
        self.assertEqual(first["settled"], 0, "nothing was settled: there was no text to read")
        self.assertNotIn(
            "language_coverage",
            self.profile(doc_id),
            "an unread document must not be recorded as answered",
        )
        self.assertIn(
            LANGUAGE_CONDITION,
            self.profile(doc_id)["external_llm_gate"]["failed_conditions"],
            "and must stay refused in the meantime",
        )
        self.assertEqual(
            self.row(doc_id)["residual_risk"],
            measured,
            "nor may its detection-time level be overwritten on a pass that read nothing",
        )

        (self.tmp_path / "belge.txt").write_text(TURKISH_PETITION, encoding="utf-8")
        second = app_module.backfill_language_coverage(self.conn)

        self.assertEqual(second["examined"], 1, "the second run must still reach it")
        self.assertEqual(self.profile(doc_id)["language_coverage"]["language"], TURKISH)
        self.assertEqual(
            self.row(doc_id)["residual_risk"],
            measured,
            "and the recovered document must not be left wearing an Unknown badge",
        )
        self.assertEqual(self.row(doc_id)["risk_summary"], measured_summary)
        self.assertNotIn(
            LANGUAGE_CONDITION,
            self.profile(doc_id)["external_llm_gate"]["failed_conditions"],
        )

    def test_running_it_again_changes_nothing(self) -> None:
        self.seed("dilekce.txt", WITNESS_STATEMENT)
        app_module.backfill_language_coverage(self.conn)
        settled = self.conn.execute("SELECT * FROM documents ORDER BY id").fetchall()

        summary = app_module.backfill_language_coverage(self.conn)

        self.assertEqual(summary, {"examined": 0, "settled": 0, "unreadable": 0})
        self.assertEqual(
            [tuple(row) for row in settled],
            [tuple(row) for row in self.conn.execute("SELECT * FROM documents ORDER BY id")],
        )

    def test_a_document_already_carrying_an_answer_is_left_alone(self) -> None:
        self.conn.execute(
            """
            INSERT INTO documents (filename, filepath, file_extension, extraction_status, privacy_profile)
            VALUES ('current.txt', ?, '.txt', 'Complete', ?)
            """,
            (
                str(self.tmp_path / "current.txt"),
                json.dumps(analyze_privacy("current.txt", TURKISH_PETITION), ensure_ascii=False),
            ),
        )
        summary = app_module.backfill_language_coverage(self.conn)
        self.assertEqual(summary["examined"], 0)


if __name__ == "__main__":
    unittest.main()
