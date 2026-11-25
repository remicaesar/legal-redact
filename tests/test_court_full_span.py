"""The place word of a Turkish venue must not ship in the delivered file.

The court rules used to start at the branch number, so on
`Ankara 5. Asliye Hukuk Mahkemesi` the finding covered the court and not
`Ankara`. Text outside every finding cannot be approved by a reviewer, so it
cannot be redacted on export: measured, Turkish court full-span recall was 0 of
16 and `Ankara`, `İzmir`, `ÇANKAYA` and `BEŞİKTAŞ` each survived verbatim into
the deliverable, directly beside a pseudonymised court. Court plus city is a
strong re-identifier for a case. The bar-registration rule had the same shape
and left `Baro` outside its span (full-span recall 2 of 6).

Two layers, because only one of them is the product:

  * `CourtPlaceSpanTests` / `BarRegistrationSpanTests` assert on offsets, which
    is where the defect lives;
  * `DeliveredFileTests` runs the bundled sample through the real review and
    export HTTP surfaces and reads the `w:t` runs of the produced DOCX. A test
    that only checks a finding's offsets can pass while the export still leaks,
    and the leak is the defect.

Every identifier named here is either from the repository's own synthetic
sample or invented for this file.
"""

import json
import re
import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from xml.etree import ElementTree
from zipfile import ZIP_DEFLATED, ZipFile

import tests.env_setup  # noqa: F401  -- sets LEGAL_ANALYZER_SECRET_KEY before app is imported
import app as app_module
from legal_analyzer import privacy, turkish_places
from legal_analyzer.redaction_plan import plan_segments
from legal_analyzer.semantic_preservation import render
from legal_analyzer.extraction import extract_docx_text
from legal_analyzer.privacy import analyze_privacy
from legal_analyzer.taxonomy import CATEGORIES, SUBCATEGORIES
from werkzeug.security import generate_password_hash

PROJECT_DIR = Path(__file__).resolve().parent.parent
SAMPLE_DOCX = PROJECT_DIR / "samples" / "documents" / "turkish_petition.docx"
OCR_FIXTURE = PROJECT_DIR / "tests" / "fixtures" / "ocr_scan_noisy.txt"
W_NAMESPACE = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def make_docx(path: Path, text: str) -> None:
    """A minimal real DOCX package, so the export path is the production one."""
    with ZipFile(path, "w", ZIP_DEFLATED) as archive:
        archive.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
            "</Types>",
        )
        archive.writestr(
            "_rels/.rels",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
            "</Relationships>",
        )
        archive.writestr(
            "word/document.xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            f"<w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:body></w:document>",
        )


def court_findings(text: str) -> list[dict]:
    return [
        finding
        for finding in analyze_privacy("karar.txt", text)["risk_map"]
        if finding["category"] == "court_or_authority"
    ]


def findings_of(text: str, category: str) -> list[dict]:
    return [f for f in analyze_privacy("dilekce.txt", text)["risk_map"] if f["category"] == category]


class SpanCoverageAssertions(unittest.TestCase):
    """Coverage asserted on offsets, never on a `sample` substring.

    A substring assertion on `sample` passes on a finding of some other
    category that happens to sweep the word up -- the generic court suffix rule
    carries 120 characters of trailing clause, and the three context rules carry
    up to 160. What has to hold is that the needle lies inside a finding OF THE
    CATEGORY that is meant to cover it, which is what makes it approvable as
    that entity and removable on export.
    """

    def assert_inside_a_finding(self, text: str, needle: str, findings: list[dict]):
        occurrences = [match.span() for match in re.finditer(re.escape(needle), text)]
        self.assertTrue(occurrences, f"{needle!r} does not occur in the text")
        for start, end in occurrences:
            self.assertTrue(
                any(f["start"] <= start and end <= f["end"] for f in findings),
                f"{needle!r} at [{start}:{end}] is covered by no finding: "
                f"{[(f['start'], f['end'], f['sample']) for f in findings]}",
            )


class CourtPlaceSpanTests(SpanCoverageAssertions):
    def test_place_word_is_inside_the_court_finding(self):
        """The measured defect, one case per casing the rule has to survive.

        `İZMİR` is the case a naive fold breaks on: it lower-cases to `izmir`
        only under a Turkish-aware fold, so a rule leaning on `.lower()` misses
        the all-caps heading that Turkish filings are actually written in.
        `İstanbul Anadolu` is the two-word form -- the courthouse qualifier is
        part of the venue name without being a settlement.
        """
        cases = [
            ("Ankara 5. Asliye Hukuk Mahkemesi kararı tebliğ edilmiştir.", "Ankara"),
            ("İzmir 2. İş Mahkemesi dosyası incelenmiştir.", "İzmir"),
            ("ÇANKAYA 4. İCRA MÜDÜRLÜĞÜ dosyasına kaydedilmiştir.", "ÇANKAYA"),
            ("BEŞİKTAŞ 1. ASLİYE TİCARET MAHKEMESİ kararı", "BEŞİKTAŞ"),
            ("İZMİR 2. İŞ MAHKEMESİ kararı", "İZMİR"),
            ("ANKARA 7. ASLİYE HUKUK MAHKEMESİ DURUŞMA TUTANAĞI", "ANKARA"),
            ("İstanbul Anadolu 3. Asliye Ticaret Mahkemesi kararı", "İstanbul Anadolu"),
            ("Beyoğlu 24. Noterliği'nde düzenlenmiştir.", "Beyoğlu"),
            ("Masalkent 3. Asliye Hukuk Mahkemesi", "Masalkent"),
        ]
        for text, place in cases:
            with self.subTest(place=place):
                self.assert_inside_a_finding(text, place, court_findings(text))

    def test_place_word_and_court_are_one_finding_not_two(self):
        """One span, so one approval and one pseudonym.

        Two adjacent findings would also cover the place, but the reviewer would
        decide the city and the court separately and the export would replace
        them with two placeholders -- a venue split across [COURT_AUTHORITY_1]
        and [COURT_AUTHORITY_2] reads as two courts.
        """
        text = "Ankara 5. Asliye Hukuk Mahkemesi kararı tebliğ edilmiştir."
        findings = court_findings(text)
        self.assertEqual(len(findings), 1, [f["sample"] for f in findings])
        self.assertEqual(findings[0]["sample"], "Ankara 5. Asliye Hukuk Mahkemesi")

    def test_lowercase_running_text_before_a_court_is_not_absorbed(self):
        """The prefix is uppercase-initial so it cannot eat ordinary prose.

        These rules run under IGNORECASE, which folds `[A-ZÇĞİÖŞÜ]`; without the
        scoped `(?-i:...)` the prefix would reach back over the lowercase
        connectives that actually precede a court in running text and report
        them as part of the venue.

        The word IMMEDIATELY before the branch number has to be the lowercase
        one for this to discriminate: the prefix is capped at two words and must
        end where the number begins, so a probe whose last two words are both
        capitalised places ("... İstanbul Anadolu 4. Asliye ...") matches
        identically with the fold and without it, and asserts nothing.

        Asserted as an exact sample rather than "the prose word is absent from
        every sample": the generic court-suffix rule legitimately sweeps a
        trailing clause, so "yazısı" appears in ITS sample either way, and an
        absence assertion would fail for a reason that has nothing to do with
        the fold.
        """
        for text, expected in (
            ("Dava dosyası 5. Asliye Hukuk Mahkemesi kararı tebliğ edilmiştir.", "5. Asliye Hukuk Mahkemesi"),
            (
                "Kadıköy Kaymakamlığı yazısı 3. Sulh Ceza Hakimliği dosyasına eklenmiştir",
                "3. Sulh Ceza Hakimliği",
            ),
        ):
            with self.subTest(text=text):
                self.assertIn(expected, [f["sample"] for f in court_findings(text)])

    def test_one_venue_named_three_ways_gets_one_placeholder(self):
        """One venue, one pseudonym, however the sentence in front of it reads.

        `_placeholder_for` keys on the whole sample, so any word the place
        prefix reaches back over splits one venue into several placeholders, and
        inconsistent pseudonyms actively mislead a reader following the
        document.

        Two probes, because each catches a prefix width the other cannot. The
        venue WITH a place word went to three placeholders under a two-word
        prefix, which absorbed `Müvekkil`, `Ayrıca` and `Dosya`. The venue with
        NO place word went to three under a one-word prefix, which absorbed the
        same words once `Kadıköy` was not there to fill the slot -- so a fixture
        that always supplies a place word cannot express the violation at all.
        """
        for label, text in (
            (
                "with a place word",
                "Müvekkil Kadıköy 2. Noterliği onaylı örneği ile başvurmuştur. "
                "Ayrıca Kadıköy 2. Noterliği kaydı incelenmiştir. "
                "Dosya Kadıköy 2. Noterliği tarafından gönderilmiştir.",
            ),
            (
                "with no place word",
                "Müvekkil 2. Noterliği onaylı örneği ile başvurmuştur. "
                "Ayrıca 2. Noterliği kaydı incelenmiştir. "
                "Dosya 2. Noterliği tarafından gönderilmiştir.",
            ),
        ):
            with self.subTest(probe=label):
                findings = court_findings(text)
                self.assertEqual(len(findings), 3, [f["sample"] for f in findings])
                self.assertEqual({f["placeholder"] for f in findings}, {"[NOTARY_1]"})

    def test_capitalised_non_place_word_before_a_venue_is_never_absorbed(self):
        """The place list is what separates `Ankara` from `Dosya`.

        A structural capitalised-word prefix cannot: every word here is
        uppercase-initial and sits exactly where `Ankara` sits in
        `Ankara 5. Asliye Hukuk Mahkemesi`. Absorbing one both deletes ordinary
        legal text from the deliverable and gives the venue a pseudonym of its
        own, which is the failure the two probes above measure.

        Mid-sentence on purpose -- a venue alone on its line is a heading and
        keeps its leading words whatever they are, which the Masalkent pair
        below pins.
        """
        for word in ("Ayrıca", "Dosya", "Karar", "Mahkeme", "Müvekkil"):
            with self.subTest(word=word):
                text = f"Bu hususta {word} 5. Asliye Hukuk Mahkemesi kararı incelenmiştir."
                samples = [f["sample"] for f in court_findings(text)]
                self.assertIn("5. Asliye Hukuk Mahkemesi", samples)

    def test_the_bundled_samples_invented_place_is_covered_in_both_positions(self):
        """`Masalkent` is in the list, so its position on the page is irrelevant.

        `samples/documents/turkish_petition.docx` names `Masalkent 3. Asliye
        Hukuk Mahkemesi` and declares itself `uydurulmuştur`, so no real
        gazetteer can ever hold it -- and that document is the objective's own
        headline example of the defect. It lives in
        `turkish_places.SYNTHETIC_CORPUS_PLACES` for exactly that reason.

        Both positions are asserted because a line-position rule stood here for
        one round and covered only the heading: the same venue named again in
        the body came out a second pseudonym with its city in cleartext beside
        it. A heading-only assertion cannot tell the two designs apart.
        """
        for label, text in (
            ("heading", "Masalkent 3. Asliye Hukuk Mahkemesi"),
            ("mid-prose", "Dava Masalkent 3. Asliye Hukuk Mahkemesi nezdinde açılmıştır."),
        ):
            with self.subTest(position=label):
                self.assert_inside_a_finding(text, "Masalkent", court_findings(text))

    def test_an_unlisted_word_before_a_venue_is_not_absorbed_by_position(self):
        """No line-position fallback: a heading is not a licence to take a word.

        `Sayın` heads a Turkish petition's address line and is not a place. The
        deleted own-line escape took it, which both deleted ordinary legal text
        from the deliverable and split the venue into two pseudonyms against the
        body mention below.
        """
        text = "Sayın Ankara 5. Asliye Hukuk Mahkemesi\n\nDavaya Ankara 5. Asliye Hukuk Mahkemesi bakmaktadır."
        findings = court_findings(text)
        self.assertEqual(
            [f["sample"] for f in findings],
            ["Ankara 5. Asliye Hukuk Mahkemesi", "Ankara 5. Asliye Hukuk Mahkemesi"],
        )
        self.assertEqual({f["placeholder"] for f in findings}, {"[COURT_1]"})

    def test_a_scanned_documents_ascii_place_is_inside_the_court_finding(self):
        """The same leak, reached through this repository's own OCR path.

        `legal_analyzer/ocr.py` feeds tesseract output through these rules, and
        tesseract strips Turkish diacritics: `tests/fixtures/ocr_scan_noisy.txt`
        opens with `IZMIR CUMHURIYET BASSAVCILIGI`. `tr_fold` alone sends that
        spelling to `ızmır` and the correct `İzmir` to `izmir`, so the list
        missed every scanned venue and the city shipped beside a pseudonymised
        court -- the objective's defect verbatim, on scanned documents.

        Every court and party rule in `privacy.py` already carries its ASCII
        alternate, so the place list was the one gate that did not.
        """
        for text, place in (
            ("ISTANBUL 3. SULH CEZA HAKIMLIGI", "ISTANBUL"),
            ("KADIKOY 2. ASLIYE HUKUK MAHKEMESI kararı", "KADIKOY"),
            ("IZMIR 12. ICRA MUDURLUGU dosyası", "IZMIR"),
            ("BESIKTAS 1. ASLIYE TICARET MAHKEMESI", "BESIKTAS"),
        ):
            with self.subTest(place=place):
                self.assert_inside_a_finding(text, place, court_findings(text))

    def test_a_generic_authority_carries_its_place_word_too(self):
        """The same defect reached through the rule that has no branch number.

        `GENERIC_COURT_SUFFIX_RULE` anchors on the institution word, so every
        authority it matches -- prosecutor's office, governorship, district
        governorship, trade registry -- left its leading city outside every
        finding: unapprovable, and therefore unredactable on export. It carries
        the same list-gated prefix as the numbered rules now.

        The list gate is the whole safety margin here, because this rule has no
        branch number to pin a prefix against, so the last two cases are the
        load-bearing half: a real place the directory does not hold and an
        ordinary capitalised word must both stay out. `Alsancak` is the former
        -- a neighbourhood below the level at which venues are designated, and
        the residual the module docstring keeps open on purpose.
        """
        for text, place in (
            ("İstanbul Cumhuriyet Başsavcılığı soruşturma başlattı", "İstanbul"),
            ("IZMIR CUMHURIYET BASSAVCILIGI dosyasi", "IZMIR"),
            ("Kadıköy Valiliği yeni bir karar almıştır", "Kadıköy"),
            ("İstanbul Ticaret Sicili Müdürlüğü kaydı güncellendi", "İstanbul"),
        ):
            with self.subTest(place=place):
                self.assert_inside_a_finding(text, place, court_findings(text))

        for text, excluded in (
            ("Alsancak Cumhuriyet Başsavcılığı dosyası", "Alsancak"),
            ("Dosya Cumhuriyet Başsavcılığı tarafından gönderildi", "Dosya"),
        ):
            with self.subTest(excluded=excluded):
                for finding in court_findings(text):
                    self.assertNotIn(excluded, finding["sample"])

    def test_a_named_spans_sweep_ending_mid_venue_does_not_narrow_the_authority(self):
        """Suppression reads the institution anchor, not the widened start.

        The Yargıtay/Danıştay rule sweeps a fixed 60 characters past its own
        name, so its span can end BETWEEN a following authority's place word and
        that authority's institution word. Here the sweep ends at offset 68,
        `Kadıköy` begins at 60 and `Valiliği` at 68.

        Decided on the anchor (68), the governorship is not inside the sweep and
        keeps its city. Decided on the widened start (60) it is, so the match is
        skipped and the scan resumes at 68 -- re-finding the authority without
        `Kadıköy`, which is then covered only by the Danıştay finding and so
        carries a different venue's placeholder. The padding is filler chosen to
        put the boundary in that position and means nothing itself.
        """
        text = (
            "Danıştay ictihadi " + "x" * 32 + " uyarinca Kadıköy Valiliği "
            "islemi iptal edilmistir"
        )
        self.assertEqual(text.index("Kadıköy"), 60)
        self.assertEqual(text.index("Valiliği"), 68)

        governorship = [f for f in court_findings(text) if f["sample"].endswith("iptal edilmistir")]
        self.assertEqual(len(governorship), 1, [f["sample"] for f in court_findings(text)])
        self.assertTrue(governorship[0]["sample"].startswith("Kadıköy Valiliği"), governorship[0]["sample"])

    def test_a_high_court_named_for_its_city_carries_that_city(self):
        """Rule (d), the last court rule to get the prefix.

        Turkey's nine regional administrative courts are each officially named
        for their city, so `Ankara Bölge İdare Mahkemesi` is one venue name --
        and with the rule anchored on `Bölge` the city sat outside every finding
        and shipped beside the pseudonymised court.

        `Bölge Adliye Mahkemesi` used to be covered only by accident, because
        `bölge adliye` is also one of `GENERIC_COURT_SUFFIX_RULE`'s triggers
        while its sibling `Bölge İdare Mahkemesi` is not -- two court names one
        word apart behaving differently. Both are asserted so neither can drift
        back.

        This is the only rule carrying a place prefix AND a trailing sweep, so
        the last case pins that the prefix is still list-gated on it: `Dosya` is
        an ordinary word and must stay out.
        """
        for text, place in (
            ("Ankara Bölge İdare Mahkemesi kararı tebliğ edildi", "Ankara"),
            ("ANKARA BOLGE IDARE MAHKEMESI karari", "ANKARA"),
            ("Kadıköy Bölge Adliye Mahkemesi kararı", "Kadıköy"),
            ("BEŞİKTAŞ Bölge Adliye Mahkemesi kararı", "BEŞİKTAŞ"),
            ("İzmir Bölge İdare Mahkemesi dosyası", "İzmir"),
            ("ÇANKAYA Bölge İdare Mahkemesi kararı", "ÇANKAYA"),
        ):
            with self.subTest(place=place):
                self.assert_inside_a_finding(text, place, court_findings(text))

        ordinary = "Dosya Bölge İdare Mahkemesi kararı"
        for finding in court_findings(ordinary):
            self.assertNotIn("Dosya", finding["sample"])

    def test_a_high_court_leading_its_numbered_chamber_stays_covered(self):
        """`Yargıtay` is the institution, not a place, and covers itself.

        It was in the place list for two rounds, which let the NUMBERED rule
        take it into its prefix and render the venue as one placeholder. The
        list is places only again, so the high-court rule covers `Yargıtay`
        through its own span and the numbered rule covers the chamber.

        Measured and stated rather than glossed: the two findings OVERLAP
        without either containing the other, so no single finding spans the
        whole venue name, this venue takes TWO placeholders, and the preview
        reads `[COURT_AUTHORITY_2][COURT_AUTHORITY_1]`. That is byte-identical
        to the base commit -- the place-list entry was masking it, not fixing it
        -- and it is a pseudonym-tidiness defect, not a leak: every CHARACTER of
        the venue is inside some finding, so a reviewer can approve all of it
        and the exporter can remove all of it.

        Union coverage is therefore what this asserts, character by character,
        rather than a single containing span, which would assert a property the
        code has never had.
        """
        text = "Yargıtay 9. Hukuk Dairesi kararı incelenmiştir."
        findings = court_findings(text)
        venue_start, venue_end = 0, len("Yargıtay 9. Hukuk Dairesi")
        uncovered = [
            offset
            for offset in range(venue_start, venue_end)
            if not any(f["start"] <= offset < f["end"] for f in findings)
        ]
        self.assertEqual(uncovered, [], [(f["start"], f["end"], f["sample"]) for f in findings])

    def test_a_province_plus_qualifier_venue_is_covered_whole(self):
        """`Ankara Batı` and `İstanbul Anadolu` are the only two of their shape.

        They are the whole of `VENUE_QUALIFIERS`: grepping the Ministry's
        courthouse directory, they are its only two province-plus-qualifier
        designations, and the four direction words that used to sit in that set
        appear there only inside settlement names.

        Dropping a qualifier costs more than the qualifier itself, which is what
        this pins. `_venue_prefix_start` walks right to left and stops at the
        first non-place token, so with `Batı` gone the walk halts there and
        gives back `Ankara` too -- the delivered file then reads
        `Ankara Batı [COURT_AUTHORITY_1]` and leaks both words, not one.
        """
        for text, venue in (
            ("Ankara Batı 5. Asliye Hukuk Mahkemesi kararı tebliğ edildi", "Ankara Batı"),
            ("İstanbul Anadolu 3. Asliye Ticaret Mahkemesi kararı", "İstanbul Anadolu"),
        ):
            with self.subTest(venue=venue):
                self.assert_inside_a_finding(text, venue, court_findings(text))

    def test_a_directory_sourced_district_is_covered(self):
        """Districts come from the Ministry's directory, not from a hand list.

        `Konak` (İzmir) and `Çaycuma` (Zonguldak) are judicial locations the
        hand-written list never held; `Alsancak` is deliberately absent, being a
        neighbourhood one level below the level at which venues are designated
        -- see the module docstring for why that residual is kept rather than
        closed.
        """
        for text, place in (
            ("Konak 4. Asliye Hukuk Mahkemesi kararı", "Konak"),
            ("Çaycuma 1. Asliye Hukuk Mahkemesi kararı", "Çaycuma"),
        ):
            with self.subTest(place=place):
                self.assert_inside_a_finding(text, place, court_findings(text))

    def test_a_historical_venue_the_directory_dropped_is_still_covered(self):
        """`Sincan` is a real district the Ministry's directory does not list.

        Its courthouse was merged into Ankara Batı Adliyesi in 2014 and the
        directory lists current organisation, so the district lost its row --
        inconsistently, since 255 other merged rows keep their names. Documents
        still cite the historical venue, and without the hand addition in
        `turkish_places.SOURCE_GAP_PLACES` the district name sits outside every
        finding and ships beside the pseudonymised venue.
        """
        text = "Sincan 2. İcra Müdürlüğü dosyası"
        self.assert_inside_a_finding(text, "Sincan", court_findings(text))

    def test_no_place_the_previous_list_covered_was_lost(self):
        """A list swap must widen. Narrowing is the one outcome forbidden here.

        The hand-written list this file's places replaced is reproduced below,
        as the contract that swap had to honour. It is not a copy of the current
        list -- it is the OLD one, and the assertion is that every place it
        recognised is still recognised today, whatever the source becomes next.

        This exists because the superset property was asserted from a handful of
        remembered names last time and was false: `Sincan` was lost and shipped.
        The four direction words the previous set carried are excluded on
        purpose -- `Doğu`, `Kuzey` and `Merkez` were dropped deliberately, being
        settlement-name fragments rather than court-naming qualifiers, and that
        removal was a decision rather than an accident.
        """
        previously_covered = """
            Adana Adıyaman Afyonkarahisar Ağrı Amasya Ankara Antalya Artvin
            Aydın Balıkesir Bilecik Bingöl Bitlis Bolu Burdur Bursa Çanakkale
            Çankırı Çorum Denizli Diyarbakır Edirne Elazığ Erzincan Erzurum
            Eskişehir Gaziantep Giresun Gümüşhane Hakkari Hatay Isparta Mersin
            İstanbul İzmir Kars Kastamonu Kayseri Kırklareli Kırşehir Kocaeli
            Konya Kütahya Malatya Manisa Kahramanmaraş Mardin Muğla Muş
            Nevşehir Niğde Ordu Rize Sakarya Samsun Siirt Sinop Sivas Tekirdağ
            Tokat Trabzon Tunceli Şanlıurfa Uşak Van Yozgat Zonguldak Aksaray
            Bayburt Karaman Kırıkkale Batman Şırnak Bartın Ardahan Iğdır Yalova
            Karabük Kilis Osmaniye Düzce
            Kadıköy Beşiktaş Çankaya Beyoğlu Şişli Ümraniye Maltepe Bakırköy
            Üsküdar Fatih Keçiören Karşıyaka Bornova Konak Pendik Kartal
            Sarıyer Ataşehir Nilüfer Osmangazi Selçuklu Melikgazi Muratpaşa
            Gaziosmanpaşa Küçükçekmece Büyükçekmece Zeytinburnu Bağcılar
            Esenyurt Tuzla Silivri Gebze Çorlu Yenimahalle Mamak Etimesgut
            Sincan Altındağ Bayrampaşa Beykoz Çekmeköy
            Anadolu Batı Masalkent
        """.split()
        lost = [w for w in previously_covered if not turkish_places.is_venue_place_token(w)]
        self.assertEqual(lost, [], f"the place list narrowed: {lost}")

    def test_every_listed_place_can_be_captured_by_the_prefix(self):
        """The regex class must cover the alphabet the place list actually uses.

        Derived, not enumerated: this walks every entry in every place set and
        asserts the venue prefix can capture it. The specific names that broke
        are symptoms -- the class omitted `â`, so twelve directory districts
        (`Hakkâri`, `Milâs`, `Kâhta`, `İslâhiye`, ...) could never be handed to
        `_venue_prefix_start` and their city shipped beside the pseudonymised
        venue, while `is_venue_place_token` returned True the whole time. The
        list held the place; only the capture half could not reach it.

        Asserting the property rather than the dozen names is what stops the
        next list expansion reopening it, the same shape of guard as the
        derivation's row-count assertion.
        """
        prefix = re.compile(privacy._COURT_PLACE_PREFIX + r"\b\d", re.IGNORECASE)
        sources = (
            turkish_places.DISTRICTS
            | turkish_places.SOURCE_GAP_PLACES
            | turkish_places.VENUE_QUALIFIERS
            | turkish_places.SYNTHETIC_CORPUS_PLACES
        )

        def captured(place: str) -> str | None:
            match = prefix.match(f"{place} 1")
            return match.group("court_place") if match else None

        uncapturable = sorted(place for place in sources if captured(place) != f"{place} ")
        self.assertEqual(uncapturable, [], f"the prefix cannot capture these places: {uncapturable}")

    def test_a_circumflex_place_is_inside_the_court_finding(self):
        """The behaviour the coverage property above protects, end to end."""
        for place in ("Hakkâri", "Milâs", "Kâhta", "İslâhiye"):
            with self.subTest(place=place):
                text = f"{place} 1. Asliye Hukuk Mahkemesi kararı"
                self.assert_inside_a_finding(text, place, court_findings(text))

    def test_collapsing_diacritics_did_not_start_absorbing_scaffolding(self):
        """Widening the key must not widen what counts as a place.

        Collapsing `ı ş ğ ç ö ü` onto ASCII makes more words compare equal, so
        the ordinary Turkish words that sit in front of a venue are re-asserted
        in their undotted spellings -- the ones a scan produces, and the ones a
        collision would surface in.
        """
        for word in ("Dosya", "Ayrica", "Karar", "Mahkeme", "Muvekkil", "Sirketi", "Sayin", "Valiligi"):
            with self.subTest(word=word):
                text = f"Bu hususta {word} 5. Asliye Hukuk Mahkemesi karari incelenmistir."
                self.assertIn("5. Asliye Hukuk Mahkemesi", [f["sample"] for f in court_findings(text)])

    def test_a_bare_place_name_in_prose_is_not_a_court_finding(self):
        """The place list is a span boundary, never a detection category.

        Nothing may emit a finding for a place name on its own: `Ankara` in
        `Davacı Ankara ilinde ikamet etmektedir.` names no venue, and reporting
        it as a court would put a false [COURT_AUTHORITY_n] in the preview.
        """
        text = "Davacı Ankara ilinde ikamet etmektedir."
        profile = analyze_privacy("dilekce.txt", text)
        self.assertEqual([], court_findings(text), "a bare place name is not a venue")
        for finding in profile["risk_map"]:
            self.assertNotEqual("Ankara", finding["sample"].strip())

    def test_two_word_place_prefix_is_still_taken_whole(self):
        """The courthouse qualifier is part of the venue name, so both words.

        `İstanbul Anadolu` names one courthouse complex; a finding that covered
        only `Anadolu` would leave a major city shipping beside a pseudonymised
        court -- the defect this file exists to pin, on the repository's own
        `tests/fixtures/sample_commercial_case.txt`.
        """
        text = "Dosya kapsamında İstanbul Anadolu 4. Asliye Hukuk Mahkemesi nezdinde dava açılmıştır."
        findings = court_findings(text)
        self.assertEqual(len(findings), 1, [f["sample"] for f in findings])
        self.assertEqual(findings[0]["sample"], "İstanbul Anadolu 4. Asliye Hukuk Mahkemesi")

    def test_prefix_does_not_reach_over_a_line_break(self):
        """A court heading on the line after "T.C." keeps only the heading.

        A `sample` containing a newline matches nothing in a DOCX part -- the
        part's text is the concatenation of its `w:t` runs -- so it can never be
        removed from the deliverable however it was reviewed. `_COURT_PLACE_PREFIX`
        separates its words with `[ \\t]+` for exactly this reason, and
        `tests/test_redaction_parity.py` pins the invariant corpus-wide.

        The line above the heading has to END in a capitalised word for this to
        discriminate. Under `\\s+` the prefix would join "BAKANLIĞI" across the
        break; with a preceding "T.C." alone it cannot, because "C." is not a
        word the prefix can match and the probe then passes either way.
        """
        text = "T.C.\nADALET BAKANLIĞI\nANKARA 7. ASLİYE HUKUK MAHKEMESİ\nDURUŞMA TUTANAĞI"
        findings = court_findings(text)
        self.assertTrue(findings)
        for finding in findings:
            self.assertNotIn("\n", finding["sample"])
        self.assertIn("ANKARA 7. ASLİYE HUKUK MAHKEMESİ", [f["sample"] for f in findings])
        self.assert_inside_a_finding(text, "ANKARA", findings)

    def test_prefix_does_not_swallow_the_person_name_in_front_of_the_venue(self):
        """A lawyer named before the venue keeps their own PERSON placeholder.

        build_redacted_preview resolves an overlap by taking the longest span at
        an offset, so a place prefix that reached back over the name would emit
        it as [COURT_AUTHORITY_n] while the same lawyer named elsewhere still
        came out [PERSON_n] -- one person, two pseudonyms in one document.
        """
        text = "Vekili Av. Ekin Düşsel 3. Asliye Hukuk Mahkemesine başvurmuştur."
        profile = analyze_privacy("dilekce.txt", text)
        courts = [f for f in profile["risk_map"] if f["category"] == "court_or_authority"]
        self.assertTrue(courts)
        for finding in courts:
            self.assertNotIn("Ekin", finding["sample"])
            self.assertNotIn("Düşsel", finding["sample"])
        self.assertIn("[PERSON_1]", profile["redacted_preview"])

    def test_prefix_does_not_swallow_the_party_role_in_front_of_the_venue(self):
        """The role word goes, the place stays -- the trim stops at the claim's end.

        Dropping the whole prefix on an overlap instead would put `Ankara` back
        outside every finding, which is the defect this file exists to pin.
        """
        text = "Davacı Ankara 5. Asliye Hukuk Mahkemesinde dava açmıştır."
        findings = court_findings(text)
        self.assertEqual(len(findings), 1, [f["sample"] for f in findings])
        self.assertNotIn("Davacı", findings[0]["sample"])
        self.assert_inside_a_finding(text, "Ankara", findings)

    def test_an_authority_in_front_of_a_venue_keeps_its_own_finding(self):
        """The suppression window is the span the FINDING claims, not the match.

        An earlier round fed `_generic_court_matches` the raw match start, whose
        premise was that raw and trimmed starts differ only over text some other
        finding already claims. The place list broke that premise: a captured
        word the list REJECTS is given back to nobody, and the raw window then
        hid it. `Kaymakamlığı` here is the governorship's own word -- rejected
        by the place list, claimed by no other rule -- and with a raw window it
        lay inside no finding at all, so no reviewer could approve it and the
        exporter could not remove it.

        The cost of the reversal is the nested duplicate asserted below;
        `resolve_redaction_regions` clips nested targets, so it cannot corrupt
        the deliverable.

        `Ümraniye` IS in the place list, and the generic rule now carries the
        place prefix -- yet the finding here is still `Kaymakamlığı 3`. That is
        the claimed-span trim, not a gap in the list: the role-name rule reads
        "Şüpheli Ümraniye Kaymakamlığı" as a person, so `Ümraniye` is already
        inside a natural_person_name finding and `_court_start` gives it back
        rather than letting two findings claim it.

        The coverage assertion is the point of the last line: the SAMPLES being
        right does not by itself prove `Ümraniye` is approvable, and a word
        outside every finding is the leak this file exists to pin. Measured,
        coverage no longer rests on that person false positive alone -- since
        the generic rule gained the place prefix, `Ümraniye` is in DISTRICTS and
        the court span picks it up directly if the person claim goes away, and
        the criminal_allegation sweep covers it if both do. Three independent
        paths, which is why the assertion resists mutation rather than being
        vacuous.
        """
        text = "Şüpheli Ümraniye Kaymakamlığı 3. Sulh Ceza Hakimliği dosyası"
        profile = analyze_privacy("dilekce.txt", text)
        courts = [f for f in profile["risk_map"] if f["category"] == "court_or_authority"]
        self.assertEqual([f["sample"] for f in courts], ["Kaymakamlığı 3", "3. Sulh Ceza Hakimliği"])
        self.assert_inside_a_finding(text, "Kaymakamlığı", courts)
        self.assert_inside_a_finding(text, "Ümraniye", profile["risk_map"])

    def test_generic_suffix_rule_still_yields_to_the_widened_named_span(self):
        """The widened span must not cost an authority its only finding.

        `_named_court_spans` feeds the window `_generic_court_matches` skips,
        and a window wider than the findings it stands for is how an authority
        ends up unreported.

        The authority has to sit ADJACENT to the branch number for this to
        discriminate. This fixture's earlier form put `Cumhuriyet Başsavcılığı`
        a clause away from any numbered venue, so it was never inside the
        suppression window at all and stayed green right through a round in
        which `Valiliği` and `Kaymakamlığı` lost their findings entirely.

        `Şişli Tapu Müdürlüğü` at the front of the sentence has a finding of its
        own now, from `DIRECTORATE_RULE`; before that rule it had none, and the
        district shipped. Asserting it here as well pins that the new named span
        does not widen the window in a way that costs `Valiliği` its finding --
        the directorate ends at `Müdürlüğü`, well before the governorship.
        """
        text = "Şişli Tapu Müdürlüğü ve İstanbul Valiliği 5. Asliye Hukuk Mahkemesi kayıtlarına işlendi"
        findings = court_findings(text)
        samples = [f["sample"] for f in findings]
        self.assertIn("5. Asliye Hukuk Mahkemesi", samples)
        self.assertIn("Şişli Tapu Müdürlüğü", samples)
        self.assert_inside_a_finding(text, "Valiliği", findings)

    def test_a_second_authority_after_a_named_court_keeps_its_finding(self):
        """The other ordering: named court first, authority in its 120-char tail."""
        text = "İstanbul Anadolu 4. Asliye Ceza Mahkemesi kararı Kadıköy Cumhuriyet Başsavcılığı'na gönderilmiştir"
        findings = court_findings(text)
        self.assertIn("İstanbul Anadolu 4. Asliye Ceza Mahkemesi", [f["sample"] for f in findings])
        self.assert_inside_a_finding(text, "Cumhuriyet Başsavcılığı", findings)


class DirectorateAuthorityTests(SpanCoverageAssertions):
    """District plus state directorate is the same re-identifier as city plus court.

    Land registry, population registry, tax office, police and social-security
    directorates are cited by district throughout Turkish case files, and before
    `DIRECTORATE_RULE` every one of the sentences below reached the redacted
    preview completely unchanged: no court rule named these offices, and
    `GENERIC_COURT_SUFFIX_RULE` has no bare `müdürlüğü` trigger. The rule
    carries the shared `_COURT_PLACE_PREFIX`, so the district resolves through
    the same list gate as a court's city.
    """

    REPORTED = (
        ("Taşınmaz Şişli Tapu Müdürlüğü kayıtlarına işlenmiştir.", "Şişli", "Şişli Tapu Müdürlüğü"),
        ("Kadıköy Nüfus Müdürlüğü'nden alınan kayıt örneği eklenmiştir.", "Kadıköy", "Kadıköy Nüfus Müdürlüğü"),
        ("Beşiktaş Vergi Dairesi Müdürlüğü'ne bildirilmiştir.", "Beşiktaş", "Beşiktaş Vergi Dairesi Müdürlüğü"),
        ("Kadıköy İlçe Emniyet Müdürlüğü ifade tutanağı", "Kadıköy", "Kadıköy İlçe Emniyet Müdürlüğü"),
        ("Bakırköy SGK İl Müdürlüğü'ne başvurulmuştur.", "Bakırköy", "Bakırköy SGK İl Müdürlüğü"),
    )

    # The placeholder each one is pseudonymised under. These all shared
    # [COURT_AUTHORITY_1] until the prefix became the institution's type, which
    # is what makes the tax office and the police directorate readable as what
    # they are -- see legal_semantics.institution_type.
    PLACEHOLDERS = {
        "Şişli Tapu Müdürlüğü": "[ADMINISTRATIVE_AUTHORITY_1]",
        "Kadıköy Nüfus Müdürlüğü": "[ADMINISTRATIVE_AUTHORITY_1]",
        "Beşiktaş Vergi Dairesi Müdürlüğü": "[TAX_AUTHORITY_1]",
        "Kadıköy İlçe Emniyet Müdürlüğü": "[LAW_ENFORCEMENT_AUTHORITY_1]",
        "Bakırköy SGK İl Müdürlüğü": "[ADMINISTRATIVE_AUTHORITY_1]",
    }

    def test_each_reported_directorate_covers_its_district(self):
        """The five sentences the gap was reported on, asserted on offsets and preview.

        Offsets say the district is approvable as part of the authority; the
        preview says it is actually replaced. Both, because a finding of the
        right category can exist while the preview still shows the district
        (it did for `Valiliği 5` before the generic rule grew its prefix).
        """
        for text, district, authority in self.REPORTED:
            with self.subTest(authority=authority):
                findings = court_findings(text)
                self.assertEqual([f["sample"] for f in findings], [authority])
                self.assert_inside_a_finding(text, district, findings)
                preview = analyze_privacy("karar.txt", text)["redacted_preview"]
                self.assertNotIn(district, preview)
                self.assertNotIn(authority, preview)
                self.assertIn(self.PLACEHOLDERS[authority], preview)

    def test_every_listed_directorate_form_covers_its_district(self):
        """The forms the list names, including the scanned (ASCII, all-caps) spelling.

        `tesseract` strips Turkish diacritics, and every other court rule
        carries an ASCII alternate for that reason, so the directorate names do
        too -- `KADIKOY NUFUS MUDURLUGU` is what an accepted OCR page of a
        population-register extract reads as.
        """
        for text, district, authority in (
            ("Kadıköy Nüfus ve Vatandaşlık Müdürlüğü kaydı", "Kadıköy", "Kadıköy Nüfus ve Vatandaşlık Müdürlüğü"),
            ("KADIKOY NUFUS MUDURLUGU kaydi", "KADIKOY", "KADIKOY NUFUS MUDURLUGU"),
            ("SISLI TAPU MUDURLUGU yazisi", "SISLI", "SISLI TAPU MUDURLUGU"),
            ("BEŞİKTAŞ VERGİ DAİRESİ MÜDÜRLÜĞÜ", "BEŞİKTAŞ", "BEŞİKTAŞ VERGİ DAİRESİ MÜDÜRLÜĞÜ"),
            ("Beşiktaş Vergi Dairesi Başkanlığı yazısı", "Beşiktaş", "Beşiktaş Vergi Dairesi Başkanlığı"),
            ("Beşiktaş Vergi Dairesi kayıtları", "Beşiktaş", "Beşiktaş Vergi Dairesi"),
            ("İstanbul İl Emniyet Müdürlüğü yazısı", "İstanbul", "İstanbul İl Emniyet Müdürlüğü"),
            ("Bakırköy Sosyal Güvenlik Merkezi'ne başvurulmuştur", "Bakırköy", "Bakırköy Sosyal Güvenlik Merkezi"),
            ("Kadıköy SGK Sosyal Güvenlik Merkezi kaydı", "Kadıköy", "Kadıköy SGK Sosyal Güvenlik Merkezi"),
            ("İstanbul Sosyal Güvenlik İl Müdürlüğü yazısı", "İstanbul", "İstanbul Sosyal Güvenlik İl Müdürlüğü"),
            ("Ankara İl Milli Eğitim Müdürlüğü onayı", "Ankara", "Ankara İl Milli Eğitim Müdürlüğü"),
            ("Çankaya İlçe Milli Eğitim Müdürlüğü onayı", "Çankaya", "Çankaya İlçe Milli Eğitim Müdürlüğü"),
            ("Şişli Tapu Sicil Müdürlüğü kaydı", "Şişli", "Şişli Tapu Sicil Müdürlüğü"),
            ("İstanbul Tapu ve Kadastro Bölge Müdürlüğü yazısı", "İstanbul", "İstanbul Tapu ve Kadastro Bölge Müdürlüğü"),
            ("Şişli Kadastro Müdürlüğü yazısı", "Şişli", "Şişli Kadastro Müdürlüğü"),
        ):
            with self.subTest(authority=authority):
                findings = court_findings(text)
                self.assertEqual([f["sample"] for f in findings], [authority])
                self.assert_inside_a_finding(text, district, findings)

    def test_a_company_department_is_not_an_authority(self):
        """A bare `müdürlüğü` names a department of a company far more often than an office.

        `İNSAN KAYNAKLARI MÜDÜRLÜĞÜ` heads both termination letters in the
        held-out corpus, which is why the rule lists directorates by name rather
        than triggering on the suffix: as a generic-rule alternative the bare
        word made every line below a HIGH court_or_authority finding sweeping
        the clause after it, and still covered none of the reported districts.
        `İstanbul Bölge Satış Müdürlüğü` is the hard one -- a listed place
        followed by a level word -- and it must stay a department.
        """
        for text in (
            "şirketin insan kaynakları müdürlüğü tarafından hazırlanmıştır.",
            "Satış Müdürlüğü raporu ektedir.",
            "İNSAN KAYNAKLARI MÜDÜRLÜĞÜ",
            "Şirketimizin İstanbul Bölge Satış Müdürlüğü tarafından onaylanmıştır.",
            "Finans Müdürlüğü onayı alınmıştır.",
            "Pazarlama Müdürlüğü ile Bilgi İşlem Müdürlüğü arasında yazışma yapılmıştır.",
            "Yönetim kurulu, şirket Genel Müdürlüğü'ne yetki vermiştir.",
        ):
            with self.subTest(text=text):
                self.assertEqual(court_findings(text), [])

    def test_prose_about_a_kind_of_office_is_not_a_named_authority(self):
        """Lowercase is a reference to the kind of office, not a named one.

        `ilgili vergi dairesine` names no office and carries nothing to
        pseudonymise; reporting it would add a HIGH finding to ordinary
        procedural prose. The institution's first word has to be capitalised.
        """
        for text in (
            "Beyanname ilgili vergi dairesine süresinde verilmiştir.",
            "Taşınmazın devri için tapu müdürlüğüne başvurulur.",
            "Kayıt örneği nüfus müdürlüğünden alınabilir.",
        ):
            with self.subTest(text=text):
                self.assertEqual(court_findings(text), [])

    def test_a_bare_tax_office_label_is_not_a_named_authority(self):
        """A bare `Vergi Dairesi` with no listed place in front is a field label.

        In a party block the label is followed by the district after a colon;
        pseudonymising the label while the district ships makes the output look
        handled, so the finding there is the district alone
        (`TAX_OFFICE_FIELD_RULE`) and never the label. An unlisted word in
        front (`Alsancak`) leaves it just as bare. With a listed district, and
        in the longer forms, it is still an office.
        """
        for text in (
            "Vergi Dairesi ve Vergi No bilgileri eksiktir.",
            "Alsancak Vergi Dairesi kayıtları",
        ):
            with self.subTest(text=text):
                self.assertEqual(court_findings(text), [])
        for text, expected in (
            ("Satıcı Demir Tekstil A.Ş., Vergi Dairesi: Kadıköy, VKN: 1234567890.", "Kadıköy"),
            ("Kadıköy Vergi Dairesi kayıtları", "Kadıköy Vergi Dairesi"),
            ("Vergi Dairesi Müdürlüğü yazısı", "Vergi Dairesi Müdürlüğü"),
            ("Vergi Dairesi Başkanlığı yazısı", "Vergi Dairesi Başkanlığı"),
        ):
            with self.subTest(text=text):
                self.assertEqual([f["sample"] for f in court_findings(text)], [expected])

    def test_an_unlisted_word_before_a_directorate_stays_out_of_the_span(self):
        """The list gate decides the district, exactly as it does for a court.

        `Alsancak` is a real neighbourhood the list keeps out on purpose (see
        `turkish_places`), `Dosya` and `Taşınmaz` are ordinary capitalised
        words, and a district on the line above is not on the same line. Each
        must be left outside the span rather than deleted from the deliverable
        as part of the authority.
        """
        for text, expected in (
            ("Alsancak Tapu Müdürlüğü kaydı", "Tapu Müdürlüğü"),
            ("Dosya Tapu Müdürlüğü tarafından gönderildi", "Tapu Müdürlüğü"),
            ("Taşınmaz Şişli Tapu Müdürlüğü kayıtlarına işlenmiştir.", "Şişli Tapu Müdürlüğü"),
            ("Şişli\nTapu Müdürlüğü kaydı", "Tapu Müdürlüğü"),
        ):
            with self.subTest(text=text):
                self.assertEqual([f["sample"] for f in court_findings(text)], [expected])

    def test_a_case_suffix_is_outside_the_span_and_the_pseudonym(self):
        """`Müdürlüğüne` and `Müdürlüğü'nden` are one office, not two.

        The span ends at the stem, so the folded core the pseudonym is keyed on
        does not change with the grammatical case of the sentence around it.
        """
        text = (
            "Şişli Tapu Müdürlüğüne başvurulmuştur. "
            "Şişli Tapu Müdürlüğü'nden cevap gelmiştir. "
            "Şişli Tapu Müdürlüğünce işlem yapılmıştır."
        )
        findings = court_findings(text)
        self.assertEqual([f["sample"] for f in findings], ["Şişli Tapu Müdürlüğü"] * 3)
        self.assertEqual({f["placeholder"] for f in findings}, {"[ADMINISTRATIVE_AUTHORITY_1]"})


class VenuePseudonymIdentityTests(SpanCoverageAssertions):
    """One venue is one pseudonym; two venues are never one.

    The pseudonym is keyed on the folded venue CORE, so a venue named with its
    city once and bare afterwards stays one identity. Keying on the core alone
    would collapse two cities' courts that share a core into one placeholder,
    asserting they are the same venue -- so a group unifies only when it carries
    at most one distinct place.
    """

    def placeholders(self, text: str) -> list[str]:
        return [f["placeholder"] for f in court_findings(text)]

    def test_the_same_venue_named_with_and_without_its_city_is_one_pseudonym(self):
        text = (
            "Masalkent 3. Asliye Hukuk Mahkemesi\n"
            "Davacı, 3. Asliye Hukuk Mahkemesi kararına itiraz etmiştir."
        )
        self.assertEqual(len(self.placeholders(text)), 2)
        self.assertEqual(set(self.placeholders(text)), {"[COURT_1]"})

    def test_two_cities_sharing_a_venue_core_stay_two_pseudonyms(self):
        """The mirror-image defect, which is worse than the one it fixes.

        `Ankara 5. Asliye Hukuk Mahkemesi` and `İzmir 5. Asliye Hukuk Mahkemesi`
        fold to the same core. Unifying them would make the delivered document
        state that two courts in two different cities are one venue.
        """
        text = "Ankara 5. Asliye Hukuk Mahkemesi ve İzmir 5. Asliye Hukuk Mahkemesi kararları."
        self.assertEqual(
            self.placeholders(text), ["[COURT_1]", "[COURT_2]"]
        )

    def test_one_city_plus_bare_mentions_unify_across_the_whole_document(self):
        text = (
            "Ankara 5. Asliye Hukuk Mahkemesi. Dosya 5. Asliye Hukuk Mahkemesi "
            "gönderildi. Ankara 5. Asliye Hukuk Mahkemesi kararı."
        )
        self.assertEqual(len(self.placeholders(text)), 3)
        self.assertEqual(set(self.placeholders(text)), {"[COURT_1]"})

    def test_a_directorate_named_with_and_without_its_district_is_one_pseudonym(self):
        """The directorate goes through the same identity keying as a court."""
        text = (
            "Taşınmaz Şişli Tapu Müdürlüğü kayıtlarına işlenmiştir. "
            "Tapu Müdürlüğü yazısı dosyaya eklenmiştir."
        )
        self.assertEqual(len(self.placeholders(text)), 2)
        self.assertEqual(set(self.placeholders(text)), {"[ADMINISTRATIVE_AUTHORITY_1]"})

    def test_two_districts_directorates_stay_two_pseudonyms(self):
        """Şişli's land registry and Kadıköy's are two offices, not one."""
        text = "Şişli Tapu Müdürlüğü ve Kadıköy Tapu Müdürlüğü kayıtları karşılaştırılmıştır."
        self.assertEqual(
            self.placeholders(text), ["[ADMINISTRATIVE_AUTHORITY_1]", "[ADMINISTRATIVE_AUTHORITY_2]"]
        )


# Each surname is also an ordinary word or the prefix of one, written in the
# same document lower-case or inflected.
SURNAME_PREFIX_DOCUMENTS = (
    (
        "Davacı Ayşe Bor vekili aracılığıyla dava açmıştır. Davalının borcunu ödemediği sabittir. "
        "Bor Ankara 2. Asliye Hukuk Mahkemesine başvurulmuştur.",
        "borcunu",
    ),
    (
        "Davacı Ekin Kale vekili aracılığıyla dava açmıştır. Kale 5. Asliye Hukuk Mahkemesine "
        "dilekçe sunulmuş, mahkeme kalemine teslim edilmiştir.",
        "kalemine",
    ),
    (
        "Davacı Umut Can vekili aracılığıyla dava açmıştır. Can 2. Asliye Hukuk Mahkemesine "
        "başvurulmuş, can güvenliği tehlikededir.",
        "can güvenliği",
    ),
)


# A one-word party name with a province read into it, then a later venue in
# that province.
PROVINCE_READ_INTO_A_NAME_DOCUMENTS = (
    (
        "Müvekkil Kartal Ankara 5. Asliye Hukuk Mahkemesinde. "
        "Dosya Ankara 3. Asliye Hukuk Mahkemesine gönderildi.",
        "Ankara",
    ),
    (
        "Tanık Mehmet Bursa 2. Asliye Ceza Mahkemesinde dinlenmiştir. "
        "Dosya Bursa 4. Asliye Ceza Mahkemesine gönderildi.",
        "Bursa",
    ),
    (
        "Davacı Ali Ankara 5. Asliye Hukuk Mahkemesinde dava açmıştır. "
        "Dosya Ankara 3. Asliye Hukuk Mahkemesine gönderildi.",
        "Ankara",
    ),
)


class SurnameBeforeAVenueTests(SpanCoverageAssertions):
    """A party's bare surname in front of a venue is the party, not the place.

    Sourcing districts from the Ministry directory made many of them ordinary
    surnames too, so "Davacı Ekin Kartal ... Kartal 5. Asliye Hukuk
    Mahkemesine" read the second "Kartal" as the venue's district: [PERSON_1]
    for the full name and the bare surname inside [COURT_1] -- one person, two
    pseudonyms in one document. Each test asserts the surname's placement on
    offsets, inside a natural_person_name finding carrying the full name's
    placeholder; the guards ride in the same documents, because a guard is only
    meaningful where the giveback is active.

    Every name here is invented for this file; the surnames are chosen because
    they are on the place list.
    """

    def persons(self, findings: list[dict]) -> list[dict]:
        return [f for f in findings if f["category"] == "natural_person_name"]

    def courts(self, findings: list[dict]) -> list[dict]:
        return [f for f in findings if f["category"] == "court_or_authority"]

    def assert_mention_of(self, text: str, findings: list[dict], full_name: str, offset: int, surname: str):
        """`surname` at `offset` is a person finding under `full_name`'s placeholder."""
        persons = self.persons(findings)
        named = [f for f in persons if f["sample"] == full_name]
        self.assertTrue(named, f"{full_name!r} was not detected: {[f['sample'] for f in persons]}")
        mention = [f for f in persons if f["start"] <= offset and offset + len(surname) <= f["end"]]
        self.assertTrue(
            mention,
            f"{surname!r} at {offset} is in no person finding: "
            f"{[(f['category'], f['sample'], f['start'], f['end']) for f in findings]}",
        )
        self.assertEqual({f["placeholder"] for f in mention}, {named[0]["placeholder"]})
        for court in self.courts(findings):
            self.assertFalse(
                court["start"] <= offset < court["end"],
                f"{surname!r} at {offset} is still inside the court finding {court['sample']!r}",
            )

    def test_a_bare_surname_before_a_venue_keeps_the_persons_pseudonym(self):
        for surname in ("Kartal", "Kiraz", "Çınar", "Ceyhan", "Sakarya", "Seyhan"):
            with self.subTest(surname=surname):
                # Otherwise the venue would never have absorbed it and the
                # test would prove nothing.
                self.assertTrue(turkish_places.is_venue_place_token(surname))
                text = (
                    f"Davacı Ekin {surname} vekili aracılığıyla dava açmıştır. "
                    f"{surname} 5. Asliye Hukuk Mahkemesine dilekçe sunmuştur."
                )
                findings = analyze_privacy("dilekce.txt", text)["risk_map"]
                self.assert_mention_of(text, findings, f"Ekin {surname}", text.rindex(surname), surname)
                self.assertTrue(self.courts(findings), "the venue itself lost its finding")

    def test_a_surname_in_front_of_a_place_gives_back_only_the_surname(self):
        """"Kartal Ankara 5. ..." -- Kartal goes to the person, Ankara stays the venue's."""
        text = (
            "Davacı Ekin Kartal vekili aracılığıyla dava açmıştır. "
            "Kartal Ankara 5. Asliye Hukuk Mahkemesine dilekçe sunmuştur."
        )
        findings = analyze_privacy("dilekce.txt", text)["risk_map"]
        self.assert_mention_of(text, findings, "Ekin Kartal", text.rindex("Kartal"), "Kartal")
        self.assert_inside_a_finding(text, "Ankara", self.courts(findings))

    def test_a_surname_behind_a_place_is_not_given_back(self):
        """"Ankara Kartal 3. ..." -- releasing Kartal would orphan Ankara.

        A claim makes the venue start after its end, so giving back a surname
        that stands to the RIGHT of a genuine place would leave the place in no
        finding at all, shipping in cleartext. Only the leftmost run goes back.
        """
        text = (
            "Davacı Ekin Kartal vekili aracılığıyla dava açmıştır. "
            "Kartal 5. Asliye Hukuk Mahkemesine dilekçe sunmuştur. "
            "Dosya Ankara Kartal 3. Asliye Hukuk Mahkemesinden gönderilmiştir."
        )
        findings = analyze_privacy("dilekce.txt", text)["risk_map"]
        self.assert_mention_of(text, findings, "Ekin Kartal", text.index("Kartal 5."), "Kartal")
        self.assert_inside_a_finding(text, "Ankara", self.courts(findings))

    def test_a_district_that_is_no_partys_surname_stays_the_venues(self):
        """The giveback is keyed on THIS document's people, not on the word."""
        text = (
            "Davacı Ekin Kartal vekili aracılığıyla dava açmıştır. "
            "Kartal 5. Asliye Hukuk Mahkemesine dilekçe sunmuştur. "
            "Dosya daha önce Ceyhan 1. Asliye Hukuk Mahkemesinde görülmüştür."
        )
        findings = analyze_privacy("dilekce.txt", text)["risk_map"]
        self.assert_mention_of(text, findings, "Ekin Kartal", text.index("Kartal 5."), "Kartal")
        self.assert_inside_a_finding(text, "Ceyhan", self.courts(findings))
        self.assertFalse([f for f in self.persons(findings) if "Ceyhan" in f["sample"]])

    def test_a_city_read_into_a_name_does_not_become_a_surname(self):
        """The role-name regex reads "Davacı Ilgın Sevgen Ankara 5. ..." as a person.

        Counting "Ankara" as that person's surname would make every later
        Ankara venue a mention of them and take the city out of the court.
        """
        text = (
            "Davacı Ilgın Sevgen Ankara 5. Asliye Hukuk Mahkemesinde dava açmıştır. "
            "Davalı Ekin Kartal cevap vermiştir. "
            "Kartal Ankara 3. Asliye Hukuk Mahkemesine itiraz etmiştir."
        )
        findings = analyze_privacy("dilekce.txt", text)["risk_map"]
        self.assertIn("Ilgın Sevgen Ankara", [f["sample"] for f in self.persons(findings)])
        self.assert_mention_of(text, findings, "Ekin Kartal", text.index("Kartal Ankara"), "Kartal")
        third_venue = text.index("Ankara 3.")
        self.assertTrue(
            any(f["start"] <= third_venue < f["end"] for f in self.courts(findings)),
            [(f["category"], f["sample"], f["start"]) for f in findings],
        )

    def test_a_full_name_in_front_of_a_venue_still_establishes_its_surname(self):
        """"Av. Selim Çınar 3. Asliye ..." is the lawyer's only full mention.

        The venue's place capture reaches back over any name in front of it, so
        reading that overlap as "a city read into the name" left Çınar
        unestablished and the later bare "Çınar" inside the court finding.
        """
        text = (
            "Av. Selim Çınar 3. Asliye Hukuk Mahkemesine sundu. "
            "Çınar Ankara 6. Asliye Hukuk Mahkemesine sundu."
        )
        findings = analyze_privacy("dilekce.txt", text)["risk_map"]
        self.assert_mention_of(text, findings, "Selim Çınar", text.rindex("Çınar"), "Çınar")
        self.assert_inside_a_finding(text, "Ankara", self.courts(findings))

    def test_the_rendering_replaces_only_the_capitalised_whole_word(self):
        """A bare surname is often an ordinary word or its prefix.

        The canvas resolves approved samples by string match widened to word
        bounds, so an approved "Bor" rewrote "borcunu" as the person.
        """
        for text, ordinary in SURNAME_PREFIX_DOCUMENTS:
            with self.subTest(ordinary=ordinary):
                risk_map = analyze_privacy("dilekce.txt", text)["risk_map"]
                findings = [
                    dict(
                        f,
                        id=index,
                        review_status="approved",
                        source="detector",
                        start_offset=f["start"],
                        end_offset=f["end"],
                    )
                    for index, f in enumerate(risk_map)
                ]
                rendered = "".join(segment.text for segment in plan_segments(text, findings, {"approved"}))
                self.assertIn(ordinary, rendered)
                self.assertEqual(rendered.count("[PERSON_1]"), 2, rendered)

    def test_the_province_list_is_the_81_directory_provinces(self):
        self.assertEqual(len(turkish_places.PROVINCES), 81)
        self.assertLessEqual(turkish_places.PROVINCES, turkish_places.DISTRICTS)

    def test_a_province_read_into_a_two_word_name_does_not_become_a_surname(self):
        """"Davacı Ali Ankara 5. ..." is the person "Ali Ankara"; Ankara stays a city."""
        for text, city in PROVINCE_READ_INTO_A_NAME_DOCUMENTS:
            with self.subTest(city=city):
                findings = analyze_privacy("dilekce.txt", text)["risk_map"]
                self.assertFalse(
                    [f for f in self.persons(findings) if f["sample"] == city],
                    [(f["category"], f["sample"]) for f in findings],
                )
                later_venue = text.rindex(city)
                self.assertTrue(
                    any(f["start"] <= later_venue < f["end"] for f in self.courts(findings)),
                    [(f["category"], f["sample"], f["start"]) for f in findings],
                )

    def test_a_shared_surname_goes_to_the_nearest_person_named_before_it(self):
        text = (
            "Davacı Ekin Kartal ile davalı Deniz Kartal arasındaki uyuşmazlıkta "
            "Kartal 5. Asliye Hukuk Mahkemesine cevap dilekçesi sunmuştur."
        )
        findings = analyze_privacy("dilekce.txt", text)["risk_map"]
        placeholders = {f["sample"]: f["placeholder"] for f in self.persons(findings)}
        self.assertNotEqual(placeholders.get("Ekin Kartal"), placeholders.get("Deniz Kartal"))
        self.assert_mention_of(text, findings, "Deniz Kartal", text.rindex("Kartal"), "Kartal")


class BarRegistrationSpanTests(SpanCoverageAssertions):
    def test_baro_is_inside_the_bar_registration_finding(self):
        for text in (
            "Vekil Baro Sicil No: 45678 ile kayıtlıdır.",
            "BARO SİCİL NO: 45678",
            "Av. Selin Aydın, Baro Sicil Numarası 12345",
        ):
            with self.subTest(text=text):
                findings = findings_of(text, "bar_registration_number")
                self.assertTrue(findings, "no bar_registration_number finding at all")
                needle = "Baro" if "Baro" in text else "BARO"
                self.assert_inside_a_finding(text, needle, findings)

    def test_the_possessive_barosu_and_its_city_are_inside_the_finding(self):
        """The standard Turkish form, and the same list gate as the courts.

        `İstanbul Barosu Sicil No: 45678` is how a Turkish filing names a bar
        registration. The rule reached only the bare nominative `Baro`, so this
        matched from `Sicil` and left `İstanbul Barosu` outside every finding of
        any category -- the association and its city shipping in cleartext
        beside a pseudonymised number, which is the court defect verbatim.

        The city joins because `turkish_places` recognises it, not because of
        where it sits: the last case puts an unrelated capitalised word in
        exactly that position and it must stay out.

        The parenthesised form alone was NOT enough to pin this. The `(` stops
        PERSON_CONTEXT_RE's capture short, so that fixture could not express the
        violation it was named for; the two signature-block layouts below are
        the shapes where the person regex crosses the line break, claims
        `İstanbul Barosu Sicil No`, and is then itself discarded -- leaving the
        city covered by nothing at all. Asserted on OFFSETS, because the point
        is coverage rather than which rule provides it.
        """
        for text in (
            "Av. Mehmet Kaya (İstanbul Barosu Sicil No: 45678)",
            "Av. Mehmet Kaya İstanbul Barosu Sicil No: 45678",
            "VEKİLİ: Av.\nİstanbul Barosu Sicil No: 45678",
            "Vekili Av. \nİstanbul Barosu Sicil No: 45678",
            "Sayın Av. Mehmet Kaya\nİstanbul Barosu Sicil No: 45678",
        ):
            with self.subTest(layout=text.replace("\n", "\\n")):
                self.assert_inside_a_finding(text, "İstanbul", analyze_privacy("dilekce.txt", text)["risk_map"])

        plain = "Av. Mehmet Kaya (İstanbul Barosu Sicil No: 45678)"
        self.assertEqual(
            [f["sample"] for f in findings_of(plain, "bar_registration_number")],
            ["İstanbul Barosu Sicil No: 45678"],
        )

        unlisted = "Vekil Barosu Sicil No: 45678"
        self.assertEqual(
            [f["sample"] for f in findings_of(unlisted, "bar_registration_number")],
            ["Barosu Sicil No: 45678"],
        )

    def test_one_registration_number_is_one_pseudonym(self):
        """A registration named twice is one lawyer, however each mention reads.

        Widening this rule's span to carry `İstanbul Barosu` changed the matched
        string, and the pseudonym was keyed on that string -- so a signature
        block writing the full form and an annex writing the bare `Sicil No:`
        delivered [BAR_REGISTRATION_1] and [BAR_REGISTRATION_2], asserting two
        lawyers' registrations where the document names one. Keying on the
        number, as the venue grouping keys on the branch number, fixes it.
        """
        for label, text in (
            ("full then bare", "Av. Mehmet Kaya, Baro Sicil No: 45678\nEk olarak Sicil No: 45678 kaydı sunulmuştur."),
            ("city then plain", "İstanbul Barosu Sicil No: 45678 ile Baro Sicil No: 45678 aynı kayıttır."),
        ):
            with self.subTest(probe=label):
                findings = findings_of(text, "bar_registration_number")
                self.assertEqual(len(findings), 2, [f["sample"] for f in findings])
                self.assertEqual({f["placeholder"] for f in findings}, {"[BAR_REGISTRATION_1]"})

    def test_two_different_registrations_stay_two_pseudonyms(self):
        """The collision guard, the direction that matters more.

        Keying on the number alone would be right for the case above and wrong
        here twice over: two different numbers are two lawyers, and the same
        digits under two different bar associations are also two lawyers, since
        registration numbers are issued per bar.
        """
        for label, text in (
            ("different numbers", "Baro Sicil No: 45678 ve Baro Sicil No: 99887 kayıtlıdır."),
            ("same digits, two bars", "İstanbul Barosu Sicil No: 45678 ve Ankara Barosu Sicil No: 45678 kayıtlıdır."),
        ):
            with self.subTest(probe=label):
                findings = findings_of(text, "bar_registration_number")
                self.assertEqual(len(findings), 2, [f["sample"] for f in findings])
                self.assertEqual(
                    len({f["placeholder"] for f in findings}), 2,
                    [(f["sample"], f["placeholder"]) for f in findings],
                )

    def test_shapes_without_the_word_baro_still_match_unchanged(self):
        """The leading `baro` is optional, so neither older shape regresses."""
        for text, expected in (
            ("baro no: 123 olarak bildirilmiştir.", "baro no: 123"),
            ("Personel sicil numarası 884411 kayıtlıdır.", "sicil numarası 884411"),
        ):
            with self.subTest(text=text):
                samples = [f["sample"] for f in findings_of(text, "bar_registration_number")]
                self.assertEqual(samples, [expected])


class ExportHarness(unittest.TestCase):
    """A DOCX through the real review and export HTTP surfaces.

    The route is the one the accuracy assessment used: extract, persist the
    detector's findings, log in as a reviewer, decide, `mark_redacted`,
    `approve`, then download the reviewed DOCX and read its `w:t` runs. Reading
    the runs rather than re-extracting is deliberate -- the runs are what a
    reader of the file sees, and no extraction step sits between them and the
    assertion.
    """

    def install_document(self, docx_path: Path):
        """Point the app at a fresh temporary database holding `docx_path`.

        Callable more than once per test. Each call captures the globals it is
        about to overwrite in a closure rather than on `self`: storing them on
        the instance made the second call record the FIRST call's temporary
        path as "the original", so the last cleanup restored a TemporaryDirectory
        it had already deleted and leaked it to every later test module.
        """
        self.tmp = TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)
        self.db_path = self.tmp_path / "court_span.db"
        previous_db_path = app_module.DB_PATH
        previous_export_dir = app_module.EXPORT_DIR
        db_path = self.db_path
        app_module.DB_PATH = db_path
        app_module.EXPORT_DIR = self.tmp_path / "exports"
        app_module.READY_DB_PATHS.discard(db_path.resolve())

        def restore():
            app_module.DB_PATH = previous_db_path
            app_module.EXPORT_DIR = previous_export_dir
            app_module.READY_DB_PATHS.discard(db_path.resolve())

        self.addCleanup(restore)
        self.addCleanup(self.tmp.cleanup)

        self.source_text, warning, _ = extract_docx_text(docx_path, 200_000)
        self.profile = analyze_privacy(docx_path.name, self.source_text, warning)

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.executescript(Path("db/schema.sql").read_text(encoding="utf-8"))
        for name, name_tr, icon in CATEGORIES:
            conn.execute("INSERT OR IGNORE INTO categories (name, name_tr, icon) VALUES (?, ?, ?)", (name, name_tr, icon))
            category_id = conn.execute("SELECT id FROM categories WHERE name = ?", (name,)).fetchone()[0]
            for sub_name, sub_name_tr in SUBCATEGORIES[name]:
                conn.execute(
                    "INSERT OR IGNORE INTO subcategories (category_id, name, name_tr) VALUES (?, ?, ?)",
                    (category_id, sub_name, sub_name_tr),
                )
        conn.execute(
            "INSERT INTO users (username, password_hash, role) VALUES (?, ?, ?)",
            ("reviewer", generate_password_hash("secret"), "reviewer"),
        )
        conn.execute(
            """
            INSERT INTO documents (
                id, filename, filepath, file_extension, file_size, title, extraction_status,
                privacy_profile, residual_risk, risk_summary, recommended_strategy,
                external_llm_readiness, human_review_required, redaction_status,
                review_status, ocr_status
            ) VALUES (1, ?, ?, '.docx', ?, 'dilekce', 'Complete', ?, ?, ?, ?, ?, 1, ?, 'pending_review', 'not_required')
            """,
            (
                docx_path.name,
                str(docx_path),
                docx_path.stat().st_size,
                json.dumps(self.profile),
                self.profile["residual_risk"]["level"],
                self.profile["residual_risk"]["summary"],
                self.profile["recommended_strategy"],
                self.profile["external_llm_readiness"],
                self.profile["redaction_status"],
            ),
        )
        app_module.replace_detector_findings(
            conn, 1, self.profile["risk_map"], "detector", "word/document.xml", self.source_text
        )
        conn.execute("INSERT OR IGNORE INTO matters (id, name, status) VALUES (1, 'Unassigned', 'active')")
        conn.execute("INSERT OR IGNORE INTO document_matters (document_id, matter_id) VALUES (1, 1)")
        conn.commit()
        conn.close()

        self.client = app_module.app.test_client()
        self.client.post("/login", json={"username": "reviewer", "password": "secret"})

    def decide(self, action: str, **selector):
        response = self.client.post(
            "/api/document/1/findings/review-batch",
            json={"action": action, "only_pending": True, **selector},
        )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        return response.get_json()["updated"]

    def exported_runs(self) -> list[str]:
        """Every `w:t` run of the reviewed DOCX the export endpoint returns."""
        self.decide("approve")
        for action in ("mark_redacted", "approve"):
            response = self.client.post("/api/document/1/review", json={"action": action})
            self.assertEqual(response.status_code, 200, response.get_data(as_text=True))

        response = self.client.get("/api/document/1/redacted-export?format=docx")
        # The body is a DOCX, so it is only decodable as text when the request
        # failed and Flask returned a JSON error instead.
        self.assertEqual(response.status_code, 200, response.get_data()[:400])
        exported = self.tmp_path / "reviewed.docx"
        exported.write_bytes(response.get_data())
        response.close()

        runs: list[str] = []
        with ZipFile(exported) as archive:
            for part in archive.namelist():
                if not part.endswith(".xml"):
                    continue
                root = ElementTree.fromstring(archive.read(part))
                runs.extend(node.text or "" for node in root.iter(f"{{{W_NAMESPACE}}}t"))
        return runs

class DeliveredFileTests(ExportHarness):
    """The bundled sample every new user opens first.

    `samples/documents/turkish_petition.docx` names `Masalkent 3. Asliye Hukuk
    Mahkemesi`. Before the place word was inside the court's span the
    deliverable read `Masalkent [COURT_AUTHORITY_1]`: the court pseudonymised,
    the place it sits in shipped in cleartext.
    """

    def setUp(self):
        self.assertTrue(SAMPLE_DOCX.exists(), f"bundled sample missing: {SAMPLE_DOCX}")
        self.install_document(SAMPLE_DOCX)

    def test_the_place_word_is_gone_from_the_delivered_runs(self):
        runs = self.exported_runs()
        self.assertTrue(runs, "the exported DOCX has no text runs at all")
        # The sample's own court is "Masalkent 3. Asliye Hukuk Mahkemesi". The
        # court word is asserted alongside the place so this cannot pass on an
        # export that lost the whole paragraph rather than redacting it.
        self.assertIn("[COURT_1]", "".join(runs))
        for run in runs:
            self.assertNotIn("Masalkent", run, f"the venue's place word ships in the deliverable: {run!r}")

    def test_the_court_paragraph_is_a_single_placeholder(self):
        """Not `Masalkent [COURT_AUTHORITY_1]` -- the measured before-state.

        Pinning the whole line keeps this from passing on an export that merely
        moved the leak somewhere else in the paragraph.
        """
        runs = self.exported_runs()
        court_lines = [run for run in runs if "[COURT_1]" in run]
        self.assertEqual(court_lines, ["[COURT_1]"], runs)


class RetainedCompanyExportTests(ExportHarness):
    """A retained identifier must survive the export WHOLE.

    `redaction_targets_for_document` removes a finding's `sample` from the DOCX
    by literal string match, so a court span that reached back over the company
    in front of it takes the company's last word with it. The reviewer here
    makes the two decisions AGENTS.md says the reviewed export must honour
    separately -- retain the company, approve the court -- and a court finding
    reading `Şirketi 3. Asliye Hukuk Mahkemesi` delivers
    `Ada Lojistik Limited [COURT_AUTHORITY_1]`: the retained name silently
    truncated, with no gate or QA signal that anything was lost.

    The company is invented for this file.
    """

    SENTENCE = "Ada Lojistik Limited Şirketi 3. Asliye Hukuk Mahkemesi kararına itiraz etti."

    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        source = Path(tmp.name) / "itiraz.docx"
        make_docx(source, self.SENTENCE)
        self.install_document(source)

    def test_a_retained_company_is_not_truncated_by_the_court_span(self):
        retained = self.decide("retain", categories=["company_name"])
        self.assertEqual(retained, 1, "no company_name finding to retain")
        runs = self.exported_runs()
        delivered = "".join(runs)

        self.assertIn("[COURT_1]", delivered, runs)
        self.assertIn("Ada Lojistik Limited Şirketi", delivered, runs)
        self.assertNotIn("Şirketi 3.", delivered, runs)


class GenericAuthorityDeliveredFileTests(ExportHarness):
    """The three repository fixtures the generic-rule gap was measured on.

    Offsets are not the defect; the delivered file is. Each of these opens with
    a city in front of an authority that `GENERIC_COURT_SUFFIX_RULE` matches,
    and before the rule carried a place prefix each city sat outside every
    finding -- so a reviewer could not approve it and the exporter could not
    remove it, and the city shipped beside a pseudonymised authority.

    `ocr_scan_noisy.txt` is the one that matters most: `blueprints/ocr.py` runs
    `analyze_privacy` on accepted OCR text, so that path is live, and tesseract
    is what produces the diacritic-stripped `IZMIR CUMHURIYET BASSAVCILIGI`.

    Each fixture's text is carried into a DOCX and driven through the real
    review and export endpoints, so what is asserted is the `w:t` runs a reader
    of the delivered file sees.
    """

    FIXTURES = {
        "criminal_investigation.txt": "İstanbul",
        "corporate_resolution.txt": "İstanbul",
        "ocr_scan_noisy.txt": "IZMIR",
    }

    # Two prosecutor's offices and a trade registry, each under its own type.
    FIXTURE_PLACEHOLDERS = {
        "criminal_investigation.txt": "[PROSECUTOR_OFFICE_1]",
        "corporate_resolution.txt": "[ADMINISTRATIVE_AUTHORITY_1]",
        "ocr_scan_noisy.txt": "[PROSECUTOR_OFFICE_1]",
    }

    HIGH_COURT_SENTENCE = "Ankara Bölge İdare Mahkemesi kararı tebliğ edildi"

    def delivered_runs(self, fixture: str) -> list[str]:
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        source = Path(tmp.name) / "fixture.docx"
        text = (PROJECT_DIR / "tests" / "fixtures" / fixture).read_text(encoding="utf-8")
        make_docx(source, "</w:t></w:r></w:p><w:p><w:r><w:t>".join(text.splitlines()))
        self.install_document(source)
        return self.exported_runs()

    def test_the_city_in_front_of_an_authority_is_gone_from_the_delivered_runs(self):
        for fixture, city in self.FIXTURES.items():
            with self.subTest(fixture=fixture):
                delivered = "".join(self.delivered_runs(fixture))
                self.assertIn(self.FIXTURE_PLACEHOLDERS[fixture], delivered)
                self.assertNotIn(city, delivered)

    def test_a_regional_courts_city_is_gone_from_the_delivered_runs(self):
        """Rule (d) proven on the export, not only on offsets.

        A finding's offsets can be right while the delivered file still carries
        the word, which is why this file has an export layer at all.
        """
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        source = Path(tmp.name) / "bim.docx"
        make_docx(source, self.HIGH_COURT_SENTENCE)
        self.install_document(source)

        delivered = "".join(self.exported_runs())
        self.assertIn("[COURT_1]", delivered)
        self.assertNotIn("Ankara", delivered)

    def test_a_directorates_district_is_gone_from_the_delivered_runs(self):
        """The five reported directorate sentences, proven on the export."""
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        source = Path(tmp.name) / "directorates.docx"
        sentences = [text for text, _district, _authority in DirectorateAuthorityTests.REPORTED]
        make_docx(source, "</w:t></w:r></w:p><w:p><w:r><w:t>".join(sentences))
        self.install_document(source)

        delivered = "".join(self.exported_runs())
        self.assertIn("[ADMINISTRATIVE_AUTHORITY_1]", delivered)
        for _text, district, authority in DirectorateAuthorityTests.REPORTED:
            with self.subTest(authority=authority):
                self.assertNotIn(district, delivered)
                self.assertNotIn(authority, delivered)


class TaxOfficeFieldDeliveredFileTests(ExportHarness):
    """A party block's "Vergi Dairesi:" value, proven on the export.

    The district after the label shipped beside a pseudonymised tax number.
    Driven through the real review and export endpoints, so what is asserted
    is the `w:t` runs of the delivered file: the label stays and the district
    is gone from it.
    """

    PARTY_BLOCK = (
        "Satıcı: Demir Tekstil A.Ş.",
        "Vergi Dairesi: Kadıköy, VKN: 1111111114",
        "Alıcı: Ada Lojistik Ltd. Şti.",
        "Vergi Dairesi: Çankaya VKN: 2222222224",
    )

    def test_the_district_after_the_label_is_gone_from_the_delivered_runs(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        source = Path(tmp.name) / "party_block.docx"
        make_docx(source, "</w:t></w:r></w:p><w:p><w:r><w:t>".join(self.PARTY_BLOCK))
        self.install_document(source)

        delivered = "".join(self.exported_runs())
        self.assertIn("Vergi Dairesi: [TAX_AUTHORITY_1]", delivered)
        self.assertIn("Vergi Dairesi: [TAX_AUTHORITY_2]", delivered)
        for district in ("Kadıköy", "Çankaya"):
            with self.subTest(district=district):
                self.assertNotIn(district, delivered)


class SurnameBeforeAVenueDeliveredFileTests(ExportHarness):
    """The surname giveback proven on the export, not only on offsets.

    Before it, the deliverable read "[PERSON_1] ... [COURT_1]": the bare
    surname vanished into the court's placeholder, so a reader following the
    party lost them at exactly the sentence that says where they filed.
    """

    SENTENCE = (
        "Davacı Ekin Kartal vekili aracılığıyla dava açmıştır. "
        "Kartal 5. Asliye Hukuk Mahkemesi kararına itiraz etmiştir."
    )

    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        source = Path(tmp.name) / "dilekce.docx"
        make_docx(source, self.SENTENCE)
        self.install_document(source)

    def test_the_bare_surname_is_delivered_as_the_persons_placeholder(self):
        runs = self.exported_runs()
        delivered = "".join(runs)
        self.assertNotIn("Kartal", delivered, runs)
        self.assertEqual(delivered.count("[PERSON_1]"), 2, runs)
        self.assertIn("[PERSON_1] [COURT_1]", delivered, runs)


class SurnameMentionDeliveredFileTests(ExportHarness):
    """The ordinary word the surname prefixes survives the export and its QA."""

    def test_only_the_surname_is_delivered_as_the_persons_placeholder(self):
        for text, ordinary in SURNAME_PREFIX_DOCUMENTS:
            with self.subTest(ordinary=ordinary):
                tmp = TemporaryDirectory()
                self.addCleanup(tmp.cleanup)
                source = Path(tmp.name) / "dilekce.docx"
                make_docx(source, text)
                self.install_document(source)
                runs = self.exported_runs()
                delivered = "".join(runs)
                self.assertIn(ordinary, delivered, runs)
                self.assertEqual(delivered.count("[PERSON_1]"), 2, runs)
                qa = self.client.get("/api/document/1/redacted-export/qa?format=docx")
                self.assertEqual(qa.status_code, 200, qa.get_data(as_text=True))
                self.assertEqual(qa.get_json()["leakage_count"], 0, qa.get_json())

    def test_a_province_read_into_a_name_is_not_rewritten_across_the_export(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        source = Path(tmp.name) / "dilekce.docx"
        make_docx(
            source,
            PROVINCE_READ_INTO_A_NAME_DOCUMENTS[0][0]
            + " Tebligat adresi Çankaya/Ankara olup vekil Ankara Barosu üyesidir.",
        )
        self.install_document(source)
        delivered = "".join(self.exported_runs())
        self.assertIn("Çankaya/Ankara", delivered)
        self.assertIn("Ankara Barosu", delivered)

    def test_a_reviewer_added_one_word_name_still_removes_its_inflected_forms(self):
        """Whole-word matching is the detector's surname mention only.

        A reviewer who adds a missed surname wants it gone, "YILMAZIN" and a
        lower-case "yılmaz" included.
        """
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        source = Path(tmp.name) / "dilekce.docx"
        make_docx(
            source,
            "Davacı Ekin Kartal vekili aracılığıyla dava açmıştır. "
            "Tanık YILMAZIN beyanı alınmış, yılmaz imzalamıştır.",
        )
        self.install_document(source)
        response = self.client.post(
            "/api/document/1/findings", json={"text": "Yılmaz", "category": "natural_person_name"}
        )
        self.assertIn(response.status_code, (200, 201), response.get_data(as_text=True))
        runs = self.exported_runs()
        delivered = "".join(runs).casefold()
        self.assertNotIn("yılmaz", delivered, runs)
        self.assertNotIn("yilmaz", delivered, runs)
        qa = self.client.get("/api/document/1/redacted-export/qa?format=docx")
        self.assertEqual(qa.get_json()["leakage_count"], 0, qa.get_json())

    def test_the_measured_rendering_is_the_delivered_one(self):
        """semantic_preservation.render() scores the text the export ships."""
        text, ordinary = SURNAME_PREFIX_DOCUMENTS[0]
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        source = Path(tmp.name) / "dilekce.docx"
        make_docx(source, text)
        self.install_document(source)
        measured = render(self.source_text, self.profile["risk_map"]).text
        delivered = "".join(self.exported_runs())
        self.assertIn(ordinary, measured)
        self.assertEqual(measured.strip(), delivered.strip())


# The investigation-number probes (gold/README.md, "Investigation numbers and
# genitive authority names"). Each sentence is one of the reported probes or a
# form of it; the offices, numbers and notary are invented for this file.
INVESTIGATION_PROBES = [
    (
        "İstanbul Cumhuriyet Başsavcılığının 2024/12345 sayılı soruşturma dosyası incelendi.",
        "İstanbul Cumhuriyet Başsavcılığı",
        "2024/12345",
    ),
    (
        "Ankara Cumhuriyet Başsavcılığının 2025/4410 Soruşturma No'lu dosyası celbedildi.",
        "Ankara Cumhuriyet Başsavcılığı",
        "2025/4410",
    ),
    (
        "İzmir Cumhuriyet Başsavcılığı 2023/9071 soruşturma numaralı dosya kapsamında ifade verdi.",
        "İzmir Cumhuriyet Başsavcılığı",
        "2023/9071",
    ),
]


class InvestigationReferenceSpanTests(SpanCoverageAssertions):
    """An investigation number and the office that runs it, each a whole span.

    Measured before the change: in the first two probes neither the office nor
    the number was inside any finding, because every court rule ended its
    institution on `\\b` and the genitive "Başsavcılığının" never offers one,
    and the case-number rule only read a number AFTER its label ("Soruşturma
    No: ..."). In the third the office was a finding, but the generic rule's
    120-character sweep carried it to the end of the sentence, so approving it
    blanked the whole clause. Exact samples are asserted, which pins both
    halves: the finding is the whole name, and nothing past it.
    """

    def test_each_reported_form_gives_the_office_and_the_number_their_own_finding(self):
        for text, office, number in INVESTIGATION_PROBES:
            with self.subTest(text=text):
                self.assertEqual([f["sample"] for f in court_findings(text)], [office])
                self.assertEqual(
                    [f["sample"] for f in findings_of(text, "case_or_investigation_number")], [number]
                )

    def test_the_genitive_and_other_case_endings_stay_outside_an_authority_span(self):
        """One case per rule that ends on an institution word.

        The span ends at the stem, so "Mahkemesinin" and "Mahkemesi" carry one
        pseudonym. Before the change the numbered court backtracked to
        "Ankara 5. Asliye Hukuk", leaving the court word outside the finding,
        and the notary, the prosecutor's offices and the high court matched
        nothing at all.
        """
        cases = [
            ("Ankara 5. Asliye Hukuk Mahkemesinin kararı okundu.", "Ankara 5. Asliye Hukuk Mahkemesi"),
            ("Ankara 5. Asliye Hukuk Mahkemesince verilen karar okundu.", "Ankara 5. Asliye Hukuk Mahkemesi"),
            ("Kadıköy 3. Sulh Ceza Hakimliğinin kararı okundu.", "Kadıköy 3. Sulh Ceza Hakimliği"),
            ("Beyoğlu 24. Noterliğinin düzenlediği ihtarname okundu.", "Beyoğlu 24. Noterliği"),
            ("Kadıköy Cumhuriyet Savcılığının yazısı geldi.", "Kadıköy Cumhuriyet Savcılığı"),
            ("Ankara Cumhuriyet Başsavcılığına başvuruldu.", "Ankara Cumhuriyet Başsavcılığı"),
            ("Ankara Cumhuriyet Başsavcılığından yazı geldi.", "Ankara Cumhuriyet Başsavcılığı"),
            ("IZMIR CUMHURIYET BASSAVCILIGININ yazisi geldi.", "IZMIR CUMHURIYET BASSAVCILIGI"),
        ]
        for text, authority in cases:
            with self.subTest(text=text):
                self.assertEqual([f["sample"] for f in court_findings(text)], [authority])

    def test_a_genitive_authority_without_a_name_rule_is_still_covered_from_its_place(self):
        """The generic rule keeps its sweep; what changed is that it now fires."""
        text = "Kadıköy Kaymakamlığının yazısı geldi."
        self.assert_inside_a_finding(text, "Kadıköy Kaymakamlığı", court_findings(text))

    def test_an_investigation_bureau_is_part_of_its_offices_name(self):
        """The sweep used to cover the bureau; the name rule must not drop it."""
        text = (PROJECT_DIR / "tests" / "fixtures" / "criminal_investigation.txt").read_text(encoding="utf-8")
        first_line = text.splitlines()[0]
        self.assertEqual(
            [f["sample"] for f in court_findings(first_line)],
            ["İstanbul Cumhuriyet Başsavcılığı Dolandırıcılık Suçları Soruşturma Bürosu"],
        )
        text = "İstanbul Cumhuriyet Başsavcılığı Terör ve Örgütlü Suçlar Soruşturma Bürosu tarafından 2024/9 ile işlem yapıldı."
        self.assertEqual(
            [f["sample"] for f in court_findings(text)],
            ["İstanbul Cumhuriyet Başsavcılığı Terör ve Örgütlü Suçlar Soruşturma Bürosu"],
        )

    def test_every_form_of_one_investigation_number_is_one_pseudonym(self):
        """Keyed on the register, as "Soruşturma No: ..." already was.

        An Esas number with the same digits is a different file and keeps its
        own placeholder.
        """
        text = (
            "Soruşturma No: 2024/12345 dosyası açıldı. "
            "Başsavcılığın 2024/12345 sayılı soruşturma dosyası, 2024/12345 soruşturma numaralı dosya "
            "ve 2024/12345 Soruşturma No'lu dosya aynıdır. 2024/12345 Esas sayılı dosya ayrıdır."
        )
        placeholders = [f["placeholder"] for f in findings_of(text, "case_or_investigation_number")]
        self.assertEqual(len(placeholders), 5, placeholders)
        self.assertEqual(len(set(placeholders[:4])), 1, placeholders)
        self.assertNotEqual(placeholders[4], placeholders[0])

    def test_a_number_the_offices_old_sweep_covered_is_still_inside_a_finding(self):
        """Ending the office at its name must not leave the number after it bare.

        The office's 120-character sweep used to cover whatever number followed
        it. Measured over these offices and forms before
        privacy._prosecutor_file_numbers existed: 28 sentences whose number the
        old rules covered sat in no finding after the sweep was removed. Each
        must carry exactly one case_or_investigation_number finding, and the
        office's own finding must still end at its name.
        """
        offices = [
            "İstanbul Cumhuriyet Başsavcılığı",
            "İstanbul Cumhuriyet Başsavcılığının",
            "İstanbul Cumhuriyet Başsavcılığı'nın",
            "Ankara Cumhuriyet Başsavcılığına",
            "IZMIR CUMHURIYET BASSAVCILIGININ",
            "Kadıköy Cumhuriyet Savcılığının",
        ]
        tails = [
            "numaralı dosyası incelendi",
            "sayılı dosyası incelendi",
            "Hazırlık No'lu dosyası incelendi",
            "Sor. No'lu dosyası incelendi",
            "nolu dosyası incelendi",
            "dosyası incelendi",
        ]
        for office in offices:
            for middle in ("", "tarafından yürütülen "):
                for tail in tails:
                    text = f"{office} {middle}2024/12345 {tail}."
                    with self.subTest(text=text):
                        numbers = findings_of(text, "case_or_investigation_number")
                        self.assertEqual([f["sample"] for f in numbers], ["2024/12345"])
                        number_start = text.index("2024/12345")
                        self.assertTrue(all(f["end"] < number_start for f in court_findings(text)))

    def test_a_file_number_after_an_office_shares_the_soruşturma_pseudonym(self):
        text = (
            "Soruşturma No: 2024/12345 dosyası açıldı. "
            "İstanbul Cumhuriyet Başsavcılığı'nın 2024/12345 numaralı dosyası incelendi."
        )
        placeholders = [f["placeholder"] for f in findings_of(text, "case_or_investigation_number")]
        self.assertEqual(len(placeholders), 2, placeholders)
        self.assertEqual(len(set(placeholders)), 1, placeholders)

    def test_a_file_number_naming_another_register_keeps_its_own_pseudonym(self):
        text = (
            "Ankara Cumhuriyet Başsavcılığı'na 2024/12 sayılı iddianame ile dava açıldı. "
            "Soruşturma No: 2024/12"
        )
        placeholders = [f["placeholder"] for f in findings_of(text, "case_or_investigation_number")]
        self.assertEqual(len(placeholders), 2, placeholders)
        self.assertNotEqual(placeholders[0], placeholders[1])

    def test_an_inflected_register_word_is_the_same_register(self):
        texts = [
            "Ankara Cumhuriyet Başsavcılığı'nın 2024/12 sayılı iddianamesiyle dava açıldı. ",
            "Ankara Cumhuriyet Başsavcılığı'nın 2024/12 sayılı iddianame ile dava açıldı. ",
            "Ankara Cumhuriyet Başsavcılığı'nın 2024/12 Hazırlığı incelendi. ",
        ]
        for sentence in texts[:2]:
            found = findings_of(sentence + "Soruşturma No: 2024/12", "case_or_investigation_number")
            self.assertEqual(len(found), 2, found)
            self.assertNotEqual(found[0]["placeholder"], found[1]["placeholder"])
        text = "".join(texts) + "2024/12 sayılı soruşturmanın dosyası. Soruşturma No: 2024/12"
        placeholders = [f["placeholder"] for f in findings_of(text, "case_or_investigation_number")]
        self.assertEqual(len(placeholders), 5, placeholders)
        self.assertEqual(placeholders[0], placeholders[1])
        self.assertEqual(placeholders[3], placeholders[4])
        self.assertEqual(len({placeholders[0], placeholders[2], placeholders[4]}), 3, placeholders)

    def test_each_office_searches_its_own_window(self):
        """A number within the second office's window but past the first's."""
        text = (
            "İzmir Cumhuriyet Başsavcılığı ve Ankara Cumhuriyet Başsavcılığı arasında "
            "yetki uyuşmazlığı çıkmış olup taraflar uzun süre yazışmış ve konu henüz çözülmemiş iken "
            "2024/88 dosya incelendi."
        )
        number_start = text.index("2024/88")
        self.assertGreater(number_start - text.index("Başsavcılığı") - len("Başsavcılığı"), 120)
        self.assertLessEqual(number_start - text.index("Ankara Cumhuriyet Başsavcılığı") - len("Ankara Cumhuriyet Başsavcılığı"), 120)
        self.assertEqual([f["sample"] for f in findings_of(text, "case_or_investigation_number")], ["2024/88"])

    def test_a_petitions_salutation_is_not_an_authority(self):
        """Accepting the dative must not report "Sayın Hakimliğine" as a venue."""
        text = "Ankara 11. Aile Mahkemesi Sayın Hakimliğine"
        self.assertEqual([f["sample"] for f in court_findings(text)], ["Ankara 11. Aile Mahkemesi"])


class InvestigationReferenceDeliveredFileTests(ExportHarness):
    """The investigation-number fix proven on the delivered DOCX, not only on offsets."""

    def install_lines(self, lines: list[str]) -> None:
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        source = Path(tmp.name) / "sorusturma.docx"
        make_docx(source, "</w:t></w:r></w:p><w:p><w:r><w:t>".join(lines))
        self.install_document(source)

    def test_the_office_and_the_number_are_gone_and_the_clause_is_not(self):
        """Approving every finding redacts the office and the number, not the clause.

        The third line is the over-redaction the issue reported: the office's
        finding no longer reaches past its name, so the number has its own
        placeholder and "kapsamında ifade verdi" ships.

        Each line is a docket reference, so since the docket-reference change
        none carries a criminal_allegation finding; before it, the clauses had to be retained
        by hand here or approving everything would have redacted them.
        """
        self.install_lines([text for text, _office, _number in INVESTIGATION_PROBES])
        self.assertEqual(self.decide("retain", categories=["criminal_allegation"]), 0)
        runs = self.exported_runs()
        self.assertEqual(
            runs,
            [
                "[PROSECUTOR_OFFICE_1] [CASE_NUMBER_1] sayılı soruşturma dosyası incelendi.",
                "[PROSECUTOR_OFFICE_2] [CASE_NUMBER_2] Soruşturma No'lu dosyası celbedildi.",
                "[PROSECUTOR_OFFICE_3] [CASE_NUMBER_3] soruşturma numaralı dosya kapsamında ifade verdi.",
            ],
        )

    def test_the_gold_fixture_ships_none_of_its_offices_courts_or_numbers(self):
        text = (PROJECT_DIR / "tests" / "fixtures" / "investigation_reference.txt").read_text(encoding="utf-8")
        self.install_lines(text.splitlines())
        delivered = "".join(self.exported_runs())
        for needle in (
            "İstanbul", "Ankara", "İzmir", "Kadıköy", "Beyoğlu", "Masalkent",
            "Başsavcılığı", "Hakimliği", "Noterliği", "Mahkemesi",
            "2024/12345", "2025/4410", "2023/9071", "2024/55",
        ):
            with self.subTest(needle=needle):
                self.assertNotIn(needle, delivered)
        self.assertIn("[NOTARY_1]", delivered)
