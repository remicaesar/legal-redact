"""A docket reference to an investigation is not a criminal allegation.

"soruşturma" is a criminal_allegation trigger, so every procedural citation of
an investigation file -- "2024/12345 sayılı soruşturma dosyası incelendi",
"Soruşturma No: 2024/12345 olan dosya incelendi", an office called "Soruşturma
Bürosu" -- was reported CRITICAL. A CRITICAL badge on a case number teaches a
reviewer that CRITICAL does not mean what it says.

The docket form -- the word tied to a case number, or the office name -- is
still a trigger when its own clause names a person or carries an allegation: "2024/12348 sayılı soruşturma dosyasında X'in dolandırıcılık
suçunu işlediği iddia edilmektedir" has no other trigger ("suçunu" is not
"suç"), so dropping the docket word outright would ship that allegation with
no finding at all. Those cases are pinned here on offsets, not on samples.
"""

from __future__ import annotations

import time
import unittest
from pathlib import Path

from legal_analyzer.privacy import analyze_privacy

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "docket_references.txt"


def criminal_findings(text: str) -> list[dict]:
    return [f for f in analyze_privacy("not.txt", text)["risk_map"] if f["category"] == "criminal_allegation"]


def covered(findings: list[dict], text: str, needle: str) -> bool:
    """Every character of `needle` lies inside some finding."""
    start = text.index(needle)
    return all(any(f["start"] <= i < f["end"] for f in findings) for i in range(start, start + len(needle)))


# The three probes from the issue, verbatim, and the other docket shapes the
# case-number rule reads.
DOCKET_REFERENCES = [
    "İstanbul Cumhuriyet Başsavcılığının 2024/12345 sayılı soruşturma dosyası incelendi.",
    "Soruşturma No: 2024/12345 olan dosya incelendi.",
    "Soruşturma No: 2026/123 sayılı dosya Soruşturma Bürosu tarafından yürütülmektedir.",
    "Ankara Cumhuriyet Başsavcılığının 2025/4410 Soruşturma No'lu dosyası celbedildi.",
    "İzmir Cumhuriyet Başsavcılığı 2023/9071 soruşturma numaralı dosya kapsamında ifade verdi.",
    "2024/12347 sayılı soruşturma kapsamında tanık ifadesi alınmıştır.",
    "Sorusturma No : 2026 / 3377",
    "Dosyada sahte belge bulunmamaktadır.\n2024/12345 sayılı soruşturma dosyası incelendi.",
    "Sahte belge incelemesi\n\n2024/12345 sayılı soruşturma dosyası incelendi.",
    "Müşteki : Ahmet Demir\nSoruşturma No : 2026/3377",
]

# "soruşturma" that is NOT tied to a case number, or whose docket clause names
# a person or an offence. These stay CRITICAL: the docket exemption fails
# closed. The first four were found by review of the first version of this
# change, which exempted each of them; the next nine put the person or the
# allegation in front of the docket, where the second version did not look,
# six of them on the line before it, as a wrapped PDF text layer delivers it,
# three of those inside a field value ("Konu: ...") that wraps onto the docket
# and one wrapping just before a lowercase "soruşturma No:".
# The last is the price of looking there: an office named for an offence
# ("Dolandırıcılık Suçları") carries a marker in its own name, and the
# clause cannot tell that from an allegation in front of the docket word.
STILL_CRITICAL = [
    ("2024/12345 sayılı soruşturma dosyasında Ahmet Demir'in rüşvet aldığı tespit edilmiştir.", "rüşvet aldığı tespit edilmiştir"),
    ("Ahmet Demir hakkında 2 soruşturma başlatıldı.", "soruşturma başlatıldı"),
    ("Ahmet Demir'in soruşturma dosyası gönderildi.", "soruşturma dosyası gönderildi"),
    ("2024/12345 sayılı soruşturma dosyasında Ahmet Demir'in ifadesi alındı.", "ifadesi alındı"),
    ("2024/12345 sayılı soruşturma dosyasında zimmet konusu incelendi.", "zimmet konusu incelendi"),
    ("Soruşturma dosyası incelendi.", "Soruşturma dosyası incelendi"),
    (
        "Ahmet Demir'in rüşvet aldığı iddiasıyla başlatılan 2024/12345 sayılı soruşturma dosyası incelendi.",
        "soruşturma dosyası incelendi",
    ),
    (
        "Ahmet Demir'in de ifadesinin alındığı 2024/12345 sayılı soruşturma dosyası incelendi.",
        "soruşturma dosyası incelendi",
    ),
    (
        "Dosya incelendi. Şirket müdürünün rüşvet aldığı iddiasıyla başlatılan 2024/12345 sayılı soruşturma dosyası incelendi.",
        "soruşturma dosyası incelendi",
    ),
    (
        "Ahmet Demir'in rüşvet aldığı iddiasıyla başlatılan\n2024/12345 sayılı soruşturma dosyası incelendi.",
        "soruşturma dosyası incelendi",
    ),
    (
        "Şirket müdürünün rüşvet aldığı iddiasıyla başlatılan\n2024/12345 sayılı soruşturma dosyası incelendi.",
        "soruşturma dosyası incelendi",
    ),
    (
        "Konu: Şirket müdürünün rüşvet aldığı iddiasıyla başlatılan\n2024/12345 sayılı soruşturma dosyası incelendi.",
        "soruşturma dosyası incelendi",
    ),
    (
        "Konu: Ahmet Demir'in rüşvet aldığı iddiasıyla başlatılan\n2024/12345 sayılı soruşturma dosyası incelendi.",
        "soruşturma dosyası incelendi",
    ),
    (
        "Açıklama: Şirket müdürünün zimmet iddiasıyla başlatılan\n2024/12345 sayılı soruşturma dosyası incelendi.",
        "soruşturma dosyası incelendi",
    ),
    (
        "Şirket müdürünün rüşvet aldığı iddiasıyla açılan\nsoruşturma No: 2024/12345 olan dosya incelendi.",
        "olan dosya incelendi",
    ),
    (
        "Dosyanın ilk incelemesi kapsamında toplanan belgeler ve tutanaklar ile ilgili birimlerin "
        "yazışmaları bir arada değerlendirilmiş olup bu değerlendirme sırasında taraflarca sunulan "
        "dilekçeler ve ekleri de göz önünde bulundurulmuş ve dosya kapsamındaki tüm belgeler ayrı "
        "ayrı ele alınmış ve kayıt altına alınmış ve ilgili makamlara bildirilmiş ve sonrasında "
        "yapılan toplantıda da aynı hususlar yeniden gündeme getirilmiş ve şirket müdürünün rüşvet "
        "aldığı iddiasıyla başlatılan\n2024/12345 sayılı soruşturma dosyası incelendi.",
        "soruşturma dosyası incelendi",
    ),
    (
        "İstanbul Cumhuriyet Başsavcılığı Dolandırıcılık Suçları Soruşturma Bürosu",
        "Soruşturma Bürosu",
    ),
]

# Clauses that allege an offence. Each must keep a CRITICAL finding over the
# allegation itself.
ALLEGATIONS = [
    (
        "Şüpheli Kemal Arslan'ın hırsızlık suçunu işlediği iddia edilen eylemleri ayrıca değerlendirilecektir.",
        "hırsızlık suçunu işlediği iddia edilen",
    ),
    (
        "2024/12348 sayılı soruşturma dosyasında Burak Ertan'ın dolandırıcılık suçunu işlediği iddia edilmektedir.",
        "dolandırıcılık suçunu işlediği iddia edilmektedir",
    ),
    (
        "Soruşturma No: 2024/12349 olan dosyada Burak Ertan'a rüşvet isnat edilmektedir.",
        "rüşvet isnat edilmektedir",
    ),
    (
        "2024/12350 sayılı soruşturma dosyasında şüpheli sıfatıyla ifadesi alınmıştır.",
        "şüpheli sıfatıyla ifadesi alınmıştır",
    ),
]


class DocketReferenceIsNotAnAllegationTests(unittest.TestCase):
    def test_a_docket_reference_has_no_criminal_allegation_finding(self):
        for text in DOCKET_REFERENCES:
            with self.subTest(text=text):
                self.assertEqual([f["sample"] for f in criminal_findings(text)], [])

    def test_the_docket_keeps_its_case_number_finding(self):
        """The number is still pseudonymised; only the CRITICAL clause goes."""
        text = "Soruşturma No: 2026/123 sayılı dosya Soruşturma Bürosu tarafından yürütülmektedir."
        numbers = [
            f["sample"]
            for f in analyze_privacy("not.txt", text)["risk_map"]
            if f["category"] == "case_or_investigation_number"
        ]
        self.assertEqual(numbers, ["Soruşturma No: 2026/123"])

    def test_an_investigation_against_a_person_is_still_critical(self):
        """Bare "soruşturma" -- not a docket citation -- is unchanged."""
        text = "Burak Ertan hakkında soruşturma başlatılmıştır."
        findings = criminal_findings(text)
        self.assertTrue(findings)
        self.assertTrue(covered(findings, text, "soruşturma başlatılmıştır"))


class AllegationIsStillCriticalTests(unittest.TestCase):
    def test_an_unnumbered_or_personal_or_offence_docket_still_reports(self):
        for text, clause in STILL_CRITICAL:
            with self.subTest(text=text):
                findings = criminal_findings(text)
                self.assertTrue(findings)
                self.assertTrue(all(f["risk"] == "CRITICAL" for f in findings))
                self.assertTrue(covered(findings, text, clause), [f["sample"] for f in findings])

    def test_each_allegation_is_covered_by_a_critical_finding(self):
        for text, allegation in ALLEGATIONS:
            with self.subTest(text=text):
                findings = criminal_findings(text)
                self.assertTrue(all(f["risk"] == "CRITICAL" for f in findings))
                self.assertTrue(covered(findings, text, allegation), [f["sample"] for f in findings])


class LongUnpunctuatedRunTests(unittest.TestCase):
    """A line-per-record extraction: thousands of docket rows, none punctuated.

    Each docket word looks back only a bounded stretch, so the run costs time in
    proportion to its length rather than to its square, and an allegation on the
    rows just above a docket still reaches it.
    """

    ROW = "Soruşturma kaydı 2026/3377 sayılı soruşturma dosya satırı\n"

    def test_a_long_run_finishes_quickly_and_a_nearby_allegation_still_reports(self):
        allegation = "Şirket müdürünün rüşvet aldığı iddiasıyla açılan\n"
        text = self.ROW * 3000 + allegation + "2024/12345 sayılı soruşturma dosyası incelendi"
        started = time.perf_counter()
        findings = criminal_findings(text)
        self.assertLess(time.perf_counter() - started, 10.0)
        self.assertTrue(covered(findings, text, "soruşturma dosyası incelendi"), [f["sample"] for f in findings][-3:])


class GoldFixtureTests(unittest.TestCase):
    """The gold document, line by line: numbered dockets silent, allegations reported.

    The heading "Soruşturma dosyalarına ilişkin yazışma notu" cites no number,
    so it keeps its CRITICAL finding: the exemption is for numbered dockets only.
    """

    def test_only_the_allegation_lines_and_the_unnumbered_heading_carry_criminal_findings(self):
        text = FIXTURE.read_text(encoding="utf-8")
        findings = criminal_findings(text)
        lines_with_findings = sorted(
            {text.count("\n", 0, f["start"]) for f in findings}
        )
        allegation_lines = [
            index for index, line in enumerate(text.splitlines()) if "işlediği iddia" in line
        ]
        self.assertEqual(len(allegation_lines), 2)
        self.assertEqual(lines_with_findings, [0] + allegation_lines)
        for allegation in (
            "dolandırıcılık suçunu işlediği iddia edilmektedir",
            "hırsızlık suçunu işlediği iddia edilen",
        ):
            with self.subTest(allegation=allegation):
                self.assertTrue(covered(findings, text, allegation))


if __name__ == "__main__":
    unittest.main()
