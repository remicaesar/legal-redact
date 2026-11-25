"""A court decision cited the way Turkish lawyers cite it must not ship.

Two defects on one line, "Yargıtay 3. Hukuk Dairesi'nin 2021/4521 E., 2022/1187
K. sayılı kararı" -- the form every Yargıtay, Danıştay and regional-court
citation takes:

  * the case and decision numbers matched no rule at all. The case-number rule
    read "Esas No: 2021/4521" and "2021/4521 Esas", but not the initials "E." and
    "K.", so both numbers sat in no finding, could not be approved, and were
    delivered verbatim;
  * the court was reported twice. The high-court rule's sweep stops at the first
    full stop, which is the chamber's ordinal, so it reported "Yargıtay 3", and
    the numbered-court rule reported "3. Hukuk Dairesi" again: two overlapping
    findings and two placeholders for one court, with the chamber keyed apart
    from its court.

The gold regression case is `tests/fixtures/case_law_opinion.txt`. The audit's
substring matching cannot see the second defect ("Yargıtay 3" is a substring of
the label), so it is pinned here on offsets and on the delivered file.

Every identifier here is invented.
"""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from legal_analyzer import privacy
from legal_analyzer.privacy import analyze_privacy, build_redacted_preview
from tests.test_court_full_span import ExportHarness, SpanCoverageAssertions, make_docx

PROJECT_DIR = Path(__file__).resolve().parent.parent

CITATION = "Yargıtay 3. Hukuk Dairesi'nin 2021/4521 E., 2022/1187 K. sayılı kararı incelendi."


def findings_of(text: str, category: str) -> list[dict]:
    return [f for f in analyze_privacy("gorus.txt", text)["risk_map"] if f["category"] == category]


class CitationCaseNumberTests(SpanCoverageAssertions):
    """The E. / K. initials, after the number and in front of it."""

    def test_numbers_cited_with_trailing_initials_are_case_numbers(self):
        cases = findings_of(CITATION, "case_or_investigation_number")
        self.assertEqual([f["sample"] for f in cases], ["2021/4521", "2022/1187"])
        self.assert_inside_a_finding(CITATION, "2021/4521", cases)
        self.assert_inside_a_finding(CITATION, "2022/1187", cases)

    def test_numbers_cited_with_leading_initials_are_case_numbers(self):
        text = "Danıştay 10. Daire, E. 2023/1234, K: 2024/567 sayılı kararında aynı görüştedir."
        cases = findings_of(text, "case_or_investigation_number")
        self.assertEqual([f["sample"] for f in cases], ["E. 2023/1234", "K: 2024/567"])

    def test_the_initial_names_the_register_the_placeholder_is_keyed_on(self):
        """E. is the Esas register and K. the Karar register, written out or not.

        The same digits in the same register are one case wherever they are
        written; in the other register they are a different number.
        """
        text = (
            "Esas No: 2021/4521 olan dosyada 2021/4521 E. sayılı karar ile E. 2021/4521 "
            "aynı dosyadır; 2021/4521 K. sayılı karar ise başka bir karar numarasıdır."
        )
        placeholders = [f["placeholder"] for f in findings_of(text, "case_or_investigation_number")]
        self.assertEqual(len(placeholders), 4)
        self.assertEqual(len(set(placeholders[:3])), 1, placeholders)
        self.assertNotEqual(placeholders[3], placeholders[0])

    def test_the_initials_are_read_only_as_capitals_with_a_full_stop(self):
        for text in (
            "Fatura 2024/12 e. tarafından düzenlenmiştir.",
            "Ek: 2024/12 sayılı liste dosyadadır.",
            "Belge 2024/12 Eki olarak sunulmuştur.",
        ):
            with self.subTest(text=text):
                self.assertEqual(findings_of(text, "case_or_investigation_number"), [])

    def test_a_citation_sample_never_crosses_a_line(self):
        text = "Yargıtay 3. Hukuk Dairesi'nin 2021/4521\nE. sayılı kararı ve E.\n2022/1187 sayılı kararı"
        for finding in analyze_privacy("gorus.txt", text)["risk_map"]:
            with self.subTest(sample=finding["sample"]):
                self.assertNotIn("\n", finding["sample"])
                self.assertNotIn("\n", text[finding["start"]:finding["end"]])


class HighCourtChamberTests(SpanCoverageAssertions):
    """One court, its chamber included, one finding and one pseudonym."""

    def test_a_numbered_chamber_is_one_finding_with_its_court(self):
        courts = findings_of(CITATION, "court_or_authority")
        self.assertEqual(
            [(f["sample"], f["start"], f["end"]) for f in courts],
            [("Yargıtay 3. Hukuk Dairesi", 0, 25)],
        )

    def test_every_chamber_shape_is_inside_its_courts_finding(self):
        for text, court in (
            ("Danıştay 10. Daire de aynı ilkeyi benimsemiştir.", "Danıştay 10. Daire"),
            ("Yargıtay 12. CD'nin kararı bozulmuştur.", "Yargıtay 12. CD"),
            (
                "İstanbul Bölge Adliye Mahkemesi 8. Hukuk Dairesi'nin kararı kesindir.",
                "İstanbul Bölge Adliye Mahkemesi 8. Hukuk Dairesi",
            ),
            (
                "Ankara Bölge İdare Mahkemesi 2. İdari Dava Dairesi kararı tebliğ edildi.",
                "Ankara Bölge İdare Mahkemesi 2. İdari Dava Dairesi",
            ),
            ("YARGITAY 3. HUKUK DAİRESİ KARARI", "YARGITAY 3. HUKUK DAİRESİ"),
        ):
            with self.subTest(text=text):
                courts = findings_of(text, "court_or_authority")
                self.assertEqual([f["sample"] for f in courts], [court])
                self.assert_inside_a_finding(text, court, courts)

    def test_a_chamber_with_an_attached_case_suffix_is_inside_its_courts_finding(self):
        """TDK writes the case ending onto the institution name with no apostrophe."""
        for text, court in (
            ("Yargıtay 3. Hukuk Dairesinin 2021/4521 E., 2022/1187 K. sayılı kararı", "Yargıtay 3. Hukuk Dairesi"),
            ("Yargıtay 3. Hukuk Dairesince verilen karar onanmıştır.", "Yargıtay 3. Hukuk Dairesi"),
            ("Dosya Yargıtay 3. Hukuk Dairesine gönderilmiştir.", "Yargıtay 3. Hukuk Dairesi"),
            ("Dava Yargıtay 3. Hukuk Dairesinde görülmüştür.", "Yargıtay 3. Hukuk Dairesi"),
            ("Dosya Yargıtay 3. Hukuk Dairesinden geri gelmiştir.", "Yargıtay 3. Hukuk Dairesi"),
            ("Yargıtay 3. Hukuk Dairesindeki dosya incelendi.", "Yargıtay 3. Hukuk Dairesi"),
            ("Yargıtay 3. Hukuk Dairesiyle yazışılmıştır.", "Yargıtay 3. Hukuk Dairesi"),
            ("YARGITAY 3. HUKUK DAİRESİNİN 2021/4521 E. sayılı kararı", "YARGITAY 3. HUKUK DAİRESİ"),
            ("YARGITAY 3. HUKUK DAIRESININ kararı", "YARGITAY 3. HUKUK DAIRESI"),
            ("Dosya YARGITAY 3. HUKUK DAIRESINE gönderildi.", "YARGITAY 3. HUKUK DAIRESI"),
            ("Dosya Danıştay 10. Daireye gönderilmiştir.", "Danıştay 10. Daire"),
            ("Danıştay 10. Dairece verilen karar kesindir.", "Danıştay 10. Daire"),
            ("Dava Danıştay 10. Dairede görülmüştür.", "Danıştay 10. Daire"),
            ("Dosya Danıştay 10. Daireden geri gelmiştir.", "Danıştay 10. Daire"),
            ("Dosya DANIŞTAY 10. DAİREYE gönderilmiştir.", "DANIŞTAY 10. DAİRE"),
        ):
            with self.subTest(text=text):
                start = text.index(court)
                courts = findings_of(text, "court_or_authority")
                self.assertEqual(
                    [(f["sample"], f["start"], f["end"]) for f in courts],
                    [(court, start, start + len(court))],
                )
                self.assertNotIn("\n", courts[0]["sample"])

    def test_a_suffixed_chamber_takes_its_unsuffixed_mentions_placeholder(self):
        for text in (
            "Yargıtay 3. Hukuk Dairesi kararı, Yargıtay 3. Hukuk Dairesinin ilamı, "
            "YARGITAY 3. HUKUK DAİRESİNCE verilen karar ve Yargıtay 4. Hukuk Dairesinin kararı.",
            "Danıştay 10. Daire kararı, Danıştay 10. Daireye gönderilen dosya, "
            "DANIŞTAY 10. DAİREDEN gelen yazı ve Danıştay 4. Dairece verilen karar.",
        ):
            with self.subTest(text=text):
                placeholders = [f["placeholder"] for f in findings_of(text, "court_or_authority")]
                self.assertEqual(len(placeholders), 4, placeholders)
                self.assertEqual(placeholders[0], placeholders[1])
                self.assertEqual(placeholders[0], placeholders[2])
                self.assertNotEqual(placeholders[0], placeholders[3])
                preview = build_redacted_preview(text, analyze_privacy("gorus.txt", text)["risk_map"])
                for word in ("Hukuk", "HUKUK", "Daire", "DAİRE", "Yargıtay", "Danıştay", "DANIŞTAY"):
                    self.assertNotIn(word, preview)

    def test_the_chamber_is_keyed_with_its_court(self):
        """The same court at every mention, and two courts never one.

        The chamber used to carry its own key, so "3. Hukuk Dairesi" of the
        regional court and of Yargıtay shared one pseudonym.
        """
        text = (
            "YARGITAY 3. HUKUK DAİRESİ kararı, Yargıtay 3. Hukuk Dairesi'nin ilamı ve "
            "İstanbul Bölge Adliye Mahkemesi 3. Hukuk Dairesi'nin kararı."
        )
        placeholders = [f["placeholder"] for f in findings_of(text, "court_or_authority")]
        self.assertEqual(len(placeholders), 3, placeholders)
        self.assertEqual(placeholders[0], placeholders[1])
        self.assertNotEqual(placeholders[0], placeholders[2])

    def test_every_spelling_of_one_chamber_takes_one_placeholder(self):
        """Daire, Dairesi, a suffixed form and the HD/CD abbreviation are one venue."""
        cases = (
            (
                "Danıştay 10. Dairesi'nin kararı, Danıştay 10. Daire'nin görüşü, "
                "Danıştay 10. Daireye gönderilen dosya, Danıştay 10. Dairesinin ilamı "
                "ve Danıştay 11. Daire kararı.",
                ["Danıştay 10. Dairesi", "Danıştay 10. Daire", "Danıştay 10. Daire",
                 "Danıştay 10. Dairesi", "Danıştay 11. Daire"],
            ),
            (
                "Yargıtay 3. HD kararı, Yargıtay 3. Hukuk Daire'nin görüşü, "
                "Yargıtay 3. Hukuk Dairesinin ilamı ve Yargıtay 4. Hukuk Dairesi kararı.",
                ["Yargıtay 3. HD", "Yargıtay 3. Hukuk Daire", "Yargıtay 3. Hukuk Dairesi",
                 "Yargıtay 4. Hukuk Dairesi"],
            ),
            (
                "Yargıtay 3.HD kararı, Yargıtay 3. Hukuk Dairesi'nin ilamı, "
                "Yargıtay 3.Hukuk Dairesi görüşü, Yargıtay 3.  Hukuk\tDairesi kararı "
                "ve Yargıtay 4.Hukuk Dairesi kararı.",
                ["Yargıtay 3.HD", "Yargıtay 3. Hukuk Dairesi", "Yargıtay 3.Hukuk Dairesi",
                 "Yargıtay 3.  Hukuk\tDairesi", "Yargıtay 4.Hukuk Dairesi"],
            ),
            (
                "Yargıtay 12. CD kararı, Yargıtay 12. Ceza Dairesi'nin ilamı, "
                "Yargıtay 12. Ceza Daire'nin görüşü ve Yargıtay 12. Hukuk Dairesi kararı.",
                ["Yargıtay 12. CD", "Yargıtay 12. Ceza Dairesi", "Yargıtay 12. Ceza Daire",
                 "Yargıtay 12. Hukuk Dairesi"],
            ),
        )
        for text, samples in cases:
            with self.subTest(text=text):
                courts = findings_of(text, "court_or_authority")
                self.assertEqual([f["sample"] for f in courts], samples)
                placeholders = [f["placeholder"] for f in courts]
                self.assertEqual(len(set(placeholders[:-1])), 1, placeholders)
                self.assertNotEqual(placeholders[0], placeholders[-1])

    def test_a_folded_chamber_stays_apart_from_another_court(self):
        text = (
            "Yargıtay 3. HD kararı, Yargıtay 3.Hukuk Daire'nin görüşü ve "
            "İstanbul Bölge Adliye Mahkemesi 3.Hukuk Daire'nin kararı, "
            "İstanbul Bölge Adliye Mahkemesi 3. HD kararı."
        )
        placeholders = [f["placeholder"] for f in findings_of(text, "court_or_authority")]
        self.assertEqual(len(placeholders), 4, placeholders)
        self.assertEqual(placeholders[0], placeholders[1])
        self.assertEqual(placeholders[2], placeholders[3])
        self.assertNotEqual(placeholders[0], placeholders[2])

    def test_a_numbered_court_outside_a_high_court_is_unchanged(self):
        text = "Ankara 5. Asliye Hukuk Mahkemesi'nin 2025/417 E. sayılı dosyası."
        courts = findings_of(text, "court_or_authority")
        self.assertEqual([f["sample"] for f in courts], ["Ankara 5. Asliye Hukuk Mahkemesi"])
        self.assertEqual([f["sample"] for f in findings_of(text, "case_or_investigation_number")], ["2025/417"])

    def test_an_unnumbered_high_court_still_takes_the_sweep(self):
        """The chamber branch is tried first and changes nothing without a chamber."""
        text = "Yargıtay Hukuk Genel Kurulu'nun 2025/417 E. sayılı kararı."
        courts = findings_of(text, "court_or_authority")
        self.assertEqual([f["sample"] for f in courts], ["Yargıtay Hukuk Genel Kurulu'nun 2025/417 E"])
        self.assert_inside_a_finding(text, "2025/417", findings_of(text, "case_or_investigation_number"))

    def test_the_rendering_names_the_court_once(self):
        preview = build_redacted_preview(CITATION, analyze_privacy("gorus.txt", CITATION)["risk_map"])
        self.assertEqual(preview, "[COURT_1]'nin [CASE_NUMBER_1] E., [CASE_NUMBER_2] K. sayılı kararı incelendi.")

    def test_the_chamber_rule_is_dropped_only_inside_a_high_courts_chamber(self):
        """The skip is keyed on the chamber span, so a separate numbered court stays."""
        text = "Yargıtay 3. Hukuk Dairesi ile 3. Hukuk Dairesi arasında yazışma."
        courts = findings_of(text, "court_or_authority")
        self.assertEqual([f["sample"] for f in courts], ["Yargıtay 3. Hukuk Dairesi", "3. Hukuk Dairesi"])
        self.assertEqual(privacy.DETECTION_RULES.count(privacy.NUMBERED_COURT_RULE), 1)
        self.assertEqual(privacy.DETECTION_RULES.count(privacy.HIGH_COURT_RULE), 1)


class CitationDeliveredFileTests(ExportHarness):
    """The gold fixture through the real review and export surfaces."""

    def delivered_text(self) -> str:
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        source = Path(tmp.name) / "gorus.docx"
        text = (PROJECT_DIR / "tests" / "fixtures" / "case_law_opinion.txt").read_text(encoding="utf-8")
        make_docx(source, "</w:t></w:r></w:p><w:p><w:r><w:t>".join(text.splitlines()))
        self.install_document(source)
        return "".join(self.exported_runs())

    def test_no_case_number_or_chamber_ships(self):
        delivered = self.delivered_text()
        self.assertIn("[COURT_1]", delivered)
        for needle in ("2021/4521", "2022/1187", "2020/2210", "2021/356", "Yargıtay", "Hukuk Dairesi", "Danıştay", "10. Daire"):
            with self.subTest(needle=needle):
                self.assertNotIn(needle, delivered)


if __name__ == "__main__":
    unittest.main()
