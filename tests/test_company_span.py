"""A company finding's span begins at the company name, not at the clause.

The company_name rule is a lazy body behind a case-sensitive capital, so the
leftmost match started at the EARLIEST capital within ninety characters of the
legal-form suffix: in "Sözleşme gereği Davalı taraf olan yıldızlı örnek holding a.ş."
the finding was the whole clause. That span is the redaction target, so the
reviewed export replaced "Sözleşme gereği Davalı taraf olan" along with the
company, and the clause text became the pseudonym key, giving one company a
different placeholder in every sentence. `privacy._company_start` now walks back
from the suffix over the name and stops at the first word that is not part of
it.

These tests assert offsets, because the gold audit matches a label against a
finding by substring in either direction and therefore cannot see a span that is
too wide. Every company and person named here is invented.
"""

import unittest
from pathlib import Path

from legal_analyzer.privacy import analyze_privacy

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "company_span_clauses.txt"


def company_findings(text: str) -> list[tuple[int, int, str]]:
    profile = analyze_privacy("company_span.txt", text)
    return [
        (finding["start"], finding["end"], finding["sample"])
        for finding in profile["risk_map"]
        if finding["category"] == "company_name"
    ]


def expected(text: str, name: str) -> tuple[int, int, str]:
    start = text.index(name)
    return (start, start + len(name), name)


class CompanySpanStartsAtTheNameTests(unittest.TestCase):
    """The four probes from the issue, one sentence each, on exact offsets."""

    PROBES = {
        # A lowercase company: the clause words before it are Turkish grammar.
        "Sözleşme gereği Davalı taraf olan yıldızlı örnek holding a.ş. ödemeyi yapmadı.": "yıldızlı örnek holding a.ş.",
        # A capitalised company after a lowercase clause word.
        "Taraflar arasında Ankara'da imzalanan sözleşmeye göre Mavi Deniz Lojistik Ltd. Şti. borçludur.":
            "Mavi Deniz Lojistik Ltd. Şti.",
        "Bu nedenle Davacı ile Kuzey Yapı Anonim Şirketi arasında imzalanan sözleşme feshedilmiştir.":
            "Kuzey Yapı Anonim Şirketi",
        # A party-role word directly in front of the name.
        "Alacaklı Yıldız LTD. ŞTİ. temsilcisi toplantıya katıldı.": "Yıldız LTD. ŞTİ.",
    }

    def test_each_probe_spans_exactly_the_company(self) -> None:
        for text, name in self.PROBES.items():
            with self.subTest(text=text):
                self.assertEqual(company_findings(text), [expected(text, name)])

    def test_the_gold_fixture_spans_exactly_each_company(self) -> None:
        text = FIXTURE.read_text(encoding="utf-8")
        names = [
            "yıldızlı örnek holding a.ş.",
            "Mavi Deniz Lojistik Ltd. Şti.",
            "Kuzey Yapı Anonim Şirketi",
            "Yıldız LTD. ŞTİ.",
            "Çınaraltı Gıda Sanayi ve Ticaret A.Ş.",
        ]
        self.assertEqual(company_findings(text), [expected(text, name) for name in names])

    def test_the_clause_words_stay_readable_in_the_preview(self) -> None:
        profile = analyze_privacy("company_span.txt", FIXTURE.read_text(encoding="utf-8"))
        preview = profile["redacted_preview"]
        for kept in (
            "Sözleşme gereği [PARTY_ROLE_1] taraf olan [COMPANY_1] ödemeyi yapmadı.",
            "Taraflar arasında Ankara'da imzalanan sözleşmeye göre [COMPANY_2] borçludur.",
            "Bu nedenle [PARTY_ROLE_2] ile [COMPANY_3] arasında",
            "[PARTY_ROLE_3] [COMPANY_4] temsilcisi",
            "Ayrıca dosyaya göre [COMPANY_5] ile ilgili",
        ):
            with self.subTest(kept=kept):
                self.assertIn(kept, preview)

    def test_one_company_keeps_one_pseudonym_across_clauses(self) -> None:
        """The clause is no longer the pseudonym key."""
        text = (
            "Sözleşme gereği Mavi Deniz Lojistik Ltd. Şti. ödemeyi yapmadı. "
            "Bu nedenle Davacı ile Mavi Deniz Lojistik Ltd. Şti. arasında uzlaşma sağlanamadı."
        )
        profile = analyze_privacy("company_span.txt", text)
        placeholders = {
            finding["placeholder"] for finding in profile["risk_map"] if finding["category"] == "company_name"
        }
        self.assertEqual(placeholders, {"[COMPANY_1]"})

    def test_a_sentence_opening_connective_stays_out_of_the_span(self) -> None:
        """'Ayrıca' is capitalised only because it opens the sentence."""
        first = "Taraflar arasında imzalanan sözleşmeye göre Mavi Deniz Lojistik Ltd. Şti. borçludur. "
        for opener in ("Ayrıca", "Ancak", "Dolayısıyla", "AYRICA"):
            text = first + opener + " Mavi Deniz Lojistik Ltd. Şti. ödeme planını da reddetmiştir."
            with self.subTest(opener=opener):
                second = text.index("Mavi", len(first))
                name = "Mavi Deniz Lojistik Ltd. Şti."
                self.assertEqual(
                    company_findings(text),
                    [expected(text, name), (second, second + len(name), name)],
                )
                profile = analyze_privacy("company_span.txt", text)
                placeholders = {
                    finding["placeholder"] for finding in profile["risk_map"] if finding["category"] == "company_name"
                }
                self.assertEqual(placeholders, {"[COMPANY_1]"})


class CompanyNameStaysWholeTests(unittest.TestCase):
    """Narrowing must never cut into the name itself: that would ship it."""

    WHOLE = (
        # A lowercase connector inside a title-case name.
        ("Rapora göre Çınaraltı Gıda Sanayi ve Ticaret A.Ş. ile görüşülmüştür.", "Çınaraltı Gıda Sanayi ve Ticaret A.Ş."),
        # An uppercase connector inside an uppercase name.
        ("TUNA GIDA SANAYİ VE TİCARET ANONİM ŞİRKETİ genel kurulu toplandı.", "TUNA GIDA SANAYİ VE TİCARET ANONİM ŞİRKETİ"),
        # An ampersand and a comma inside a firm name.
        ("We act for Penhallow, Grieve & Marsh LLP in this matter.", "Penhallow, Grieve & Marsh LLP"),
        # A lowercase preposition inside an English name.
        ("We act for the Centre for Effective Dispute Resolution Limited in this matter.",
         "Centre for Effective Dispute Resolution Limited"),
        # Lowercase prepositions inside English names.
        ("We act for Back to Basics Limited in this matter.", "Back to Basics Limited"),
        # A lowercase abbreviation inside a lowercase name.
        ("Davalı mavi inş. turizm a.ş. ödemeyi yapmadı.", "mavi inş. turizm a.ş."),
        # An article inside a lowercase name, behind a party-role word.
        ("Davalı bir numara gıda a.ş. ödemeyi yapmadı.", "bir numara gıda a.ş."),
        # An acronym in front of a title-case word.
        ("Davalı ABC Gıda Ltd. Şti. temsilcisi geldi.", "ABC Gıda Ltd. Şti."),
        # A company that is named for a person keeps the person's name.
        ("Rapora göre Selin Akbaş İnşaat Ltd. Şti. tarafından ödeme yapılmıştır.", "Selin Akbaş İnşaat Ltd. Şti."),
    )

    # Words the walk cannot tell from the name. With no grammar word or clause
    # punctuation to stop on, it keeps them: the finding may be wider than the
    # name, but never shorter.
    UNPROVEN_BOUNDARIES = (
        # A lowercase word inside a capitalised name.
        ("Bu konuda Kumsalpazarı e-çarşı Elektronik Hizmetler A.Ş. ile görüşüldü.", "Kumsalpazarı e-çarşı Elektronik Hizmetler A.Ş."),
        # A title-case word in front of an all-caps part of the same name.
        ("Bu konuda Yeşilvadi ÖRNEK BANKASI A.Ş. ile görüşüldü.", "Yeşilvadi ÖRNEK BANKASI A.Ş."),
        # A lowercase preposition inside a name, behind an unknown lowercase verb.
        ("Our client is Made with Love Ltd and it disputes the claim.", "Made with Love Ltd"),
        # An unknown lowercase word in front of a capitalised name.
        ("Sözleşme uyarınca ödeme Selin Akbaş İnşaat Ltd. Şti. tarafından yapılmıştır.", "Selin Akbaş İnşaat Ltd. Şti."),
    )

    def test_an_unsuffixed_name_before_ile_is_not_left_uncovered(self) -> None:
        for text, covered in (
            ("Kaya Holding ile Mavi Deniz Ltd. Şti. sözleşme imzaladı.", "Kaya Holding ile Mavi Deniz Ltd. Şti."),
            ("ABC İNŞAAT ile XYZ LTD. ŞTİ. arasında", "ABC İNŞAAT ile XYZ LTD. ŞTİ."),
        ):
            with self.subTest(text=text):
                self.assertEqual(company_findings(text), [expected(text, covered)])

    def test_ile_behind_a_person_or_party_role_ends_the_name(self) -> None:
        for text in (
            "Ahmet Yılmaz ile Mavi Deniz Ltd. Şti. sözleşme imzaladı.",
            "Davacı ile Mavi Deniz Ltd. Şti. sözleşme imzaladı.",
        ):
            with self.subTest(text=text):
                self.assertEqual(company_findings(text), [expected(text, "Mavi Deniz Ltd. Şti.")])

    def test_the_whole_name_stays_inside_the_finding(self) -> None:
        for text, name in self.WHOLE:
            with self.subTest(text=text):
                self.assertIn(expected(text, name), company_findings(text))

    def test_an_unproven_boundary_is_not_a_place_to_cut(self) -> None:
        for text, name in self.UNPROVEN_BOUNDARIES:
            with self.subTest(text=text):
                name_start, name_end, _ = expected(text, name)
                self.assertTrue(
                    any(start <= name_start and name_end <= end for start, end, _ in company_findings(text)),
                    company_findings(text),
                )

    def test_a_suffix_with_no_name_in_front_keeps_the_original_span(self) -> None:
        """Nothing name-like is left after the role word, so nothing is trimmed.

        Fails safe: a span that cannot be narrowed to a name is reported as
        wide as the rule matched it, never dropped.
        """
        text = "Bu dosyada Davalı Ltd. Şti. adına kimse gelmedi."
        self.assertEqual(company_findings(text), [expected(text, "Bu dosyada Davalı Ltd. Şti.")])


class OtherPartiesStayOutOfTheCompanySpanTests(unittest.TestCase):
    def test_a_person_joined_by_a_connector_is_not_welded_on(self) -> None:
        text = "This agreement is made on 14 September 2026 between Thomas Whitacre and Halvorsen Example Logistics Inc."
        self.assertEqual(company_findings(text), [expected(text, "Halvorsen Example Logistics Inc.")])

    def test_a_pair_opened_outside_the_match_still_parts_the_person(self) -> None:
        text = "made between Thomas Whitacre and Halvorsen Example Logistics Inc."
        self.assertEqual(company_findings(text), [expected(text, "Halvorsen Example Logistics Inc.")])

    def test_a_firm_named_for_its_partner_stays_one_company(self) -> None:
        text = "Selin Akbaş ve Ortakları Ltd. Şti. ile Mehmet Kaya ve Ortakları Ltd. Şti. borçludur."
        first = "Selin Akbaş ve Ortakları Ltd. Şti."
        second_start = text.index("Mehmet Kaya ve Ortakları Ltd. Şti.")
        second = (second_start, second_start + len("Mehmet Kaya ve Ortakları Ltd. Şti."), "Mehmet Kaya ve Ortakları Ltd. Şti.")
        self.assertEqual(company_findings(text), [expected(text, first), second])
        profile = analyze_privacy("company_span.txt", text)
        placeholders = [
            finding["placeholder"] for finding in profile["risk_map"] if finding["category"] == "company_name"
        ]
        self.assertEqual(len(set(placeholders)), 2, placeholders)

    def test_a_firm_named_for_its_partner_stays_whole_after_between(self) -> None:
        for text, firms in (
            (
                "This agreement is made between John Smith and Sons Limited and Peter Brown and Sons Limited.",
                ("John Smith and Sons Limited", "Peter Brown and Sons Limited"),
            ),
            (
                "Bu sözleşme Selin Akbaş ve Ortakları Ltd. Şti. ile Mehmet Kaya ve Ortakları Ltd. Şti. arasında imzalanmıştır.",
                ("Selin Akbaş ve Ortakları Ltd. Şti.", "Mehmet Kaya ve Ortakları Ltd. Şti."),
            ),
            (
                "between Selin Akbaş ve Ortakları Ltd. Şti. and Mehmet Kaya Kardeşler Ltd. Şti.",
                ("Selin Akbaş ve Ortakları Ltd. Şti.", "Mehmet Kaya Kardeşler Ltd. Şti."),
            ),
        ):
            with self.subTest(text=text):
                expected_spans = []
                cursor = 0
                for firm in firms:
                    start = text.index(firm, cursor)
                    expected_spans.append((start, start + len(firm), firm))
                    cursor = start + len(firm)
                self.assertEqual(company_findings(text), expected_spans)
                profile = analyze_privacy("company_span.txt", text)
                placeholders = [
                    finding["placeholder"] for finding in profile["risk_map"] if finding["category"] == "company_name"
                ]
                self.assertEqual(len(set(placeholders)), 2, placeholders)

    def test_two_partner_firms_after_between_in_separate_clauses_keep_distinct_pseudonyms(self) -> None:
        text = (
            "This agreement is made between John Smith and Sons Limited and the Buyer. "
            "A further agreement is made between Peter Brown and Sons Limited and the Buyer."
        )
        profile = analyze_privacy("company_span.txt", text)
        companies = [
            (finding["sample"], finding["placeholder"])
            for finding in profile["risk_map"]
            if finding["category"] == "company_name"
        ]
        self.assertEqual([sample for sample, _ in companies], ["John Smith and Sons Limited", "Peter Brown and Sons Limited"])
        self.assertEqual(len({placeholder for _, placeholder in companies}), 2, companies)

    def test_a_preposition_behind_a_person_or_party_role_ends_the_name(self) -> None:
        for text in (
            "Payment was made by John Smith to Acme Holdings Limited last year.",
            "The letter from the Claimant to Acme Holdings Limited was late.",
        ):
            with self.subTest(text=text):
                self.assertEqual(company_findings(text), [expected(text, "Acme Holdings Limited")])

    def test_an_uppercase_name_stops_at_the_title_case_word_before_it(self) -> None:
        text = "Devreden, ORNEK TEKNOLOJİ ANONİM ŞİRKETİ hisselerini devretmiştir."
        self.assertEqual(company_findings(text), [expected(text, "ORNEK TEKNOLOJİ ANONİM ŞİRKETİ")])


if __name__ == "__main__":
    unittest.main()
