"""Identifiers carried by contracts, enforcement (icra) files and notarial documents.

Each class pins one identifier that the paperwork lawyers exchange most carries
and that no rule found before: a phone number with its area code in brackets, a
company's trade-registry number (reported as a lawyer's bar registration), a
land-registry parcel, a notary's journal (yevmiye) number without a year, the
notary signing by name, an enforcement office with no branch number, and a party
named on a labelled line ("Borçlu:", "Keşideci:", "Hizmet Veren:") whose given
name the gazetteer does not hold. One more pins a false positive the enforcement
file exposed: "Alacaklı Vekili Av. ..." reported "Vekili Av" as a person.

The given names here -- Ekin, Nehir -- are deliberately outside
legal_analyzer/turkish_names.GIVEN_NAMES, so a finding cannot come from the
gazetteer instead of the path under test. Every party, office and number is
invented; phones use the reserved ranges in eval/README.md.

The last class runs the three practice samples in samples/documents/ through
the real review and export endpoints and reads the delivered DOCX, because a
finding that is detected but cannot be removed is not a redaction.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

from legal_analyzer.docx_redactor import RedactionTarget, resolve_redaction_regions
from legal_analyzer.privacy import DIRECT_IDENTIFIER_CATEGORIES, analyze_privacy
from legal_analyzer.turkish_names import tr_fold
from tests.test_court_full_span import ExportHarness

SAMPLES_DIR = Path(__file__).resolve().parents[1] / "samples" / "documents"

# Enough ordinary Turkish around a line for the document to read as Turkish,
# so a test never depends on the language gate.
_PROSE = (
    "Taraflar arasındaki ilişki bu belgede özetlenmiştir ve her iki taraf da "
    "bu bilgilerin doğru olduğunu kabul etmektedir."
)


def findings(text: str) -> list[dict]:
    return analyze_privacy("belge.txt", f"{_PROSE}\n{text}\n{_PROSE}")["risk_map"]


def of(text: str, category: str) -> list[dict]:
    return [finding for finding in findings(text) if finding["category"] == category]


def samples(text: str, category: str) -> list[str]:
    return [finding["sample"] for finding in of(text, category)]


class BracketedPhoneNumberTests(unittest.TestCase):
    def test_a_bracketed_area_code_is_a_phone_number(self):
        for line, number in (
            ("Telefon: (0299) 412 35 80", "(0299) 412 35 80"),
            ("Borçlu Telefonu: (0522) 604 17 38", "(0522) 604 17 38"),
            ("Tel: +90 (522) 604 17 38", "+90 (522) 604 17 38"),
            ("Tel: 0 (299) 412 35 80", "0 (299) 412 35 80"),
        ):
            with self.subTest(line=line):
                self.assertEqual(samples(line, "phone_number"), [number])

    def test_bracketed_and_plain_forms_of_one_number_share_a_pseudonym(self):
        placeholders = {
            finding["placeholder"]
            for finding in of("Telefon: (0299) 412 35 80\nFaks yerine: 0299 412 35 80", "phone_number")
        }
        self.assertEqual(len(placeholders), 1, placeholders)

    def test_a_bracketed_number_that_is_not_a_phone_stays_out(self):
        for line in ("Ödeme (2026) yılında 412 35 80 kuruş olarak yapılmıştır.", "Madde (12) 345 67 89"):
            with self.subTest(line=line):
                self.assertEqual(samples(line, "phone_number"), [])


class TradeRegistryNumberTests(unittest.TestCase):
    def test_a_trade_registry_number_is_its_own_category(self):
        for line, sample in (
            ("Ticaret Sicil No: 48213", "Ticaret Sicil No: 48213"),
            ("Ticaret Sicili Numarası: 7731", "Ticaret Sicili Numarası: 7731"),
            ("Tic. Sicil No: 512", "Tic. Sicil No: 512"),
        ):
            with self.subTest(line=line):
                self.assertEqual(samples(line, "trade_registry_number"), [sample])
                self.assertEqual(samples(line, "bar_registration_number"), [])

    def test_the_label_split_by_any_blank_is_still_a_trade_registry_number(self):
        for line in ("Ticaret  Sicil No: 48213", "Ticaret\nSicil No: 48213", "TİCARET\tSİCİL NO: 48213", "Tic.\nSicil No: 48213"):
            with self.subTest(line=line):
                found = of(line, "trade_registry_number")
                self.assertEqual(len(found), 1, found)
                self.assertIn("48213", found[0]["sample"])
                self.assertNotIn("\n", found[0]["sample"])
                self.assertEqual(samples(line, "bar_registration_number"), [])

    def test_a_number_on_the_next_line_is_still_reported(self):
        """The bar rule reported this layout before it handed the shape over."""
        found = of("Ticaret Sicil No:\n48213 olarak kayıtlıdır.", "trade_registry_number")
        self.assertEqual(len(found), 1, found)
        self.assertIn("48213", found[0]["sample"])

    def test_a_bar_registration_is_still_a_bar_registration(self):
        for line in ("Masalkent Barosu Sicil No: 40517", "Baro Sicil No: 40517", "personel sicil numarası 884411"):
            with self.subTest(line=line):
                self.assertEqual(len(samples(line, "bar_registration_number")), 1)
                self.assertEqual(samples(line, "trade_registry_number"), [])

    def test_moving_it_out_of_bar_registration_kept_it_a_direct_identifier(self):
        """The number counted against the gate as a bar registration, so it must still."""
        self.assertIn("trade_registry_number", DIRECT_IDENTIFIER_CATEGORIES)
        profile = analyze_privacy("belge.txt", f"{_PROSE}\nTicaret Sicil No: 48213\n{_PROSE}")
        self.assertIn("trade_registry_number", {finding["category"] for finding in profile["risk_map"]})
        self.assertTrue(profile["external_llm_gate"]["direct_identifiers_detected"])

    def test_one_registration_written_two_ways_is_one_pseudonym(self):
        placeholders = {
            finding["placeholder"]
            for finding in of("Ticaret Sicil No: 48213\nTic. Sicil No: 48213", "trade_registry_number")
        }
        self.assertEqual(len(placeholders), 1, placeholders)


class LandRegistryParcelTests(unittest.TestCase):
    FORMS = (
        ("Masalkent, 1417 ada, 23 parsel sayılı taşınmaz", "1417 ada, 23 parsel"),
        ("Tapu kaydında Ada: 1417 Parsel: 23 olarak geçmektedir.", "Ada: 1417 Parsel: 23"),
        ("Ada No: 1417, Parsel No: 23", "Ada No: 1417, Parsel No: 23"),
        ("taşınmaz 1417/23 ada/parsel olarak kayıtlıdır", "1417/23 ada/parsel"),
        ("ada/parsel: 1417/23", "ada/parsel: 1417/23"),
        ("1417 Ada 23 Parseldeki bağımsız bölüm", "1417 Ada 23 Parsel"),
    )

    def test_each_written_form_of_a_parcel_is_found(self):
        for line, sample in self.FORMS:
            with self.subTest(line=line):
                self.assertEqual(samples(line, "land_registry_parcel"), [sample])

    def test_every_form_of_one_parcel_is_one_pseudonym(self):
        text = "\n".join(line for line, _sample in self.FORMS)
        placeholders = {finding["placeholder"] for finding in of(text, "land_registry_parcel")}
        self.assertEqual(len(of(text, "land_registry_parcel")), len(self.FORMS))
        self.assertEqual(len(placeholders), 1, placeholders)

    def test_two_parcels_are_two_pseudonyms(self):
        placeholders = {
            finding["placeholder"]
            for finding in of("1417 ada, 23 parsel ve 1417 ada, 24 parsel", "land_registry_parcel")
        }
        self.assertEqual(len(placeholders), 2, placeholders)

    def test_either_word_alone_is_not_a_parcel(self):
        for line in (
            "Toplantı Büyükada'da 3 kez yapılmıştır.",
            "ada 12 kez ziyaret edilmiştir",
            "23 parsel üzerinde inceleme yapılmıştır",
        ):
            with self.subTest(line=line):
                self.assertEqual(samples(line, "land_registry_parcel"), [])


class NotaryJournalNumberTests(unittest.TestCase):
    def test_a_journal_number_without_a_year_is_found(self):
        for line, sample in (
            ("Yevmiye No: 04512", "Yevmiye No: 04512"),
            ("Yevmiye: 4512", "Yevmiye: 4512"),
            ("Yevmiye Numarası: 04512", "Yevmiye Numarası: 04512"),
            ("İşbu ihtarname 04512 yevmiye numarası ile düzenlenmiştir.", "04512 yevmiye"),
            ("04512 yevmiye sayılı vekaletname", "04512 yevmiye"),
        ):
            with self.subTest(line=line):
                self.assertEqual(samples(line, "case_or_investigation_number"), [sample])

    def test_the_header_and_the_endorsement_share_one_pseudonym(self):
        text = "Yevmiye No: 04512\nİşbu ihtarname 4512 yevmiye numarası ile düzenlenmiştir."
        placeholders = {finding["placeholder"] for finding in of(text, "case_or_investigation_number")}
        self.assertEqual(len(of(text, "case_or_investigation_number")), 2)
        self.assertEqual(len(placeholders), 1, placeholders)

    def test_redacting_a_cited_journal_number_leaves_other_numbers_alone(self):
        """The redaction target is the sample, matched as a string everywhere it occurs."""
        text = (
            f"{_PROSE}\nMasalkent 7. Noterliğinin 15.03.2026 tarih ve 12 yevmiye numarası ile "
            "düzenlediği ihtarname, 2012 yılında imzalanan sözleşmenin madde 12 hükmüne dayanır.\n"
        )
        cited = [f for f in analyze_privacy("belge.txt", text)["risk_map"] if f["category"] == "case_or_investigation_number"]
        self.assertEqual(len(cited), 1, cited)
        targets = [RedactionTarget(text=cited[0]["sample"], replacement=cited[0]["placeholder"])]
        redacted = list(text)
        for start, end, replacement, _ in reversed(resolve_redaction_regions(text, targets)):
            redacted[start:end] = replacement
        redacted = "".join(redacted)
        self.assertNotIn("12 yevmiye", redacted)
        self.assertIn("2012 yılında", redacted)
        self.assertIn("madde 12 hükmüne", redacted)

    def test_the_dated_form_is_unchanged(self):
        self.assertEqual(
            samples("Yevmiye No: 2026/18821", "case_or_investigation_number"), ["Yevmiye No: 2026/18821"]
        )

    def test_a_count_of_journal_books_is_not_a_number(self):
        self.assertEqual(samples("Noterlikte 2 yevmiye defteri tutulmaktadır.", "case_or_investigation_number"), [])


class NotarySigningByNameTests(unittest.TestCase):
    ENDORSEMENT = "Masalkent 7. Noteri Nehir Ayyıldız / Mühür ve imza yoktur."

    def test_the_notary_and_the_office_are_both_found(self):
        self.assertEqual(samples(self.ENDORSEMENT, "court_or_authority"), ["Masalkent 7. Noteri"])
        self.assertEqual(samples(self.ENDORSEMENT, "natural_person_name"), ["Nehir Ayyıldız"])

    def test_a_notary_whose_given_name_is_also_a_day_is_still_found(self):
        """"Cuma" is a weekday and a given name; only endorsement words reject a capture."""
        self.assertEqual(
            samples("Masalkent 7. Noteri Cuma Pusulacı / Mühür ve imza yoktur.", "natural_person_name"),
            ["Cuma Pusulacı"],
        )

    def test_an_endorsement_after_the_notary_is_not_a_person(self):
        self.assertEqual(samples("Beyoğlu 24. Noteri Tarafından Onaylanmıştır.", "natural_person_name"), [])

    def test_the_notary_and_the_notarys_office_are_one_venue(self):
        text = f"MASALKENT 7. NOTERLİĞİ\n{self.ENDORSEMENT}"
        notaries = of(text, "court_or_authority")
        self.assertEqual(len(notaries), 2, notaries)
        self.assertEqual({finding["placeholder"] for finding in notaries}, {"[NOTARY_1]"})


class UnnumberedEnforcementOfficeTests(unittest.TestCase):
    def test_an_office_named_for_its_caseload_is_found_with_its_city(self):
        for line, sample in (
            ("Takip Masalkent Genel İcra Dairesi nezdinde açılmıştır.", "Masalkent Genel İcra Dairesi"),
            ("Masalkent Banka Alacakları İcra Dairesi dosyası", "Masalkent Banka Alacakları İcra Dairesi"),
            ("Masalkent İcra Dairesinin dosyasında haciz yapılmıştır.", "Masalkent İcra Dairesi"),
        ):
            with self.subTest(line=line):
                found = of(line, "court_or_authority")
                self.assertEqual([finding["sample"] for finding in found], [sample])
                self.assertTrue(found[0]["placeholder"].startswith("[ENFORCEMENT_OFFICE_"), found[0])

    def test_a_numbered_office_is_still_one_finding(self):
        self.assertEqual(samples("MASALKENT 4. İCRA DAİRESİ", "court_or_authority"), ["MASALKENT 4. İCRA DAİRESİ"])

    def test_the_bare_name_of_a_kind_of_office_is_not_an_office(self):
        self.assertEqual(samples("Ödeme İcra Dairesi hesabına yapılır.", "court_or_authority"), [])


class BlankRunsAreRejectedInLinearTimeTests(unittest.TestCase):
    """A keyword followed by a long run of blanks and no number.

    The first versions of the parcel, journal-number and trade-registry rules
    chained three optional blank runs, and rejecting 3,000 spaces after "ada"
    took over 30 seconds -- on the detection path of every uploaded document.
    """

    def test_each_new_rule_rejects_a_long_blank_run_quickly(self):
        import time

        for keyword in ("ada", "Yevmiye", "Ticaret Sicil", "ada/parsel", "ada 12 parsel"):
            with self.subTest(keyword=keyword):
                started = time.perf_counter()
                analyze_privacy("belge.txt", f"{keyword}{' ' * 2000}x")
                self.assertLess(time.perf_counter() - started, 2.0)


class LabelledPartyLineTests(unittest.TestCase):
    def test_a_party_named_on_a_labelled_line_is_found(self):
        for line in (
            "Borçlu: Ekin Rüzgarlı",
            "Alacaklı: Ekin Rüzgarlı",
            "Keşideci: Ekin Rüzgarlı",
            "Muhatap: Ekin Rüzgarlı",
            "Hizmet Veren: Ekin Rüzgarlı",
            "1.2. Hizmet Veren: Ekin Rüzgarlı",
            "Kiracı: Ekin Rüzgarlı",
            "Davacı: Ekin Rüzgarlı",
            "BORÇLU: EKİN RÜZGARLI",
        ):
            with self.subTest(line=line):
                self.assertEqual([tr_fold(name) for name in samples(line, "natural_person_name")], ["ekin rüzgarlı"])

    def test_a_company_on_a_labelled_line_is_not_a_person(self):
        for line in (
            "Alacaklı: MASAL KUTUSU ÖRNEK ANONİM ŞİRKETİ",
            "1.1. Hizmet Alan: MASAL KUTUSU ÖRNEK ANONİM ŞİRKETİ",
        ):
            with self.subTest(line=line):
                self.assertEqual(samples(line, "natural_person_name"), [])

    def test_a_person_followed_by_other_fields_is_still_found(self):
        for line, name in (
            ("Muhatap: Arda Pusulacı, TCKN: 10000000146", "Arda Pusulacı"),
            ("Hakim: Zorlu Pusulacı Adres: Masal Mah.", "Zorlu Pusulacı"),
            ("Hizmet Alan: Zorlu Pusulacı Adres: Masal Mah.", "Zorlu Pusulacı"),
            ("Hizmet Alan: Ekin Rüzgarlı Tarih: 01.10.2026", "Ekin Rüzgarlı"),
            ("Muhatap: Zorlu Pusulacı Tarih: 01.01.2026", "Zorlu Pusulacı"),
            ("Borçlu: Ekin Rüzgarlı Telefon: (0522) 604 17 38", "Ekin Rüzgarlı"),
            ("Muhatap: Arda Pusulacı Daire: 7", "Arda Pusulacı"),
            ("Katip: Zorlu Pusulacı Daire: 3", "Zorlu Pusulacı"),
            ("Kiracı: Zorlu Pusulacı Sicil No: 4", "Zorlu Pusulacı"),
            ("Satıcı: Zorlu Pusulacı Telefon: 0525", "Zorlu Pusulacı"),
            ("Hakim: Zorlu Pusulacı\tAdres: Masal Mah.", "Zorlu Pusulacı"),
        ):
            with self.subTest(line=line):
                self.assertIn(name, samples(line, "natural_person_name"))

    def test_a_person_followed_by_their_own_affiliation_is_still_found(self):
        for line in (
            "Vekil: Zorlu Pusulacı İstanbul Barosu Sicil No: 12345",
            "Bilirkişi: Zorlu Pusulacı Masal Üniversitesi",
            "Yetkili: Zorlu Pusulacı Masal Gıda A.Ş.",
            "Temsilci: Zorlu Pusulacı\tMasal Gıda Ltd. Şti.",
        ):
            with self.subTest(line=line):
                self.assertTrue(
                    any("Zorlu Pusulacı" in sample for sample in samples(line, "natural_person_name")),
                    samples(line, "natural_person_name"),
                )

    def test_a_field_that_merely_starts_with_a_role_names_no_one(self):
        """The label is the role word itself, directly before the colon."""
        self.assertEqual(samples("Borçlu Mesleği: Serbest Muhasebeci", "natural_person_name"), [])


class CounselIsNotAPersonCalledVekiliTests(unittest.TestCase):
    def test_role_vekili_av_is_not_a_name(self):
        for line in ("Alacaklı Vekili Av. Ekin Düşsel", "Davacı Vekili Av. Ekin Düşsel dilekçesini sunmuştur."):
            with self.subTest(line=line):
                self.assertEqual(samples(line, "natural_person_name"), ["Ekin Düşsel"])


class PracticeSampleExportTests(ExportHarness):
    """Every identifier the samples demonstrate is gone from the delivered DOCX."""

    SECRETS = {
        "turkish_contract.docx": (
            "Ekin Rüzgarlı", "Selin Yıldızsoy", "50732413432", "46197065378", "0420187039924771",
            "8072234647", "48213", "412 35 80", "318 44 90", "TR02 0099", "Gökkuşağı", "Hayal Mahallesi",
            "hukuk@masalkutusu", "ekin.ruzgarli",
        ),
        "turkish_enforcement_file.docx": (
            "Arda Pusulacı", "Ekin Düşsel", "44703184674", "8072234647", "40517", "604 17 38",
            "TR16 0099", "99 ZZ 1234", "1417", "2026/1873", "Fener Sokak", "İCRA DAİRESİ", "İcra Dairesi",
        ),
        "turkish_notary_notice.docx": (
            "Deniz Kurgusal", "Arda Pusulacı", "Ekin Düşsel", "Nehir Ayyıldız", "10757999748",
            "44703184674", "40517", "04512", "1417", "TR30 0099", "NOTERLİĞİ", "Noteri", "Fener Sokak",
        ),
    }

    def test_each_samples_identifiers_are_removed_from_the_reviewed_docx(self):
        for name, secrets in self.SECRETS.items():
            with self.subTest(sample=name):
                self.install_document(SAMPLES_DIR / name)
                runs = self.exported_runs()
                delivered = "".join(runs)
                self.assertIn("SENTETİK ÖRNEK", delivered, "the export lost the document rather than redacting it")
                for secret in secrets:
                    self.assertNotIn(secret, delivered, f"{secret!r} ships in the reviewed {name}")
                # No finding sample in a shipped sample may cross a line: one that
                # did could never be removed from a DOCX part.
                for finding in self.profile["risk_map"]:
                    self.assertNotIn("\n", finding["sample"], finding)


if __name__ == "__main__":
    unittest.main()
