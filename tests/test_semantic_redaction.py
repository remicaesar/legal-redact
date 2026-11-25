"""Turkish legal vocabulary is classified as law, not as sensitive content.

Three failures on the synthetic NDA (tests/fixtures/nda/), each a word read
without its legal context:

* "gizli bilgi", "gizlilik yükümlülüğü" and "ticari sır" triggered the
  privilege rule, which replaced the NDA's definition, exceptions and
  obligations with [PRIVILEGED_CONTENT_n_REDACTED]. Contractual
  confidentiality is not attorney-client privilege.
* "cezai şart" -- a contractual penalty -- triggered criminal_allegation on its
  adjective "cezai", so the penalty clause and its heading came back as
  [CRIMINAL_ALLEGATION_n_REDACTED].
* every institution shared the placeholder prefix COURT_AUTHORITY, so
  "Çankaya Vergi Dairesi" in a party block was delivered as a court.

Each class below pins one of those, and beside every "is not" sits the control
proving the rule still fires where it should: a fix that silenced the rule
outright would pass the negative half and fail the positive one.
"""

from __future__ import annotations

import unittest
from pathlib import Path

from legal_analyzer import legal_semantics, redaction_policy
from legal_analyzer.privacy import analyze_privacy, build_redacted_preview

FIXTURES = Path(__file__).parent / "fixtures"


def findings(text: str) -> list[dict]:
    return analyze_privacy("probe.txt", text)["risk_map"]


def of_category(text: str, category: str) -> list[dict]:
    return [finding for finding in findings(text) if finding["category"] == category]


class ContractualPenaltyIsNotCriminalTests(unittest.TestCase):
    PENALTY_CLAUSES = (
        "Gizlilik yükümlülüğünü ihlal eden taraf, diğer tarafa her bir ihlal için 250.000 TL cezai şart ödemeyi kabul eder.",
        "İhlal halinde ceza koşulu uygulanır ve taraf 50.000 TL öder.",
        "6. CEZAİ ŞART",
        "Cezai şart aşağıdaki hesaba ödenecektir:",
        "Cezai şartın tahsili için dava açılabilir.",
        "Sozlesmedeki cezai sart ihlal halinde odenir.",
        "Taraflar cezai koşulun indirilmesini talep edemez.",
    )

    def test_cezai_sart_and_ceza_kosulu_are_not_criminal_allegations(self) -> None:
        for clause in self.PENALTY_CLAUSES:
            with self.subTest(clause=clause):
                self.assertEqual(of_category(clause, "criminal_allegation"), [])

    def test_they_are_classified_as_contractual_penalties(self) -> None:
        for clause in self.PENALTY_CLAUSES:
            with self.subTest(clause=clause):
                kinds = {term.kind for term in legal_semantics.legal_terms(clause)}
                self.assertIn("CONTRACTUAL_PENALTY", kinds)

    def test_cezai_outside_the_contract_term_is_still_criminal(self) -> None:
        """The control: "cezai" is excluded only before şart / koşul."""
        clause = "Şüpheli hakkında cezai soruşturma başlatıldı."
        self.assertTrue(of_category(clause, "criminal_allegation"))
        self.assertTrue(of_category("Fail hakkında cezai takibat yapılacaktır.", "criminal_allegation"))

    def test_a_penalty_clause_cannot_hide_a_real_allegation_beside_it(self) -> None:
        """The exclusion is a lookahead on one word, not a carve-out of the clause."""
        text = "Sözleşmedeki cezai şart ve şüpheli hakkındaki suç duyurusu dosyaya eklendi."
        criminal = of_category(text, "criminal_allegation")
        self.assertTrue(criminal)
        self.assertTrue(any(f["sample"].startswith("şüpheli hakkındaki suç duyurusu") for f in criminal), criminal)

    def test_a_penalty_term_wrapped_at_a_line_break_is_still_a_penalty(self) -> None:
        """PDF extraction keeps line breaks wherever a line ended."""
        for head in ("cezai\nşart", "CEZAİ\nŞART", "cezai \n şart"):
            text = f"Taraflar arasındaki {head} hükmü uyarınca ihlal halinde 100.000 TL ödenecektir."
            with self.subTest(head=head):
                profile = analyze_privacy("nda.txt", text)
                self.assertEqual([f for f in profile["risk_map"] if f["category"] == "criminal_allegation"], [])
                self.assertIn("[PENALTY_AMOUNT_1]", profile["redacted_preview"])
                self.assertNotIn("100.000 TL", profile["redacted_preview"])
                self.assertFalse(profile["document_context"]["appears_to_contain_sensitive_data"])
                terms = legal_semantics.legal_terms(text)
                self.assertIn("CONTRACTUAL_PENALTY", {term.kind for term in terms})
                self.assertFalse(any("\n" in term.text for term in terms), terms)

    def test_the_penalty_amount_is_the_only_thing_replaced(self) -> None:
        clause = self.PENALTY_CLAUSES[0]
        preview = analyze_privacy("nda.txt", clause)["redacted_preview"]
        self.assertEqual(
            preview,
            "Gizlilik yükümlülüğünü ihlal eden taraf, diğer tarafa her bir ihlal için [PENALTY_AMOUNT_1] cezai şart ödemeyi kabul eder.",
        )


class TaxOfficeIsNotACourtTests(unittest.TestCase):
    def test_cankaya_vergi_dairesi_is_a_tax_authority(self) -> None:
        court = of_category("Vergi Dairesi: Çankaya Vergi Dairesi, VKN: 2222222224", "court_or_authority")
        self.assertEqual([(f["sample"], f["semantic_type"], f["placeholder"]) for f in court],
                         [("Çankaya Vergi Dairesi", "TAX_AUTHORITY", "[TAX_AUTHORITY_1]")])

    def test_kozyatagi_vergi_dairesi_is_typed_as_a_tax_authority(self) -> None:
        self.assertEqual(legal_semantics.institution_type("Kozyatağı Vergi Dairesi"), legal_semantics.TAX_AUTHORITY)


class LabelledTaxOfficeTests(unittest.TestCase):
    """The value of a "Vergi Dairesi:" field is the office's name.

    "Kozyatağı" is a neighbourhood the place list does not carry, so the list
    gate gave it back and the bare "Vergi Dairesi" left behind was dropped as a
    field label: the NDA delivered "Vergi Dairesi: Kozyatağı Vergi Dairesi"
    verbatim. When the value repeats the institution, the words before it are
    the name, listed or not.
    """

    def test_a_labelled_office_named_for_an_unlisted_place_is_one_tax_authority(self) -> None:
        for text, office in (
            ("Vergi Dairesi: Kozyatağı Vergi Dairesi, VKN: 1111111114", "Kozyatağı Vergi Dairesi"),
            ("VERGI DAIRESI: KOZYATAGI VERGI DAIRESI, VKN: 1111111114", "KOZYATAGI VERGI DAIRESI"),
            ("Vergi Dairesi: Büyük Mükellefler Vergi Dairesi Başkanlığı", "Büyük Mükellefler Vergi Dairesi Başkanlığı"),
        ):
            with self.subTest(text=text):
                court = of_category(text, "court_or_authority")
                self.assertEqual([(f["sample"], f["placeholder"]) for f in court], [(office, "[TAX_AUTHORITY_1]")])
                self.assertNotIn(office.split()[0], analyze_privacy("x.txt", text)["redacted_preview"])

    def test_two_labelled_offices_stay_two_pseudonyms(self) -> None:
        text = "Vergi Dairesi: Kozyatağı Vergi Dairesi\nVergi Dairesi: Çankaya Vergi Dairesi"
        self.assertEqual(
            [f["placeholder"] for f in of_category(text, "court_or_authority")], ["[TAX_AUTHORITY_1]", "[TAX_AUTHORITY_2]"]
        )

    def test_prose_is_unchanged(self) -> None:
        """Prose is not a field. The bare district form "Vergi Dairesi: Kadıköy"
        has no second institution word to anchor on; TaxOfficeFieldValueTests
        covers it."""
        self.assertEqual(of_category("Dosya Vergi Dairesi kayıtlarına bakıldı.", "court_or_authority"), [])

    def test_no_institution_carries_the_old_shared_prefix(self) -> None:
        text = (
            "Çankaya Vergi Dairesi, İstanbul Anadolu 4. Asliye Ticaret Mahkemesi, İstanbul 5. İcra Dairesi, "
            "Beyoğlu 24. Noterliği ve İstanbul Cumhuriyet Başsavcılığı kayıtları incelendi."
        )
        placeholders = [f["placeholder"] for f in of_category(text, "court_or_authority")]
        self.assertTrue(placeholders)
        self.assertFalse([p for p in placeholders if "COURT_AUTHORITY" in p], placeholders)



class TaxOfficeFieldValueTests(unittest.TestCase):
    """The value of a "Vergi Dairesi:" field is the office's name, on its own.

    "Satıcı ... A.Ş., Vergi Dairesi: Kadıköy, VKN: ..." delivered the district
    in cleartext beside a pseudonymised tax number: DIRECTORATE_RULE drops the
    bare label, and nothing else read the value after it. The finding is the
    value only -- the label stays -- and it ends where the value does, never
    swallowing the next field.
    """

    def assert_tax_office(self, text: str, value: str) -> None:
        court = of_category(text, "court_or_authority")
        self.assertEqual(
            [(f["sample"], f["placeholder"], f["semantic_type"]) for f in court],
            [(value, "[TAX_AUTHORITY_1]", "TAX_AUTHORITY")],
        )

    def test_the_value_after_the_label_is_one_tax_authority(self) -> None:
        for text, value in (
            ("Satıcı Demir Tekstil A.Ş., Vergi Dairesi: Kadıköy, VKN: 1111111114", "Kadıköy"),
            ("VERGİ DAİRESİ: KADIKÖY, VKN: 1111111114", "KADIKÖY"),
            ("VERGI DAIRESI: KADIKOY, VKN: 1111111114", "KADIKOY"),
            ("Vergi Dairesi : Büyük Mükellefler; VKN: 1111111114", "Büyük Mükellefler"),
            ("Vergi Dairesi: Veraset ve Harçlar\nVKN: 1111111114", "Veraset ve Harçlar"),
        ):
            with self.subTest(text=text):
                self.assert_tax_office(text, value)
                preview = analyze_privacy("x.txt", text)["redacted_preview"]
                self.assertNotIn(value, preview)
                self.assertRegex(preview, r"(?i)vergi dairesi ?: \[TAX_AUTHORITY_1\]")

    def test_the_value_ends_at_the_next_field(self) -> None:
        """No comma is needed: an identifier label, or any word followed by a
        colon, opens the next field and is not part of the office's name."""
        for text in (
            "Vergi Dairesi: Kadıköy VKN: 1111111114",
            "Vergi Dairesi: Kadıköy Vergi No: 1111111114",
            "Vergi Dairesi: Kadıköy Mersis No: 0123456789012345",
            "Vergi Dairesi: Kadıköy Şube: Moda",
            "Vergi Dairesi: Kadıköy Fatura No: 2026-001",
            "Vergi Dairesi: Kadıköy Fatura Tarihi: 01.10.2026",
        ):
            with self.subTest(text=text):
                self.assert_tax_office(text, "Kadıköy")

    def test_a_value_longer_than_the_cap_is_still_redacted(self) -> None:
        """A run past four words yields its bounded prefix, never no finding:
        refusing it shipped the district in cleartext."""
        text = "Vergi Dairesi: Kadıköy Müşteri Temsilcisi Ayşe Hanım Yılmaz"
        court = of_category(text, "court_or_authority")
        self.assertEqual([f["semantic_type"] for f in court], ["TAX_AUTHORITY"])
        self.assertTrue(court[0]["sample"].startswith("Kadıköy"), court)
        self.assertNotIn("Kadıköy", analyze_privacy("x.txt", text)["redacted_preview"])

    def test_a_label_without_a_value_is_still_not_a_finding(self) -> None:
        for text in (
            "Vergi Dairesi:",
            "Vergi Dairesi: -, VKN: 1111111114",
            "Vergi Dairesi: ilgili birime bildirilecektir.",
            "Vergi Dairesi ve Vergi No bilgileri eksiktir.",
        ):
            with self.subTest(text=text):
                self.assertEqual(of_category(text, "court_or_authority"), [])
        self.assert_tax_office("Vergi Dairesi: Kadıköy", "Kadıköy")

    def test_the_field_and_the_body_name_one_office(self) -> None:
        """Keyed as DIRECTORATE_RULE keys "Kadıköy Vergi Dairesi", so the
        party block and the body share a pseudonym and two offices stay two."""
        text = (
            "Vergi Dairesi: Kadıköy, VKN: 1111111114\n"
            "Vergi Dairesi: Çankaya, VKN: 2222222224\n"
            "Beyanname Kadıköy Vergi Dairesi kaydındadır."
        )
        self.assertEqual(
            [(f["sample"], f["placeholder"]) for f in of_category(text, "court_or_authority")],
            [("Kadıköy", "[TAX_AUTHORITY_1]"), ("Çankaya", "[TAX_AUTHORITY_2]"), ("Kadıköy Vergi Dairesi", "[TAX_AUTHORITY_1]")],
        )

class InstitutionTypeTests(unittest.TestCase):
    """Typed on the full expression, never on a generic head word like "Dairesi"."""

    CASES = (
        ("Asliye Ticaret Mahkemesi", legal_semantics.COMMERCIAL_COURT),
        ("İstanbul Anadolu 4. Asliye Ticaret Mahkemesi", legal_semantics.COMMERCIAL_COURT),
        ("Ankara 5. Asliye Hukuk Mahkemesi", legal_semantics.COURT),
        ("Yargıtay 9. Hukuk Dairesi", legal_semantics.COURT),
        ("Danıştay Vergi Dava Daireleri", legal_semantics.COURT),
        ("Ankara 2. Vergi Mahkemesi", legal_semantics.COURT),
        ("İcra Hukuk Mahkemesi", legal_semantics.COURT),
        ("İcra Dairesi", legal_semantics.ENFORCEMENT_OFFICE),
        ("İstanbul 5. İcra Dairesi", legal_semantics.ENFORCEMENT_OFFICE),
        ("12. İcra Müdürlüğü", legal_semantics.ENFORCEMENT_OFFICE),
        ("Vergi Dairesi", legal_semantics.TAX_AUTHORITY),
        ("Beşiktaş Vergi Dairesi Müdürlüğü", legal_semantics.TAX_AUTHORITY),
        ("Beyoğlu 24. Noterliği", legal_semantics.NOTARY),
        ("İstanbul Cumhuriyet Başsavcılığı", legal_semantics.PROSECUTOR_OFFICE),
        ("IZMIR CUMHURIYET BASSAVCILIGI", legal_semantics.PROSECUTOR_OFFICE),
        ("Kadıköy İlçe Emniyet Müdürlüğü", legal_semantics.LAW_ENFORCEMENT_AUTHORITY),
        ("Rekabet Kurumu", legal_semantics.REGULATOR),
        ("Kişisel Verileri Koruma Kurumu", legal_semantics.REGULATOR),
        ("İstanbul Tahkim Merkezi", legal_semantics.ARBITRAL_INSTITUTION),
        ("Şişli Tapu Müdürlüğü", legal_semantics.ADMINISTRATIVE_AUTHORITY),
        ("İstanbul Valiliği", legal_semantics.ADMINISTRATIVE_AUTHORITY),
    )

    def test_each_expression_gets_its_type(self) -> None:
        for expression, expected in self.CASES:
            with self.subTest(expression=expression):
                self.assertEqual(legal_semantics.institution_type(expression), expected)

    def test_the_detected_finding_is_pseudonymised_under_its_type(self) -> None:
        for sentence, sample, prefix in (
            ("Dava İstanbul Anadolu 4. Asliye Ticaret Mahkemesi önündedir.", "İstanbul Anadolu 4. Asliye Ticaret Mahkemesi", "COMMERCIAL_COURT"),
            ("Takip İstanbul 5. İcra Dairesi dosyasındadır.", "İstanbul 5. İcra Dairesi", "ENFORCEMENT_OFFICE"),
            ("Beyanname Çankaya Vergi Dairesi kaydındadır.", "Çankaya Vergi Dairesi", "TAX_AUTHORITY"),
            ("Vekaletname Beyoğlu 24. Noterliği onaylıdır.", "Beyoğlu 24. Noterliği", "NOTARY"),
        ):
            with self.subTest(sample=sample):
                by_sample = {f["sample"]: f for f in of_category(sentence, "court_or_authority")}
                self.assertEqual(by_sample[sample]["placeholder"], f"[{prefix}_1]")
                self.assertEqual(by_sample[sample]["semantic_type"], prefix)

    def test_a_generic_match_is_typed_by_its_institution_word_not_its_sweep(self) -> None:
        """The generic rule sweeps up to 120 characters past "Kurumu"; the court
        named inside that sweep must not decide what the regulator is. The court
        here carries no branch number, so no period stops the sweep short of it
        and no named-court rule claims it."""
        finding = of_category("Rekabet Kurumu kararı Vergi Mahkemesi önüne getirildi.", "court_or_authority")[0]
        self.assertIn("Mahkemesi", finding["sample"])
        self.assertTrue(finding["sample"].startswith("Kurumu"), finding)
        self.assertEqual(finding["semantic_type"], legal_semantics.REGULATOR)


class ConfidentialityIsNotPrivilegeTests(unittest.TestCase):
    NOT_PRIVILEGE = (
        "Gizli bilgi; ticari sırlar, müşteri listeleri, fiyatlandırma bilgileri, kaynak kodları ve teknik çizimleri kapsar.",
        "Taraflar gizlilik yükümlülüğüne uyacaktır.",
        "Taraflar ticari sır niteliğindeki bilgileri üçüncü kişilerle paylaşmayacaktır.",
        "HİZMET SÖZLEŞMESİ - GİZLİ",
        "Confidential Information means all trade secrets disclosed under this NDA.",
        "The receiving party shall comply with its confidentiality obligation.",
        "Bu metin hukuki tavsiye veya kullanılabilir bir dilekçe değildir.",
        "Avukat Ayşe Kaya sözleşmeyi imzaladı.",
        # A filed petition's inflected client word, with no advice in its clause.
        "Müvekkile ödenmesi gereken bedel bugüne kadar ödenmemiştir.",
    )

    def test_contractual_confidentiality_is_not_flagged_as_privilege(self) -> None:
        for text in self.NOT_PRIVILEGE:
            with self.subTest(text=text):
                self.assertEqual(of_category(text, "privileged_or_confidential"), [])

    def test_the_retired_gold_label_is_now_a_negative(self) -> None:
        """commercial_contract.txt carried a privileged_or_confidential gold label
        on "ticari sır" -- an NDA obligation. The label was removed from gold/;
        this keeps the reason from silently coming back."""
        text = (FIXTURES / "commercial_contract.txt").read_text(encoding="utf-8")
        self.assertEqual([f["sample"] for f in findings(text) if f["category"] == "privileged_or_confidential"], [])

    def test_privilege_evidence_is_still_flagged_and_typed(self) -> None:
        for text, expected_type in (
            ("Müvekkil ile yapılan görüşmede dava stratejisi belirlenmiştir.", "LITIGATION_STRATEGY"),
            ("PRIVILEGED AND CONFIDENTIAL - prepared for the purpose of these proceedings.", "ATTORNEY_CLIENT_PRIVILEGE"),
            ("Avukat Ayşe Kaya hukuki görüşünü bildirdi.", "LEGAL_ADVICE"),
            ("Müvekkil Yıldız Ltd. Şti. ile görüşme tutanağı düzenlendi.", "ATTORNEY_CLIENT_COMMUNICATION"),
        ):
            with self.subTest(text=text):
                privileged = of_category(text, "privileged_or_confidential")
                self.assertTrue(privileged, findings(text))
                self.assertEqual({f["semantic_type"] for f in privileged}, {expected_type})
                self.assertTrue(all(f["risk"] == "CRITICAL" for f in privileged))

    def test_an_uncorroborated_lawyer_mention_does_not_hide_a_marker_in_its_sweep(self) -> None:
        """A skipped "Avukat" resumes the scan at the end of the WORD, not of its
        160-character sweep -- otherwise "müvekkil" inside it is never offered."""
        text = "Avukat Ayşe Kaya sözleşmeyi imzaladı, müvekkil dosyası ektedir."
        privileged = of_category(text, "privileged_or_confidential")
        self.assertEqual([f["sample"] for f in privileged], ["müvekkil dosyası ektedir"])

    def test_document_context_reports_privilege_and_confidentiality_apart(self) -> None:
        nda = (FIXTURES / "nda" / "mock_nda_tr.txt").read_text(encoding="utf-8")
        profile = analyze_privacy("nda.txt", nda)
        context = profile["document_context"]
        self.assertFalse(context["appears_privileged"])
        self.assertTrue(context["appears_commercially_confidential"])
        # "6. CEZAİ ŞART" lower-cases to "cezai" plus a combining dot; the
        # penalty heading must not read as criminal content either.
        self.assertFalse(context["appears_to_contain_sensitive_data"])
        checklist = " ".join(profile["human_review_checklist"])
        self.assertNotIn("attorney-client privilege, professional secrecy", checklist)
        self.assertIn("Contractual confidentiality is not attorney-client privilege", checklist)


class TypedValuesTests(unittest.TestCase):
    def test_amounts_are_typed_by_their_clause(self) -> None:
        text = (
            "Taraf her ihlal için 250.000 TL cezai şart öder. "
            "Tarafların sorumluluğu 1.500.000 TL ile sınırlıdır. "
            "Aylık hizmet bedeli 85.000 TL olarak ödenir."
        )
        amounts = {f["sample"]: (f["semantic_type"], f["placeholder"]) for f in of_category(text, "money_amount")}
        self.assertEqual(amounts, {
            "250.000 TL": ("PENALTY_AMOUNT", "[PENALTY_AMOUNT_1]"),
            "1.500.000 TL": ("LIABILITY_CAP", "[LIABILITY_CAP_1]"),
            "85.000 TL": ("AMOUNT", "[AMOUNT_1]"),
        })

    def test_one_value_in_two_roles_keeps_one_neutral_placeholder(self) -> None:
        """The export replaces every occurrence of a value with ONE string, so a
        cap label on the fee would be delivered wherever the fee appears."""
        text = "Tarafların sorumluluğu 1.000.000 TL ile sınırlıdır. Hizmet bedeli 1.000.000 TL olarak ödenir."
        amounts = of_category(text, "money_amount")
        self.assertEqual({f["placeholder"] for f in amounts}, {"[AMOUNT_1]"})
        self.assertEqual([f["semantic_type"] for f in amounts], ["LIABILITY_CAP", "AMOUNT"])

    def test_dates_are_typed_and_durations_are_not_findings(self) -> None:
        text = (
            "Doğum tarihi: 01.02.1980. Sözleşme 14.03.2026 tarihinde imzalanmıştır. "
            "Bildirim en geç 15.04.2026 tarihine kadar yapılır. "
            "Gizlilik yükümlülüğü sözleşmenin sona ermesinden sonra beş yıl daha devam eder."
        )
        dates = {f["sample"]: (f["semantic_type"], f["placeholder"]) for f in of_category(text, "date")}
        self.assertEqual(dates, {
            "01.02.1980": ("BIRTH_DATE", "[BIRTH_DATE_1]"),
            "14.03.2026": ("SIGNATURE_DATE", "[DATE_1]"),
            "15.04.2026": ("DEADLINE", "[DATE_2]"),
        })
        self.assertFalse([f for f in findings(text) if "beş yıl" in f["sample"]])

    def test_a_repeated_date_keeps_its_placeholder(self) -> None:
        text = "Sözleşme 14.03.2026 tarihinde imzalanmıştır. Ekler 14.03.2026 tarihinde teslim edilmiştir."
        self.assertEqual({f["placeholder"] for f in of_category(text, "date")}, {"[DATE_1]"})


class ReferentialConsistencyTests(unittest.TestCase):
    def test_a_defined_short_name_takes_the_companys_placeholder(self) -> None:
        text = (
            'Kestane Yazılım Anonim Şirketi (bundan sonra "Kestane" olarak anılacaktır) ile anlaşılmıştır. '
            "Kestane adına irtibat kişisi atanmıştır. KESTANE imza atmıştır.\n"
            "Hesap sahibi: Kestane Yazılım Anonim Şirketi"
        )
        companies = of_category(text, "company_name")
        self.assertEqual(
            sorted(f["sample"] for f in companies),
            sorted(["Kestane Yazılım Anonim Şirketi"] * 2 + ["Kestane"] * 2 + ["KESTANE"]),
        )
        self.assertEqual({f["placeholder"] for f in companies}, {"[COMPANY_1]"})
        preview = analyze_privacy("nda.txt", text)["redacted_preview"]
        self.assertNotIn("Kestane", preview)
        self.assertNotIn("KESTANE", preview)

    def test_a_definition_wrapped_across_lines_is_still_read(self) -> None:
        """The text layer of the NDA's PDF, line breaks where the page broke them."""
        text = (
            'bir tarafta Kestane Yazılım Anonim Şirketi (bundan\nsonra "Kestane" olarak anılacaktır) ile diğer '
            'tarafta Defne Lojistik Limited Şirketi (bundan sonra "Defne"\nolarak anılacaktır) arasında.\n'
            "Kestane adına irtibat kişisi; Defne adına irtibat kişisi."
        )
        companies = of_category(text, "company_name")
        self.assertEqual(sorted({f["sample"] for f in companies}), sorted({
            "Kestane Yazılım Anonim Şirketi", "Defne Lojistik Limited Şirketi", "Kestane", "Defne",
        }))
        for finding in companies:
            self.assertNotIn("\n", finding["sample"])
        preview = analyze_privacy("nda.pdf", text)["redacted_preview"]
        self.assertNotIn("Kestane", preview)
        self.assertNotIn("Defne", preview)

    def test_a_defined_role_stays_readable(self) -> None:
        """"Satıcı" is the legal relationship, not an identity."""
        text = (
            'Demir Tekstil Anonim Şirketi (bundan sonra "Satıcı" olarak anılacaktır) ile anlaşılmıştır. '
            "Satıcı teslimden sorumludur."
        )
        self.assertNotIn("Satıcı", [f["sample"] for f in findings(text)])
        self.assertIn("Satıcı teslimden sorumludur.", analyze_privacy("x.txt", text)["redacted_preview"])

    def test_a_labelled_and_a_bare_identifier_are_one_placeholder(self) -> None:
        mersis = of_category("MERSİS No: 0111111111400015\nŞirket 0111111111400015 numarasıyla kayıtlıdır.", "mersis_number")
        self.assertEqual({f["placeholder"] for f in mersis}, {"[MERSIS_1]"})
        cases = of_category("Esas No: 2025/318 dosyası ile 2025/318 Esas sayılı dosya aynıdır.", "case_or_investigation_number")
        self.assertEqual([f["sample"] for f in cases], ["Esas No: 2025/318", "2025/318"])
        self.assertEqual({f["placeholder"] for f in cases}, {"[CASE_NUMBER_1]"})

    def test_a_role_word_beside_a_name_survives(self) -> None:
        preview = analyze_privacy("x.txt", "Temsilcisi: Selin Arıkan, Genel Müdür")["redacted_preview"]
        self.assertEqual(preview, "Temsilcisi: [PERSON_1], Genel Müdür")


class NdaProvisionClassificationTests(unittest.TestCase):
    """classify_provisions reads the NDA's clauses as the provisions they are."""

    def setUp(self) -> None:
        self.text = (FIXTURES / "nda" / "mock_nda_tr.txt").read_text(encoding="utf-8")
        self.provisions = legal_semantics.classify_provisions(self.text)

    def types_of(self, needle: str) -> set[str]:
        start = self.text.index(needle)
        return set(legal_semantics.provision_types_at(self.provisions, start, start + len(needle)))

    def test_each_reported_clause_is_classified(self) -> None:
        for needle, expected in (
            ("Gizli bilgi; ticari sırlar", "CONFIDENTIALITY_DEFINITION"),
            ("gizli bilgi sayılmaz", "CONFIDENTIALITY_EXCEPTION"),
            ("yalnızca proje değerlendirmesi amacıyla kullanacak", "PURPOSE_LIMITATION"),
            ("bilmesi gereken çalışanlar", "NEED_TO_KNOW"),
            ("iki yıl süreyle yürürlükte kalır", "CONTRACT_DURATION"),
            ("sona ermesinden sonra beş yıl", "SURVIVAL_CLAUSE"),
            ("250.000 TL cezai şart", "CONTRACTUAL_PENALTY"),
            ("Türk hukuku uygulanır", "GOVERNING_LAW"),
            ("İstanbul Anadolu Mahkemeleri ile İcra Daireleri yetkilidir", "JURISDICTION"),
        ):
            with self.subTest(clause=expected):
                self.assertIn(expected, self.types_of(needle))

    def test_the_profile_stores_counts_and_no_text(self) -> None:
        profile = analyze_privacy("nda.txt", self.text)
        summary = profile["legal_provisions"]
        self.assertEqual(set(summary), {"clauses", "by_type", "treatment"})
        self.assertEqual(summary["treatment"], redaction_policy.PRESERVE)
        self.assertGreater(summary["by_type"]["CONTRACTUAL_CONFIDENTIALITY"], 3)
        self.assertTrue(all(isinstance(count, int) for count in summary["by_type"].values()))

    def test_clause_splitting_stays_linear_on_a_long_unbroken_line(self) -> None:
        """A single line of abbreviations that never end a sentence -- extraction
        without line breaks produces exactly this. Searching back to the clause
        start for the token before each period made this quadratic: 157 s for
        200,000 characters, against 0.5 s now. 60,000 characters keeps the
        quadratic shape well past the bound and the linear one far inside it."""
        import time

        text = "A.Ş. No. 5 Ltd. Şti. " * 2700
        started = time.monotonic()
        legal_semantics.classify_provisions(text)
        analyze_privacy("long.txt", text)
        self.assertLess(time.monotonic() - started, 5.0)

    def test_no_new_finding_sample_runs_across_a_line(self) -> None:
        """The DOCX invariant tests/test_redaction_parity.py holds for the gold fixtures."""
        for finding in findings(self.text):
            self.assertNotIn("\n", finding["sample"], finding)


class RetiringATriggerDoesNotClearALanguageTests(unittest.TestCase):
    """Fewer redaction targets must never mean more Turkish evidence.

    Every finding's span is excluded from the language signal's Turkish-word
    count. The retired privilege sweeps used to cover the law-firm names behind
    "Avukat X," -- "Kaya ve Ortakları Hukuk Bürosu" -- and with them gone the
    "ve" of each firm counted as Turkish: this wholly English form went from
    refused to TR and "Allowed for external LLM use" with a London address, a UK
    mobile, a national insurance number and a health line still in it.
    """

    FORM = (
        "WITNESS DETAILS FORM\n\n"
        "Witness name: Marigold Quillfeather\n"
        "Address: 179 Cobblewell Street, London EC2A 4NE\n"
        "Mobile: 07700 900318\n"
        "National insurance number: QQ 12 34 56 C\n"
        "Health: currently on medication for stress\n"
        "Claimant lawyer: Avukat Mehmet Kaya, Kaya ve Ortakları Hukuk Bürosu\n"
        "Respondent lawyer: Avukat Selim Demir, Demir ve Ortakları Hukuk Bürosu\n"
        "Witness lawyer: Avukat Leyla Arslan, Arslan ve Ortakları Hukuk Bürosu\n"
    )

    def test_the_form_is_not_read_as_turkish_and_stays_refused_after_review(self) -> None:
        from legal_analyzer.privacy import refresh_release_state

        profile = analyze_privacy("form.txt", self.FORM)
        self.assertNotEqual(profile["language_coverage"]["language"], "TR")
        reviewed = refresh_release_state(
            profile, redaction_completed=True, human_review_approved=True,
            unresolved_critical_count=0, direct_identifiers_remaining=False, remaining_findings=[],
        )
        self.assertFalse(reviewed["external_llm_gate"]["allowed"])

    def test_the_retired_sweeps_still_shadow_the_language_count(self) -> None:
        """The guarantee itself, independent of the law-firm rule: with only the
        retired vocabulary's sweeps excluded, the firm names carry no Turkish."""
        from legal_analyzer.classifier import _marker_evidence
        from legal_analyzer.privacy import language_evidence_spans

        turkish, _english, _run = _marker_evidence(self.FORM, language_evidence_spans(self.FORM, []))
        self.assertEqual(turkish, 0)


class NarrowedContextRulesCoverEverySweepTests(unittest.TestCase):
    """The union of every accepted trigger's own sweep, not a finditer() chain.

    finditer() over "trigger + 160-character sweep" is non-overlapping, so where
    the chain ends depends on which triggers exist: once "cezai şart" stopped
    being a trigger, the chain in this sentence began at "suç", swallowed
    "şüpheli", and ended before the prior conviction that the old chain's second
    match (starting at "şüpheli") had covered.
    """

    # The geometry the old chain and a narrowed chain disagree on: "suç" lies
    # inside the old "cezai" sweep, "şüpheli" just past its end. The old chain
    # was [cezai, +160) then [şüpheli, +160); a chain started at "suç" swallows
    # "şüpheli" and stops 80 characters short, before the prior conviction and
    # the Turkish words after it.
    SENTENCE = (
        "Sözleşmeye aykırılık halinde cezai şart ödenir ve teslim gecikmesinde usuller ayrıca belirlenmiş olup "
        "taraflardan birinin suç teşkil eden eylemleri nedeniyle iş ilişkisi dışında kalan işlemlerinde şüpheli "
        "sıfatıyla ifadesi alınan muhasebe çalışanının daha önce iki kez hırsızlıktan hüküm giydiği hususu ile "
        "ilgili olarak bu dosya ve ayrıca bir tutanak eklenmiştir."
    )

    def assert_every_sweep_is_covered(self, text: str, rule, trigger_re) -> None:
        import re

        spans = [(f["start"], f["end"]) for f in findings(text)]
        sweep = re.compile(rule.pattern, rule.flags)
        checked = 0
        for trigger in trigger_re.finditer(text):
            match = sweep.match(text, trigger.start())
            for offset in range(trigger.start(), match.end()):
                # Word characters: the space or bracket left between a segment
                # and an identifier cut out of it was never part of a finding.
                if not text[offset].isalnum():
                    continue
                checked += 1
                self.assertTrue(any(a <= offset < b for a, b in spans), (trigger.group(), offset, text[offset:offset + 20]))
        self.assertGreater(checked, 100)

    def test_the_prior_conviction_after_a_penalty_clause_is_covered(self) -> None:
        from legal_analyzer.privacy import _CONTEXT_TRIGGER_RES, CRIMINAL_RULE

        self.assert_every_sweep_is_covered(self.SENTENCE, CRIMINAL_RULE, _CONTEXT_TRIGGER_RES["criminal_allegation"])
        preview = analyze_privacy("x.txt", self.SENTENCE)["redacted_preview"]
        self.assertNotIn("hırsızlıktan hüküm giydiği", preview)
        self.assertIn("cezai şart ödenir", preview)

    def test_every_privilege_sweep_is_covered_on_a_long_line(self) -> None:
        from legal_analyzer.privacy import _CONTEXT_TRIGGER_RES, PRIVILEGE_RULE

        text = (
            "Avukat Ayşe Kaya ile yapılan görüşmede tedarik ilişkisinin geleceği ve taraflar arasındaki teslim "
            "takviminin yeniden düzenlenmesi ele alınmış olup müvekkil ile kararlaştırılan dava stratejisi uyarınca "
            "karşı tarafa yapılacak bildirimlerin içeriği ve zamanlaması ile tanıkların dinlenme sırası ayrıca "
            "belirlenmiştir."
        )
        self.assert_every_sweep_is_covered(text, PRIVILEGE_RULE, _CONTEXT_TRIGGER_RES["privileged_or_confidential"])

    def test_the_language_signal_is_never_looser_than_the_old_chains(self) -> None:
        """The old chains, written out literally, measured against the product's spans."""
        import re

        from legal_analyzer.classifier import _marker_evidence
        from legal_analyzer.privacy import _CONTEXT_SWEEP_CHAR, language_evidence_spans

        old_triggers = (
            r"\b(?:suç|suc|şüpheli|supheli|sanık|sanik|cezai|kamu davası|arama|el koyma|soruşturma|sorusturma)\b",
            r"\b(?:müvekkil|muvekkil|vekil|av\.|avukat|attorney-client|privileged|gizli|confidential|ticari sır|trade secret)\b",
        )
        for text in (self.SENTENCE, self.SENTENCE.replace("cezai şart", "gizli bilgi ihlalinde cezai şart")):
            with self.subTest(text=text[:40]):
                old = [m.span() for t in old_triggers for m in re.finditer(t + _CONTEXT_SWEEP_CHAR + "{0,160}", text, re.I)]
                head = language_evidence_spans(text, analyze_privacy("x.txt", text)["risk_map"])
                self.assertLessEqual(_marker_evidence(text, head)[0], _marker_evidence(text, old)[0])

    def test_lawyer_words_on_one_long_line_stay_linear(self) -> None:
        """Corroboration is indexed once per document: rescanning each clause per
        trigger took 35 s for 120 KB of this line."""
        import time

        text = "avukat Mehmet sözleşmeyi imzaladı ve " * 1600
        started = time.monotonic()
        analyze_privacy("long.txt", text)
        self.assertLess(time.monotonic() - started, 5.0)


class LawFirmNamesTests(unittest.TestCase):
    def test_a_law_firm_is_a_company_identifier_typed_as_counsel(self) -> None:
        text = "Davacı vekili Avukat Mehmet Kaya (Kaya ve Ortakları Hukuk Bürosu) tarafından dilekçe sunulmuştur."
        firms = [f for f in of_category(text, "company_name") if f.get("semantic_type") == "LAW_FIRM"]
        self.assertEqual([(f["sample"], f["placeholder"]) for f in firms], [("Kaya ve Ortakları Hukuk Bürosu", "[LAW_FIRM_1]")])
        self.assertEqual((firms[0]["treatment"], firms[0]["replacement"]), (redaction_policy.PSEUDONYMIZE, "[LAW_FIRM_1]"))
        preview = analyze_privacy("x.txt", text)["redacted_preview"]
        self.assertNotIn("Kaya", preview)

    def test_other_forms_and_inflections(self) -> None:
        """Inside a law-firm finding, not equal to one: like the company rule,
        this one cannot tell a sentence-initial "Dosya" from a surname and takes
        it too -- one ordinary word over-redacted, never the firm name missed."""
        for text, firm in (
            ("Dosya Demir Avukatlık Ortaklığı tarafından takip edilmektedir.", "Demir Avukatlık Ortaklığı"),
            ("YILMAZ & ARSLAN HUKUK BÜROSU adına imzalanmıştır.", "YILMAZ & ARSLAN HUKUK BÜROSU"),
            ("Yazı Kaya Hukuk Bürosu'nun antetli kağıdına yazılmıştır.", "Kaya Hukuk Bürosu"),
            ("Avukat Mehmet Kaya (Kaya, Demir ve Ortakları Hukuk Bürosu) dilekçe verdi.", "Kaya, Demir ve Ortakları Hukuk Bürosu"),
            ("Avukat Mehmet Kaya (Kaya - Demir Hukuk Bürosu) dilekçe verdi.", "Kaya - Demir Hukuk Bürosu"),
            ("Avukat Mehmet Kaya (Kaya Law Firm) dilekçe verdi.", "Kaya Law Firm"),
            ("Dosya Kaya Hukuk Danışmanlık tarafından hazırlanmıştır.", "Kaya Hukuk Danışmanlık"),
        ):
            with self.subTest(text=text):
                start = text.index(firm)
                firms = [f for f in of_category(text, "company_name") if f.get("semantic_type") == "LAW_FIRM"]
                self.assertTrue(any(f["start"] <= start and start + len(firm) <= f["end"] for f in firms), firms)
                self.assertTrue(all("'" not in f["sample"] for f in firms))


class NoTrimmedWordIsGivenBackToNobodyTests(unittest.TestCase):
    def test_a_lawyer_word_in_front_of_an_address_stays_inside_a_finding(self) -> None:
        """An uncorroborated "Avukat" reports nothing, so the address keeps it."""
        for text in (
            "Tebligat adresi: Avukat Gül Sokak No: 5 Kadıköy İstanbul olarak bildirilmiştir.",
            "Avukat Ahmet Sokak No: 3 adresinde müvekkil ile görüşülmüştür.",
        ):
            with self.subTest(text=text):
                spans = [(f["start"], f["end"]) for f in findings(text)]
                start = text.index("Avukat")
                for offset in range(start, start + len("Avukat")):
                    self.assertTrue(any(a <= offset < b for a, b in spans), (offset, findings(text)))


class CounselAssessmentIsPrivilegeTests(unittest.TestCase):
    def test_an_assessment_and_an_inflected_client_word_are_flagged(self) -> None:
        for text in (
            "Avukat olarak kanaatimiz şudur: dava açılması halinde tazminat ödemeye mahkum edilme ihtimali yüksektir.",
            "Müvekkilimizin talimatı doğrultusunda ihtarname gönderilmiştir.",
            "Müvekkilimize verilen tavsiye doğrultusunda sulh teklif edilmemiştir.",
        ):
            with self.subTest(text=text):
                privileged = of_category(text, "privileged_or_confidential")
                self.assertTrue(privileged)
                self.assertTrue(all(f["risk"] == "CRITICAL" for f in privileged))


class APleadingIsNotPrivilegeTests(unittest.TestCase):
    """A filed petition speaks about the client in exactly the words an
    assessment uses -- "aleyhine", "kanaatindeyiz" -- and a filing is not
    privileged. An inflected client word needs communication, advice or
    strategy wording beside it; assessment wording corroborates only a word
    that names counsel."""

    def test_petition_sentences_are_not_privilege(self) -> None:
        for text in (
            "Davalı şirket, müvekkilimiz aleyhine kira alacağı nedeniyle icra takibi başlatmış, "
            "müvekkilimiz süresi içinde borca itiraz etmiştir.",
            "Müvekkilimizin haklı olduğu kanaatindeyiz.",
            "Müvekkile ödenmesi gereken bedelin tahsil ihtimali bulunmaktadır.",
        ):
            with self.subTest(text=text):
                self.assertEqual(of_category(text, "privileged_or_confidential"), [])


class UnsafeAliasesAreNotOfferedTests(unittest.TestCase):
    """The export replaces an approved sample wherever it occurs, case- and
    diacritic-insensitively and widened to the whole word."""

    def test_a_short_name_inside_other_words_is_not_offered(self) -> None:
        text = 'Ak Enerji Anonim Şirketi (bundan sonra "Ak" olarak anılacaktır) hakları saklıdır. Ak imzalamıştır.'
        self.assertEqual([f["sample"] for f in of_category(text, "company_name")], ["Ak Enerji Anonim Şirketi"])
        preview = analyze_privacy("x.txt", text)["redacted_preview"]
        for word in ("olarak", "anılacaktır", "hakları", "saklıdır"):
            self.assertIn(word, preview)

    def test_a_short_name_that_is_also_an_ordinary_word_is_not_offered(self) -> None:
        text = 'Ay Yapı Anonim Şirketi ("Ay") bedeli her ay sonunda öder.'
        self.assertNotIn("Ay", [f["sample"] for f in of_category(text, "company_name")])

    def test_a_role_swallowed_by_the_company_match_is_not_a_name(self) -> None:
        text = 'Kiracı Defne Lojistik Limited Şirketi (bundan sonra "Kiracı" olarak anılacaktır) kirayı öder.'
        self.assertNotIn("Kiracı", [f["sample"] for f in of_category(text, "company_name")])

    def test_one_ordinary_use_of_the_word_does_not_drop_the_company(self) -> None:
        """Refusing on ANY collateral occurrence silently dropped the coreference
        the moment the word appeared anywhere else -- here, as a chestnut."""
        text = (
            'Kestane Yazılım Anonim Şirketi (bundan sonra "Kestane" olarak anılacaktır) ile anlaşılmıştır. '
            "Kestane adına irtibat kişisi atanmıştır. Toplantıda kestane ikram edilmiştir."
        )
        samples = [f["sample"] for f in of_category(text, "company_name")]
        self.assertEqual(samples.count("Kestane"), 2)
        self.assertNotIn("Kestane adına", analyze_privacy("x.txt", text)["redacted_preview"])

    def test_a_safe_alias_beside_an_email_carrying_it_is_still_offered(self) -> None:
        text = (
            'Kestane Yazılım Anonim Şirketi (bundan sonra "Kestane" olarak anılacaktır), '
            "e-posta hukuk@kestane.example.com. Kestane imzalar."
        )
        self.assertEqual([f["sample"] for f in of_category(text, "company_name")].count("Kestane"), 2)


class CaseNumberRegistersStayApartTests(unittest.TestCase):
    def test_the_same_digits_under_two_registers_are_two_numbers(self) -> None:
        text = "Esas No: 2025/318, Karar No: 2025/318, Soruşturma No: 2025/318 ve 2025/318 Esas sayılı dosya; 2025/318 Karar."
        placeholders = [f["placeholder"] for f in of_category(text, "case_or_investigation_number")]
        self.assertEqual(placeholders, ["[CASE_NUMBER_1]", "[CASE_NUMBER_2]", "[CASE_NUMBER_3]", "[CASE_NUMBER_1]", "[CASE_NUMBER_2]"])

    def test_a_register_typed_without_diacritics_is_the_same_register(self) -> None:
        text = "Soruşturma No: 2025/318 ve Sorusturma No: 2025/318 aynı dosyadır."
        placeholders = {f["placeholder"] for f in of_category(text, "case_or_investigation_number")}
        self.assertEqual(placeholders, {"[CASE_NUMBER_1]"})


class PreviewUsesThePolicyReplacementTests(unittest.TestCase):
    def test_a_review_finding_is_removed_as_a_passage_and_an_identifier_by_its_placeholder(self) -> None:
        text = "Şüpheli hakkında suç duyurusu var. TCKN 10000000146."
        preview = analyze_privacy("x.txt", text)["redacted_preview"]
        self.assertIn("[CRIMINAL_ALLEGATION_1_REDACTED]", preview)
        self.assertIn("[NATIONAL_ID_1]", preview)
        self.assertNotIn("[NATIONAL_ID_1_REDACTED]", preview)

    def test_a_legacy_finding_without_a_policy_decision_keeps_the_old_rule(self) -> None:
        legacy = [{
            "category": "health_data", "sample": "hasta", "risk": "CRITICAL",
            "recommended_action": "Flag for human legal review", "placeholder": "[SENSITIVE_HEALTH_DATA_1]",
            "start": 0, "end": 5,
        }]
        self.assertEqual(build_redacted_preview("hasta", legacy), "[SENSITIVE_HEALTH_DATA_1_REDACTED]")


if __name__ == "__main__":
    unittest.main()
