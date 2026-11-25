"""Document-level name propagation: a bare surname of a person named in full.

A full name found by a title, a label or the gazetteer used to be the only
mention of that person the detector could see. "Quillfeather confirmed ..."
three paragraphs later carried no title and no label, so it was in no finding,
could not be approved, and shipped in the reviewed export beside the
pseudonymised full name -- which re-identifies the [PERSON_n] it stands next to.

The gold audit cannot pin this: it matches a label against a finding by
substring in either direction, so the full name's finding already "covers" a
bare-surname label. These tests read OFFSETS, every occurrence, and the
post-decision rendering the export is built from.

Every person, place and document here is invented.
"""

from __future__ import annotations

import json
import os
import time
import unittest
from pathlib import Path

from legal_analyzer import turkish_names
from legal_analyzer.privacy import REDACTED_REVIEW_STATUSES, analyze_privacy
from legal_analyzer.redaction_plan import plan_text

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "names"
GOLD = Path(__file__).resolve().parents[1] / "gold" / "gold_labels.example.json"


def person_findings(text: str) -> list[dict]:
    return [f for f in analyze_privacy("document.txt", text)["risk_map"] if f["category"] == "natural_person_name"]


def at(findings: list[dict], start: int) -> dict | None:
    return next((f for f in findings if f["start"] == start), None)


def fully_covered(spans: list[tuple[int, int]], start: int, end: int) -> bool:
    cursor = start
    for span_start, span_end in sorted(spans):
        if span_end <= cursor:
            continue
        if span_start > cursor:
            return False
        cursor = span_end
        if cursor >= end:
            return True
    return cursor >= end


def approved_rows(text: str) -> list[dict]:
    """Every finding approved, in the row shape the shared resolver reads."""
    return [
        {
            "id": index,
            "category": f["category"],
            "sample": f["sample"],
            "placeholder": f["placeholder"],
            "replacement_text": f["placeholder"],
            "review_status": "approved",
            "source": "detector",
            "start_offset": f["start"],
            "end_offset": f["end"],
        }
        for index, f in enumerate(analyze_privacy("document.txt", text)["risk_map"], start=1)
    ]


ENGLISH = (
    "Client: Marigold Quillfeather\n"
    "Quillfeather confirmed that the invoices were sent on time.\n"
    "The report records Quillfeather's objection to the second invoice.\n"
)

TURKISH = (
    "Davacı Mehmet Kartal ile davalı Ayşe Demir arasındaki kira uyuşmazlığında taraflar dinlendi.\n"
    "Kartal'ın vekili ödeme dekontlarını sundu; Demir'in itirazı tutanağa geçirildi.\n"
)


class BareSurnameIsTheSamePersonTests(unittest.TestCase):
    def test_an_english_bare_surname_takes_the_full_names_pseudonym(self):
        found = person_findings(ENGLISH)
        full = at(found, ENGLISH.index("Marigold Quillfeather"))
        self.assertIsNotNone(full)
        mentions = [f for f in found if f["sample"] == "Quillfeather"]
        self.assertEqual([f["start"] for f in mentions], [ENGLISH.index("Quillfeather confirmed"), ENGLISH.index("Quillfeather's")])
        for mention in mentions:
            self.assertEqual(mention["placeholder"], full["placeholder"])
            self.assertEqual(ENGLISH[mention["start"]:mention["end"]], "Quillfeather")

    def test_a_turkish_case_suffix_stays_outside_the_finding(self):
        found = person_findings(TURKISH)
        kartal = at(found, TURKISH.index("Kartal'ın"))
        demir = at(found, TURKISH.index("Demir'in"))
        self.assertEqual((kartal["sample"], kartal["end"] - kartal["start"]), ("Kartal", len("Kartal")))
        self.assertEqual(demir["sample"], "Demir")
        self.assertEqual(kartal["placeholder"], at(found, TURKISH.index("Mehmet Kartal"))["placeholder"])
        self.assertEqual(demir["placeholder"], at(found, TURKISH.index("Ayşe Demir"))["placeholder"])
        self.assertNotEqual(kartal["placeholder"], demir["placeholder"])

    def test_the_mention_cites_the_full_name_it_rests_on_by_offset_only(self):
        found = person_findings(ENGLISH)
        full = at(found, ENGLISH.index("Marigold Quillfeather"))
        self.assertNotIn("anchor", full)
        for mention in (f for f in found if f["sample"] == "Quillfeather"):
            self.assertEqual(mention["anchor"], {"start": full["start"], "end": full["end"]})

    def test_a_venue_give_back_is_anchored_too(self):
        """The court path's bare surname is the same kind of evidence."""
        text = "Davacı Ekin Kartal vekili aracılığıyla dava açmıştır.\nKartal 5. Asliye Hukuk Mahkemesine dilekçe sunmuştur.\n"
        mention = at(person_findings(text), text.index("Kartal 5."))
        self.assertEqual(mention["anchor"], {"start": text.index("Ekin Kartal"), "end": text.index("Ekin Kartal") + len("Ekin Kartal")})

    def test_a_full_name_repeated_in_prose_is_one_finding_not_a_surname(self):
        """The given name is unlisted, so only the labelled line found the name.
        Marking just the surname at the repeat would ship "Peregrine" beside
        the pseudonym."""
        text = "Client: Peregrine Thistlewood\nWe met Peregrine Thistlewood at the site and PEREGRINE THISTLEWOOD signed the plan.\n"
        found = person_findings(text)
        first = at(found, text.index("Peregrine Thistlewood"))
        for repeat in ("Peregrine Thistlewood at", "PEREGRINE THISTLEWOOD"):
            start = text.index(repeat)
            finding = at(found, start)
            self.assertIsNotNone(finding, repeat)
            self.assertEqual(finding["end"] - finding["start"], len("Peregrine Thistlewood"))
            self.assertEqual(finding["placeholder"], first["placeholder"])

    def test_a_shared_surname_goes_to_the_nearest_person_named_before_it(self):
        text = (
            "Davacı Mehmet Kartal dilekçesini sundu.\n"
            "Tanık Zeynep Kartal da duruşmaya katıldı. Kartal ifadesinde olayı anlattı.\n"
        )
        found = person_findings(text)
        mention = at(found, text.index("Kartal ifadesinde"))
        self.assertEqual(mention["placeholder"], at(found, text.index("Zeynep Kartal"))["placeholder"])


class WhatIsNotAMentionTests(unittest.TestCase):
    """The precision guards. Each shape here is an ordinary word, a place or an
    institution that happens to equal a party's surname."""

    def assertNoMentionAt(self, text: str, needle: str) -> None:
        start = text.index(needle)
        for finding in person_findings(text):
            self.assertFalse(finding["start"] <= start < finding["end"], (needle, finding))

    def test_a_district_written_with_its_province(self):
        self.assertNoMentionAt("Davacı Mehmet Kartal vekili.\nTaşınmaz Kartal/İSTANBUL adresindedir.\n", "Kartal/")

    def test_a_street_or_neighbourhood_word_after_it(self):
        self.assertNoMentionAt("Davacı Mehmet Kartal vekili.\nKartal Mahallesi muhtarlığına yazı yazıldı.\n", "Kartal Mahallesi")
        self.assertNoMentionAt("Client: Marigold Baker\nThe office is on Baker Street in the city.\n", "Baker Street")

    def test_a_company_with_a_legal_form(self):
        self.assertNoMentionAt("Client: Marigold Quillfeather\nQuillfeather Limited sent the invoice.\n", "Quillfeather Limited")
        self.assertNoMentionAt("Davacı Mehmet Kartal vekili.\nKartal A.Ş. temsilcisi dinlendi.\n", "Kartal A.Ş.")

    def test_an_ordinary_word_opening_a_sentence_the_document_also_writes_in_lowercase(self):
        text = "Davacı Ayşe Demir vekili beyanda bulundu.\nDemir kapı kilitliydi. Bahçede demir parmaklık vardı.\n"
        self.assertNoMentionAt(text, "Demir kapı")

    def test_mid_sentence_the_same_word_is_still_the_person(self):
        text = "Davacı Ayşe Demir vekili beyanda bulundu.\nBahçede demir parmaklık vardı. Tanık, Demir'in orada olduğunu söyledi.\n"
        self.assertIsNotNone(at(person_findings(text), text.index("Demir'in")))

    def test_the_surname_after_the_is_still_the_person(self):
        text = (
            "The meeting was attended by Robert Ravensworth yesterday. Later Ravensworth said the fence had moved. "
            "The Ravensworth report is attached."
        )
        found = person_findings(text)
        self.assertIsNotNone(at(found, text.index("Ravensworth said")))
        self.assertIsNotNone(at(found, text.index("Ravensworth report")))

    def test_a_defined_term_equal_to_a_partys_surname_is_proposed_as_the_party(self):
        """The documented residual: the reviewer rejects "the Price"; nothing guesses it away."""
        text = "The Seller, Robert Price, warrants that the Price shall be paid in full. The Price includes VAT."
        found = person_findings(text)
        full = at(found, text.index("Robert Price"))
        for needle in ("Price shall", "Price includes"):
            with self.subTest(needle=needle):
                mention = at(found, text.index(needle))
                self.assertIsNotNone(mention)
                self.assertEqual(mention["placeholder"], full["placeholder"])

    def test_a_sentence_opening_surname_the_document_never_writes_in_lowercase(self):
        self.assertIsNotNone(at(person_findings(ENGLISH), ENGLISH.index("Quillfeather confirmed")))

    def test_a_surname_the_document_uses_as_a_given_name_is_not_propagated(self):
        """"Mehmet Ali" beside "Ali Rıza" would otherwise make every "Ali" the same man."""
        self.assertNoMentionAt("Davacı Mehmet Ali dilekçesini sundu.\nAli Rıza Bey de dinlendi; Ali geldi.\n", "Ali geldi")

    def test_a_surname_that_is_only_a_given_name_elsewhere_still_propagates(self):
        """Polat is a given name on the hand-written list, but this document never uses it as one."""
        self.assertIn("polat", {key.replace("ı", "i") for key in turkish_names.GIVEN_NAMES})
        text = "Davacı Ayşe Polat vekili beyanda bulundu.\nTanık, Polat'ın orada olduğunu söyledi.\n"
        self.assertIsNotNone(at(person_findings(text), text.index("Polat'ın")))
        used_elsewhere = text + "Ayrıca Polat Yılmaz da dinlendi.\n"
        self.assertIsNone(at(person_findings(used_elsewhere), used_elsewhere.index("Polat'ın")))

    def test_a_lowercase_word_is_never_a_mention(self):
        text = "Davacı Ayşe Demir vekili beyanda bulundu.\nKapıda demir parmaklık vardı.\n"
        self.assertNoMentionAt(text, "demir parmaklık")

    def test_a_surname_the_title_path_found_takes_the_full_names_pseudonym(self):
        for text, full in (
            ("Claimant: Marigold Quillfeather\nMr Quillfeather attended. Later Quillfeather left.", "Marigold Quillfeather"),
            ("The Buyer, Robert Price, signed the deed. Later he met Dr. Price at the office.", "Robert Price"),
        ):
            with self.subTest(text=text):
                found = person_findings(text)
                self.assertEqual({f["placeholder"] for f in found}, {at(found, text.index(full))["placeholder"]})
                self.assertTrue(all("anchor" in f for f in found if f["sample"] != full))

    def test_an_honorific_or_suffix_is_not_the_surname(self):
        for text, mention, not_a_mention in (
            ("Tanık Ayşe Kaya Hanım dinlendi.\nMahkeme, Kaya'nın beyanını aldı. Sonra Hanım geldi.\n", "Kaya'nın", "Hanım geldi"),
            ("Claimant: Robert Thistlewood Jr.\nThistlewood confirmed the boundary. Jr confirmed nothing.\n", "Thistlewood confirmed", "Jr confirmed"),
        ):
            with self.subTest(text=text):
                self.assertIsNotNone(at(person_findings(text), text.index(mention)))
                self.assertNoMentionAt(text, not_a_mention)
        text = "Davacı Mehmet Kartal geldi. Tanık Ali Bey dinlendi. Kartal Bey de geldi.\n"
        self.assertNoMentionAt(text, "Bey de")

    def test_a_period_after_a_title_or_abbreviation_does_not_end_the_sentence(self):
        for text, mention in (
            ("Davacı Ayşe Demir vekili.\nDavacı vekili Av. Demir duruşmaya geldi. Bahçede demir parmaklık vardı.\n", "Demir duruşmaya"),
            ("Client: Robert Price. He told Price, i.e. Price agreed. The price was fair.\n", "Price agreed"),
        ):
            with self.subTest(text=text):
                self.assertIsNotNone(at(person_findings(text), text.index(mention)))

    def test_a_contract_party_role_is_not_the_party_of_the_same_surname(self):
        """"Hizmet Alan: Murat Alan" -- the role keeps its words, as the
        contract party-role guard in turkish_names requires."""
        text = "Hizmet Alan: Murat Alan\nİMZALAR\nAyşe Veren\nKemal Alıcı\nKiraya Veren ve Alıcı imzaladı.\n"
        for needle in ("Alan:", "Veren ve", "Alıcı imzaladı"):
            with self.subTest(needle=needle):
                self.assertNoMentionAt(text, needle)

    def test_a_full_name_repeat_is_kept_when_its_surname_is_a_role_word(self):
        """The role guard reads bare words only: a prose repeat of "Tunç Alıcı"
        is the person, not the role "Alıcı". "Tunç" is on no given-name list, so
        only propagation can find the repeat."""
        text = "Alıcı: Tunç Alıcı\nToplantıya Tunç Alıcı katıldı ve Alıcı adına imzaladı.\n"
        repeat = text.index("Tunç Alıcı katıldı")
        finding = at(person_findings(text), repeat)
        self.assertIsNotNone(finding)
        self.assertEqual(finding["end"] - finding["start"], len("Tunç Alıcı"))
        self.assertNoMentionAt(text, "Alıcı adına")

    def test_a_province_surname_is_not_propagated(self):
        """"Selim Ankara" must not make every "Ankara" in the document the party."""
        text = "Davacı Selim Ankara dilekçesini sundu.\nToplantı Ankara'da yapıldı.\n"
        self.assertNoMentionAt(text, "Ankara'da")


class GoldDocumentsTests(unittest.TestCase):
    """The two name-propagation gold documents, read by offset."""

    @classmethod
    def setUpClass(cls):
        gold = json.loads(GOLD.read_text(encoding="utf-8"))
        cls.documents = [
            (doc, (GOLD.parent / doc["path"]).resolve())
            for doc in gold["documents"]
            if "/names/" in doc["path"]
        ]

    def test_the_gold_set_carries_every_names_fixture(self):
        self.assertEqual(sorted(path for _doc, path in self.documents), sorted(FIXTURES.glob("*.txt")))

    def test_every_occurrence_of_every_person_label_is_covered_end_to_end(self):
        for doc, path in self.documents:
            text = path.read_text(encoding="utf-8")
            spans = [(f["start"], f["end"]) for f in person_findings(text)]
            for label in doc["labels"]:
                if label["category"] != "natural_person_name":
                    continue
                start = text.find(label["text"])
                self.assertNotEqual(start, -1, label)
                while start != -1:
                    with self.subTest(document=doc["id"], label=label["text"], at=start):
                        self.assertTrue(fully_covered(spans, start, start + len(label["text"])))
                    start = text.find(label["text"], start + 1)

    def test_approving_everything_leaves_no_labelled_name_in_the_deliverable(self):
        """plan_text is the rendering the reviewed export is built from."""
        for doc, path in self.documents:
            text = path.read_text(encoding="utf-8")
            rendered = plan_text(text, approved_rows(text), REDACTED_REVIEW_STATUSES)
            for label in doc["labels"]:
                with self.subTest(document=doc["id"], label=label["text"]):
                    self.assertNotIn(label["text"], rendered)

    def test_each_bare_surname_mention_is_its_own_finding(self):
        """The audit matches a bare-surname label against the full name's finding
        by substring, so it cannot see these; read the offsets instead."""
        cases = {
            "tr_surname_mentions.txt": ["Kartal'ın", "Kartal, bir sonraki"],
            "en_listed_given_names.txt": ["Ravensworth report"],
            "en_surname_mentions.txt": ["Stanbridge confirmed"],
        }
        for name, needles in cases.items():
            text = (FIXTURES / name).read_text(encoding="utf-8")
            found = person_findings(text)
            for needle in needles:
                with self.subTest(fixture=name, needle=needle):
                    finding = at(found, text.index(needle))
                    self.assertIsNotNone(finding)
                    self.assertEqual(text[finding["start"]:finding["end"]], needle.split("'")[0].split()[0].rstrip(","))

    def test_one_person_one_pseudonym(self):
        for doc, path in self.documents:
            text = path.read_text(encoding="utf-8")
            found = person_findings(text)
            for label in doc["labels"]:
                if label["category"] != "natural_person_name" or " " in label["text"]:
                    continue
                surname = label["text"]
                placeholders = {
                    f["placeholder"] for f in found if f["sample"].split()[-1].casefold() == surname.casefold()
                }
                with self.subTest(document=doc["id"], surname=surname):
                    self.assertEqual(len(placeholders), 1, placeholders)

    def test_no_finding_spans_a_line_break(self):
        for _doc, path in self.documents:
            text = path.read_text(encoding="utf-8")
            for finding in analyze_privacy(path.name, text)["risk_map"]:
                with self.subTest(fixture=path.name, sample=finding["sample"]):
                    self.assertNotIn("\n", text[finding["start"]:finding["end"]])


SNAPSHOT = Path(__file__).resolve().parent / "fixtures" / "names_propagation_snapshot.json"
SNAPSHOT_INLINE_CASES = {
    "sentence_opening_numbered": "Davacı Ayşe Demir vekili beyanda bulundu.\n1. Demir kapı kilitliydi. (a) Demir parmaklık vardı; demir pas tuttu.\n\"Demir geldi\" dedi tanık.\n",
    "equidistant_bearers": "Client: Marigold Quillfeather and Mr Robert Quillfeather. Quillfeather replied. Mr Robert Quillfeather and Marigold Quillfeather left.\n",
    "full_name_repeat_far_on_one_line": "Mr Robert Quillfeather signed. " + "The parties agreed the terms of settlement. " * 12 + "Robert Quillfeather confirmed, and later Quillfeather paid.\n",
    "lowercase_full_name_repeat": "Davacı Ekin Kartal vekili dava açmıştır.\nKartal, duruşmaya katıldı. ekin kartal imzası mevcuttur. Tanık, Kartal'ın orada olduğunu söyledi.\n",
    "email_local_part": "Witness statement of Jonathan Stanbridge, email j.stanbridge@example.com.\nStanbridge confirmed the boundary had moved. stanbridge was not otherwise written.\n",
    "long_dash_run": "Davacı Ayşe Demir vekili.\n" + "-" * 50 + " Demir geldi. demir kapı.\n",
}


def snapshot_inputs() -> dict[str, tuple[str, str]]:
    inputs = {path.name: (path.name, path.read_text(encoding="utf-8")) for path in sorted(FIXTURES.glob("*.txt"))}
    inputs.update({"inline:" + key: ("document.txt", text) for key, text in SNAPSHOT_INLINE_CASES.items()})
    return inputs


def anchored_rows(name: str, text: str) -> list[list]:
    """Every finding propagation made: offsets, placeholder and the full name it rests on."""
    return [
        [f["start"], f["end"], f["placeholder"], f["anchor"]["start"], f["anchor"]["end"]]
        for f in analyze_privacy(name, text)["risk_map"]
        if f["category"] == "natural_person_name" and "anchor" in f
    ]


class PropagationCostTests(unittest.TestCase):
    """Propagation is near-linear in the document, without moving what it finds.

    names_propagation_snapshot.json is the recorded propagation output -- each
    anchored natural_person_name finding's offsets, placeholder and anchor --
    over the names fixtures and the inline cases above. It pins only what
    name propagation decides, so a performance change to it must leave the
    file as it is. A deliberate change to what propagation finds is recorded
    by regenerating it:

        REGENERATE_PROPAGATION_SNAPSHOT=1 python3 -m unittest tests.test_name_propagation
    """

    def test_the_propagated_findings_match_the_recorded_snapshot(self):
        actual = {key: anchored_rows(name, text) for key, (name, text) in snapshot_inputs().items()}
        if os.environ.get("REGENERATE_PROPAGATION_SNAPSHOT"):
            SNAPSHOT.write_text(json.dumps(actual, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        expected = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
        self.assertEqual(sorted(actual), sorted(expected))
        for key, rows in actual.items():
            with self.subTest(input=key):
                self.assertEqual(rows, expected[key])

    def test_a_single_line_document_at_the_extraction_cap(self):
        unit = (
            "Mr Robert Quillfeather attended. Later that day the claimant met Quillfeather again, "
            "and quillfeather was written in lowercase by the clerk. "
        )
        text = (unit * (400_000 // len(unit) + 1))[:400_000]
        started = time.perf_counter()
        found = person_findings(text)
        elapsed = time.perf_counter() - started
        self.assertIsNotNone(at(found, text.index("Quillfeather again")))
        self.assertLess(elapsed, 30.0)


if __name__ == "__main__":
    unittest.main()
