"""English detection: names, addresses, phones, money, dates, courts and claim numbers.

Every person, address, number and organisation here is invented. Phones come
from the reserved ranges in eval/README.md ("Reserved contact ranges"): Ofcom's
drama numbers and the NANP 555-0100..0199 block.

Three invariants beside the per-category behaviour, because each is the way an
English rule could do harm while every category test stays green:

* the English rules add nothing to a Turkish document (TurkishIsUntouchedTests);
* a finding covers the WHOLE identifier, not the token that anchored it
  (EnglishFixturesTests -- the gold audit matches by substring and cannot see a
  partial);
* finding English identifiers does not open the release gate on a document the
  rules cannot read (TheLanguageGateIsNotLoosenedTests).
"""

from __future__ import annotations

import json
import unittest
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock
from xml.sax.saxutils import escape
from zipfile import ZIP_DEFLATED, ZipFile

import pymupdf

from legal_analyzer import english_names, legal_semantics, privacy
from legal_analyzer.english_names import detect_english_person_names
from legal_analyzer.docx_redactor import redact_docx
from legal_analyzer.export_scan import scan_docx, scan_pdf
from legal_analyzer.privacy import analyze_privacy, build_redacted_preview, refresh_release_state
from legal_analyzer.redaction_plan import plan_text, redaction_inputs

FIXTURES = Path(__file__).resolve().parent / "fixtures"
ENGLISH_FIXTURES = sorted((FIXTURES / "english").glob("*.txt"))
GOLD = Path(__file__).resolve().parents[1] / "gold" / "gold_labels.example.json"

ENGLISH_RULES = (
    privacy.ENGLISH_PHONE_RULE,
    privacy.ENGLISH_DATE_RULE,
    privacy.ENGLISH_COURT_RULE,
    privacy.ENGLISH_CASE_NUMBER_RULE,
    privacy.ENGLISH_ADDRESS_RULE,
    privacy.ENGLISH_ACCOUNT_RULE,
)


def findings(text: str) -> list[dict]:
    return analyze_privacy("document.txt", text)["risk_map"]


def samples(text: str, category: str) -> list[str]:
    return [f["sample"] for f in findings(text) if f["category"] == category]


def names(text: str) -> list[str]:
    return [value for value, _start, _end in detect_english_person_names(text)]


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


class EnglishPersonNameTests(unittest.TestCase):
    def test_a_title_takes_a_bare_surname(self):
        """"Mr Smith" is how an English document names a party; the Turkish
        title path needs two words and never saw it."""
        self.assertEqual(samples("We wrote to Mrs Ashgrove on Monday.", "natural_person_name"), ["Ashgrove"])
        self.assertEqual(names("Before: His Honour Judge Okereke"), ["Okereke"])
        self.assertEqual(names("Counsel for the claimant, Mr J Tregarthen, appeared."), ["J Tregarthen"])

    def test_a_known_given_name_reads_forward_to_the_surname(self):
        self.assertEqual(names("The meeting was chaired by Oliver Bennett and minuted."), ["Oliver Bennett"])
        self.assertEqual(names("KEITH MORRIS attended."), ["KEITH MORRIS"])

    def test_structure_finds_a_name_whose_given_name_is_unlisted(self):
        # The precondition is the point: none of these given names is in the
        # gazetteer, so only the document's structure can find them. (Not
        # "Cornelius": the Social Security list in given_names_en carries it.)
        for given in ("marigold", "crispin", "perpetua", "barnaby"):
            self.assertNotIn(given, english_names.GIVEN_NAMES)
        cases = {
            "Name: Marigold Quillfeather\n": "Marigold Quillfeather",
            "Second witness: Barnaby Truscott\n": "Barnaby Truscott",
            "I, Crispin Fairbrother, of Bristol, say as follows.": "Crispin Fairbrother",
            "WITNESS STATEMENT OF PERPETUA ASHGROVE\n": "PERPETUA ASHGROVE",
            "PERPETUA ASHGROVE                         Claimant\n": "PERPETUA ASHGROVE",
            "(1) BARNABY TRUSCOTT of 12 Larkspur Road\n": "BARNABY TRUSCOTT",
            "This deed is made between Marigold Quillfeather and Barnaby Truscott.": "Barnaby Truscott",
            "Yours faithfully\n\nCrispin Fairbrother\n": "Crispin Fairbrother",
            "Present: Marigold Quillfeather, Barnaby Truscott\n": "Marigold Quillfeather",
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertIn(expected, names(text))

    def test_capitals_and_title_case_are_one_person(self):
        """A claim heading's capitals and the signature's title case are one
        witness. The Turkish fold maps an ASCII capital I to a dotless one, which
        keyed them to two pseudonyms."""
        text = (
            "WITNESS STATEMENT OF CORNELIUS FAIRBROTHER\n\n"
            "I, Cornelius Fairbrother, of Bristol, say as follows.\n"
        )
        placeholders = {
            f["sample"]: f["placeholder"] for f in findings(text) if f["category"] == "natural_person_name"
        }
        self.assertEqual(placeholders["CORNELIUS FAIRBROTHER"], placeholders["Cornelius Fairbrother"])

    def test_a_possessive_is_not_part_of_the_name(self):
        text = "Mr Hartington's letter was late. Mr Hartington then called."
        person = [f for f in findings(text) if f["category"] == "natural_person_name"]
        self.assertEqual([f["sample"] for f in person], ["Hartington", "Hartington"])
        self.assertEqual(len({f["placeholder"] for f in person}), 1)

    def test_a_name_reaching_past_another_keeps_its_extra_words(self):
        """Two paths can read one run differently: the list window stops at
        four words, the gazetteer starts again at "Lewis". The second reaches
        past the first, and dropping it would leave "Jones" in no finding."""
        text = "Present: Oliver Ward Lewis Hale Jones\n"
        spans = [(start, end) for _value, start, end in detect_english_person_names(text)]
        self.assertTrue(fully_covered(spans, text.index("Oliver"), text.index("Jones") + len("Jones")))

    def test_headings_companies_streets_and_products_are_not_people(self):
        for text in (
            "The file was saved as an MS Word document.",
            "The office is on George Street.",
            "Under the Master Services Agreement the Supplier shall perform.",
            "Grant Thornton LLP audited the accounts.",
            "HARTWELL LOGISTICS LIMITED                 Defendant\n",
            "The Lord Chancellor issued guidance.",
            "The Grant of Probate was issued.",
            "Shipments to DR Congo were delayed.",
            "The firm licenses MS Dynamics for billing.",
            "Employer: Demir Tekstil Sanayi ve Ticaret Anonim Sirketi\n",
        ):
            with self.subTest(text=text):
                self.assertEqual(names(text), [])

    def test_ordinary_turkish_words_do_not_start_an_english_name(self):
        """Ben, Ana and Ada are English given names and ordinary Turkish words:
        "I", "main" (Ana Sözleşme, a company's articles), "island"."""
        for text in (
            "Ben Ahmet Kaya olarak beyan ederim.",
            "Ana Sözleşme Madde 4 uyarınca yönetim kurulu toplanır.",
            "Ada Lojistik Limited Şirketi ile yapılan sözleşme",
        ):
            with self.subTest(text=text):
                self.assertEqual(names(text), [])


class EnglishPartyRoleTermTests(unittest.TestCase):
    """A defined party role on an execution page or in an attendance list is
    not a person. The gold regression case is gold_en_scc_execution.txt."""

    def test_role_lines_in_a_signature_or_list_window_are_not_people(self):
        for text in (
            "Signed for and on behalf of the parties:\n\nData Exporter\nData Importer",
            "Present:\nData Controller\nData Processor",
            "Distribution:\nSub Processor\nJoint Controller\nService Provider",
        ):
            with self.subTest(text=text):
                self.assertEqual(names(text), [])

    def test_a_person_beside_the_roles_is_still_found(self):
        text = "Present:\nData Controller\nImogen Thackeray (Chair)\nData Processor"
        self.assertEqual(names(text), ["Imogen Thackeray"])

    def test_the_guard_is_on_the_whole_candidate(self):
        """A name that merely contains a role word is a name."""
        self.assertEqual(names("Signed\n\nRowan Importer\nImogen Licensee"), ["Rowan Importer", "Imogen Licensee"])


class EnglishNameBesideItsOwnEmailTests(unittest.TestCase):
    """A capitalised run written right before an e-mail address whose local
    part spells it ("Jane Doe <jane.doe@…>", "J. Doe, j.doe@…") is that
    person, whatever the gazetteer says. The gold regression case is
    tests/fixtures/names/en_signature_and_email_names.txt."""

    def test_the_given_names_are_unlisted(self):
        # The precondition is the point: only the address can find these.
        for given in ("ottoline", "lysander", "hepzibah", "cressida", "euphemia", "septimus"):
            self.assertNotIn(given, english_names.GIVEN_NAMES)

    def test_a_name_followed_by_the_address_it_spells(self):
        cases = {
            "  1. Ottoline Quarrendon, ottoline.quarrendon@example.org\n": "Ottoline Quarrendon",
            "  - Lysander Pellingham (l.pellingham@example.org)\n": "Lysander Pellingham",
            "From: Hepzibah Wrexbridge <hepzibah_wrexbridge@example.net>\n": "Hepzibah Wrexbridge",
            "Contact Ottoline Quarrendon - oquarrendon@example.com - for access.": "Ottoline Quarrendon",
            "OTTOLINE QUARRENDON (ottoline.quarrendon@example.org)\n": "OTTOLINE QUARRENDON",
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(names(text), [expected])
                start = text.index(expected)
                self.assertIn((expected, start, start + len(expected)), detect_english_person_names(text))

    def test_an_address_that_does_not_spell_the_run_proves_nothing(self):
        for text in (
            "  1. Ottoline Quarrendon, records@example.org\n",
            "  2. Lysander Pellingham (s.pellingham@example.org)\n",
            "Write to Ottoline Quarrendon at the address below.\nquarrendon@example.org\n",
        ):
            with self.subTest(text=text):
                self.assertEqual(names(text), [])

    def test_a_team_mailbox_whose_words_the_document_uses_in_lowercase_is_not_a_person(self):
        """"Records Management (records.management@…)" spells its address the
        way a person does; the document's own lowercase "records management"
        is what says these are ordinary words."""
        for text in (
            "Questions on records management go to Records Management (records.management@example.com).",
            "For data protection queries write to Data Protection <data.protection@example.com>.",
        ):
            with self.subTest(text=text):
                self.assertEqual(names(text), [])

    def test_a_person_whose_surname_is_also_an_ordinary_word_is_still_found(self):
        """Only a run whose every capitalised word the document writes in
        lowercase is a team mailbox; one ordinary word does not make it one."""
        text = (
            "The purchase price is fixed.\n"
            "Questions go to Ottoline Price (ottoline.price@example.org).\n"
        )
        start = text.index("Ottoline Price")
        self.assertIn(("Ottoline Price", start, start + len("Ottoline Price")), detect_english_person_names(text))

    def test_guards_still_split_the_run(self):
        for text in (
            "Contact Quillon Analytics Limited (quillon.analytics@example.com).",
            "Legal Department <legal.department@example.com>",
        ):
            with self.subTest(text=text):
                self.assertEqual(names(text), [])


class EnglishSignatureLineTests(unittest.TestCase):
    """"Signed: Jane Doe" and "Signed by Jane Doe and John Roe" name the
    signatories on the signature line itself; the signature window only reads
    the lines AFTER it, and the label path wanted "Signed by:" with a colon."""

    def test_signed_with_a_colon(self):
        self.assertEqual(names("Signed: Cressida Dunstanmere\n"), ["Cressida Dunstanmere"])
        self.assertEqual(names("Signature: Cressida Dunstanmere\n"), ["Cressida Dunstanmere"])

    def test_signed_by_without_a_colon_takes_both_signatories(self):
        text = "Signed by Euphemia Ashworth-Lyle and Septimus Ravenhurst\n"
        self.assertEqual(names(text), ["Euphemia Ashworth-Lyle", "Septimus Ravenhurst"])
        self.assertEqual(
            names("The deed was signed by Euphemia Ashworth-Lyle and witnessed by Septimus Ravenhurst."),
            ["Euphemia Ashworth-Lyle", "Septimus Ravenhurst"],
        )

    def test_a_signatory_that_is_a_company_or_a_role_is_not_a_person(self):
        for text in (
            "Signed by the Employee\n",
            "Signed by Quillon Analytics Limited\n",
            "Signed by Data Exporter and Data Importer\n",
            "Signed: Director\n",
            # "executed by" is not a cue: works and services are executed by firms.
            "The works executed by Harrowby Builders were inspected.\n",
        ):
            with self.subTest(text=text):
                self.assertEqual(names(text), [])


class SignatureAndEmailFixtureTests(unittest.TestCase):
    """Every occurrence of every person in the gold fixture is covered end to
    end by a natural_person_name finding -- offsets, not the audit's substring
    match -- and the team mailboxes beside them are not people."""

    FIXTURE = FIXTURES / "names" / "en_signature_and_email_names.txt"
    PEOPLE = (
        "Ottoline Quarrendon",
        "Lysander Pellingham",
        "Hepzibah Wrexbridge",
        "Cressida Dunstanmere",
        "Euphemia Ashworth-Lyle",
        "Septimus Ravenhurst",
    )

    def test_every_occurrence_is_covered(self):
        text = self.FIXTURE.read_text(encoding="utf-8")
        person = [f for f in findings(text) if f["category"] == "natural_person_name"]
        spans = [(f["start"], f["end"]) for f in person]
        for name in self.PEOPLE:
            start = text.find(name)
            self.assertNotEqual(start, -1, name)
            while start != -1:
                with self.subTest(name=name, start=start):
                    self.assertTrue(fully_covered(spans, start, start + len(name)))
                start = text.find(name, start + 1)
        for sample in (f["sample"] for f in person):
            self.assertNotIn("Management", sample)
            self.assertNotIn("Protection", sample)


class EnglishPhoneTests(unittest.TestCase):
    def test_uk_and_north_american_numbers(self):
        for number in (
            "020 7946 0123",
            "+44 20 7946 0123",
            "+44 (0)20 7946 0123",
            "(020) 7946 0123",
            "07700 900123",
            "07700 900 123",
            "+44 7700 900123",
            "(212) 555-0147",
            "212-555-0147",
            "+1 212 555 0147",
        ):
            with self.subTest(number=number):
                self.assertEqual(samples(f"Call {number} today.", "phone_number"), [number])

    def test_a_turkish_number_is_still_one_finding(self):
        for number in ("0522 111 22 33", "05221112233", "+90 522 111 22 33"):
            with self.subTest(number=number):
                self.assertEqual(len(samples(f"Tel: {number}", "phone_number")), 1)

    def test_a_turkish_number_grouped_3_3_4_is_one_finding(self):
        """The North American shape reads "522 456 7890" out of the Turkish
        "+90 522 456 7890", which put one number on two review cards."""
        for number in ("+90 522 456 7890", "0522 456 7890"):
            with self.subTest(number=number):
                self.assertEqual(samples(f"Tel: {number}", "phone_number"), [number])

    def test_the_uk_and_us_forms_share_a_pseudonym_with_their_own_variants(self):
        text = "Call +44 20 7946 0123 or, from the UK, 020 7946 0123."
        placeholders = {f["placeholder"] for f in findings(text) if f["category"] == "phone_number"}
        self.assertEqual(len(placeholders), 1)


class EnglishDateTests(unittest.TestCase):
    def test_day_month_year_in_english(self):
        for date in (
            "12 March 2026",
            "2nd April 2026",
            "12th March, 2026",
            "3rd day of April 2026",
            "3rd of April 2026",
            "March 12, 2026",
            "Sept. 9 2026",
            "31 DECEMBER 2025",
        ):
            with self.subTest(date=date):
                self.assertEqual(samples(f"Signed on {date} in London.", "date"), [date])

    def test_a_month_without_a_day_or_a_year_is_not_a_date(self):
        for text in ("Payment is due in March 2026.", "The tenant may 2026 renew?", "15 Mart 2026 tarihinde"):
            with self.subTest(text=text):
                self.assertEqual(
                    [f for f in findings(text) if f["category"] == "date" and not f["sample"][0].isdigit()], []
                )
                self.assertNotIn("Mart", " ".join(samples(text, "date")))


class MoneyTests(unittest.TestCase):
    def test_a_currency_symbol_after_a_space_is_an_amount(self):
        """The symbols sat behind a word boundary, which a space before "$"
        does not provide, so " $1,000" and " €500" were never detected."""
        self.assertEqual(samples("The fee is $1,000 per month.", "money_amount"), ["$1,000"])
        self.assertEqual(samples("Penalty (€500) applies.", "money_amount"), ["€500"])

    def test_sterling_and_written_currencies(self):
        for amount in ("£12,500.00", "GBP 2,000", "12,500 pounds sterling", "£2.5m", "$1.2bn", "US$400", "1,000 euros"):
            with self.subTest(amount=amount):
                self.assertEqual(samples(f"The sum of {amount} is due.", "money_amount"), [amount])

    def test_a_sentence_full_stop_is_not_part_of_the_amount(self):
        self.assertEqual(samples("The claim is for £6,000.", "money_amount"), ["£6,000"])
        self.assertEqual(samples("Bedel 50.000,00 TL olarak ödenir.", "money_amount"), ["50.000,00 TL"])

    def test_a_non_breaking_space_between_amount_and_currency_is_kept(self):
        """Word keeps an amount on one line with its currency with a
        non-breaking space, and the amount must still be found."""
        for text, amount in (
            ("Bedel 50.000\xa0TL olarak ödenir.", "50.000\xa0TL"),
            ("The fee is USD\xa010,000 net.", "USD\xa010,000"),
            ("fee €\u202f500 due", "€\u202f500"),
            ("a sum of 12,500\xa0pounds\xa0sterling", "12,500\xa0pounds\xa0sterling"),
        ):
            with self.subTest(text=text):
                self.assertEqual(samples(text, "money_amount"), [amount])

    def test_a_currency_across_a_line_break_leaves_the_amount_alone(self):
        """A sample spanning a newline matches nothing in a DOCX part, so it
        could never be redacted; the amount on its own line still can."""
        for text, amount in (
            ("a sum of 12,500\npounds sterling is due", "12,500"),
            ("bedel 50.000\nTL olarak", "50.000"),
            ("the price is USD\n10.000 net", "10.000"),
            ("a fee of £\n500 is due", "500"),
            ("ücret TL\n7.500 olarak", "7.500"),
        ):
            with self.subTest(text=text):
                amounts = [f for f in findings(text) if f["category"] == "money_amount"]
                self.assertEqual(len(amounts), 1)
                span = text[amounts[0]["start"]:amounts[0]["end"]]
                self.assertNotIn("\n", span)
                self.assertEqual(span, amount)
                self.assertEqual(amounts[0]["sample"], amount)


class EnglishCourtTests(unittest.TestCase):
    def test_english_and_american_venues(self):
        for court in (
            "HIGH COURT OF JUSTICE",
            "KING'S BENCH DIVISION",
            "County Court at Central London",
            "Central London County Court",
            "Court of Appeal (Civil Division)",
            "Employment Appeal Tribunal",
            "First-tier Tribunal (Property Chamber)",
            "Technology and Construction Court",
            "Westminster Magistrates' Court",
            "United States District Court for the Southern District of New York",
        ):
            with self.subTest(court=court):
                self.assertEqual(samples(f"The matter is before the {court}.\n", "court_or_authority"), [court])

    def test_the_defined_term_and_the_jurisdiction_clause_are_not_a_venue(self):
        for text in (
            "The Court shall have regard to the evidence.",
            "The courts of England and Wales have exclusive jurisdiction.",
            "The Courts of England and Wales have exclusive jurisdiction.",
            "Any Court of competent jurisdiction may enforce a Court Order.",
            "IN THE HIGH COURT OF JUSTICE",
        ):
            with self.subTest(text=text):
                for sample in samples(text, "court_or_authority"):
                    self.assertNotIn(sample.split()[0].lower(), {"the", "in", "any", "a"})
                    self.assertNotIn("Courts of England", sample)
        self.assertEqual(samples("The Court shall have regard to the evidence.", "court_or_authority"), [])

    def test_an_english_venue_is_typed_as_a_court(self):
        found = [f for f in findings("Heard at Manchester Employment Tribunal.") if f["category"] == "court_or_authority"]
        self.assertEqual([f["placeholder"] for f in found], ["[COURT_1]"])
        self.assertEqual(legal_semantics.institution_type("Commercial Court"), legal_semantics.COMMERCIAL_COURT)

    def test_a_court_heading_joined_by_commas_is_one_venue(self):
        """The High Court names itself parent first: court, then its group or
        division, then the list. Two findings with ", " between them leave the
        comma uncovered, and a reviewer approving them gets two placeholders
        for one venue."""
        for court in (
            "High Court of Justice, Business and Property Courts of England and Wales",
            "HIGH COURT OF JUSTICE, CHANCERY DIVISION",
            "High Court of Justice, King's Bench Division, Bristol District Registry",
            "High Court of Justice, Business and Property Courts in Leeds, Insolvency and Companies List (ChD)",
        ):
            with self.subTest(court=court):
                self.assertEqual(samples(f"The claim is proceeding in the {court}.\n", "court_or_authority"), [court])

    def test_a_tribunal_sitting_at_a_place_carries_the_place(self):
        """"Sitting at" is how a tribunal or the County Court names its hearing
        centre; left outside the span, the town ships beside [COURT_1]."""
        for court in ("Employment Tribunal sitting at Watford", "County Court sitting at Central London"):
            with self.subTest(court=court):
                self.assertEqual(samples(f"Heard by the {court} on the papers.", "court_or_authority"), [court])

    def test_separate_courts_in_a_list_are_not_joined(self):
        """Only a parent court reaches over a comma; a list of venues stays a
        list, each with its own pseudonym."""
        text = "Permission was refused by the County Court, Court of Appeal and the Supreme Court in turn."
        self.assertEqual(
            samples(text, "court_or_authority"), ["County Court", "Court of Appeal", "Supreme Court"]
        )

    def test_a_building_named_court_in_an_address_is_not_a_venue(self):
        """"7 Linnet Court" in front of a town and a postcode is a block of
        flats, and the whole line is the address."""
        text = "Write to Level 2, 7 Linnet Court, Stockport SK4 2BW, United Kingdom.\n"
        self.assertEqual(samples(text, "court_or_authority"), [])
        self.assertEqual(samples(text, "address"), ["Level 2, 7 Linnet Court, Stockport SK4 2BW, United Kingdom"])


class EnglishCaseNumberTests(unittest.TestCase):
    def test_labelled_claim_and_case_numbers(self):
        for label, number in (
            ("Claim No.", "KB-2026-000418"),
            ("Claim Number:", "J01AB123"),
            ("Case No:", "6014827/2026"),
            ("Civil Action No.", "1:26-cv-04172"),
            ("Index No.", "654321/2026"),
            ("Our Ref:", "TV/LBC/2026/0331"),
            ("Appeal No.", "CA-2026-000123"),
        ):
            with self.subTest(label=label):
                self.assertEqual(
                    samples(f"{label} {number}.", "case_or_investigation_number"), [f"{label} {number}"]
                )

    def test_the_span_carries_its_label_like_the_turkish_rule(self):
        """"Esas No: 2025/318" keeps its label; so does "Claim No. KB-…". The
        label names the register, and the reference is the pair."""
        text = "Claim No. KB-2026-000418 was issued.\n"
        found = [f for f in findings(text) if f["category"] == "case_or_investigation_number"]
        self.assertEqual([(text[f["start"]:f["end"]], f["sample"]) for f in found], [("Claim No. KB-2026-000418",) * 2])

    def test_a_bare_repeat_of_a_labelled_identifier_is_its_own_finding(self):
        """The sample is the labelled form now, so a whole-token repeat
        ("claim KB-2026-000418" later in the letter) is reported where it
        stands, under the same pseudonym, for the reviewer to see."""
        text = "Claim No. KB-2026-000418 was issued; claim KB-2026-000418 is stayed. KB-2026-0004189 is not it.\n"
        profile = analyze_privacy("letter.txt", text)
        found = [f for f in profile["risk_map"] if f["category"] == "case_or_investigation_number"]
        self.assertEqual([f["sample"] for f in found], ["Claim No. KB-2026-000418", "KB-2026-000418"])
        self.assertEqual(len({f["placeholder"] for f in found}), 1)
        self.assertTrue(
            profile["redacted_preview"].startswith("[CASE_NUMBER_1] was issued; claim [CASE_NUMBER_1] is stayed.")
        )

    def test_a_repeat_in_other_case_is_still_found(self):
        text = "Claim No. KB-2026-000418 was issued; see kb-2026-000418.\n"
        found = [f for f in findings(text) if f["category"] == "case_or_investigation_number"]
        self.assertEqual([f["sample"] for f in found], ["Claim No. KB-2026-000418", "kb-2026-000418"])

    def test_a_bare_repeat_of_two_case_variants_is_one_finding(self):
        text = "Claim No. KB-2026-000418 was issued. Case No: kb-2026-000418 was listed; see KB-2026-000418 today.\n"
        found = [f for f in findings(text) if f["category"] == "case_or_investigation_number"]
        bare = text.rindex("KB-2026-000418")
        self.assertEqual(
            [(f["start"], f["end"]) for f in found if f["start"] == bare],
            [(bare, bare + len("KB-2026-000418"))],
        )

    def test_a_short_numeric_identifier_keeps_its_label_but_carries_no_repeats(self):
        """"Case No: 2026" keeps its label like every other case number, but
        its whole-token repeats get no finding of their own (they would land
        on every year); the export removes them through the bare-identifier
        needle instead (LabelledIdentifierLeavesEverywhereTests)."""
        text = "Case No: 4821 was listed; case 4821 is stayed.\n"
        found = [f for f in findings(text) if f["category"] == "case_or_investigation_number"]
        self.assertEqual([(text[f["start"]:f["end"]], f["sample"]) for f in found], [("Case No: 4821",) * 2])

    def test_one_identifier_under_two_labels_is_one_pseudonym(self):
        text = "Claim No. KB-2026-000418\nCase Number: KB-2026-000418\n"
        found = [f for f in findings(text) if f["category"] == "case_or_investigation_number"]
        self.assertEqual(len(found), 2)
        self.assertEqual(len({f["placeholder"] for f in found}), 1)

    def test_an_unlabelled_or_short_number_is_not_a_case_number(self):
        for text in ("In this case 2026 was a difficult year.", "Case: 12", "Ref: Clause 4.2", "Reference: 4.2"):
            with self.subTest(text=text):
                self.assertEqual(samples(text, "case_or_investigation_number"), [])


class EnglishAddressTests(unittest.TestCase):
    def test_street_lines_with_their_town_and_postcode(self):
        for address in (
            "179 Cobblewell Street, London EC2A 4NE",
            "Flat 3, 12 Larkspur Road, Manchester M1 2AB",
            "Ground Floor, 1 Example Square, London E14 5AB",
            "221B Baker Street",
            "1180 Example Boulevard, Apartment 6C, Brooklyn, NY 11201",
            "1600 Example Avenue NW, Washington, DC 20500",
        ):
            with self.subTest(address=address):
                self.assertEqual(samples(f"She lives at {address}.", "address"), [address])

    def test_a_postcode_anchors_an_address_without_a_house_number(self):
        self.assertEqual(
            samples("Riverside House, Canal Road, Leeds LS1 4XY\n", "address"),
            ["Riverside House, Canal Road, Leeds LS1 4XY"],
        )
        self.assertEqual(samples("Postcode: SW1A 1AA\n", "address"), ["SW1A 1AA"])

    def test_a_numbered_floor_a_numbered_building_and_the_country_are_the_address(self):
        """"Floor 12" is how a floor is written as often as "12th Floor", a
        block of flats is a numbered building ("40 Example Court"), and the
        country after the postcode is the same line of the same address."""
        for address in (
            "Floor 12, 40 Example Court, Leeds LS1 4AP, United Kingdom",
            "Level 2, 7 Linnet Court, Stockport SK4 2BW",
            "Riverside House, Canal Road, Leeds LS1 4XY, England",
        ):
            with self.subTest(address=address):
                self.assertEqual(samples(f"Registered office: {address}.\n", "address"), [address])

    def test_the_tail_stops_at_running_prose(self):
        self.assertEqual(samples("They met at 12 Rose Lane, Leeds, on 3 March.", "address"), ["12 Rose Lane, Leeds"])

    def test_numbered_prose_is_not_an_address(self):
        for text in (
            "See paragraph 4 High Court practice.",
            "Section 12 Court Order applies.",
            "Payment within 30 days of invoice.",
            "Clause 7 Data Protection obligations survive.",
        ):
            with self.subTest(text=text):
                self.assertEqual(samples(text, "address"), [])


class EnglishAccountNumberTests(unittest.TestCase):
    def test_labelled_uk_bank_details(self):
        """The label stays in the span, as the Turkish "hesap no" rule's does."""
        for detail in (
            "sort code 30-99-47",
            "Sort Code: 30 99 47",
            "account number 41095528",
            "Account No. 41095528",
            "bank account number: 0041095528",
            "a/c no. 41095528",
        ):
            with self.subTest(detail=detail):
                self.assertEqual(samples(f"Pay to {detail}, quoting the invoice.", "account_number"), [detail])

    def test_one_account_under_two_labels_is_one_pseudonym(self):
        text = "Account No. 41095528\nPlease credit account number 41095528 today.\n"
        found = [f for f in findings(text) if f["category"] == "account_number"]
        self.assertEqual(len(found), 2)
        self.assertEqual(len({f["placeholder"] for f in found}), 1)

    def test_prose_about_accounts_is_not_an_account_number(self):
        for text in (
            "The accounts for 2025 were filed late.",
            "Take into account number 4 of the schedule.",
            "Account number to follow.",
            "Sort code and account number are on the invoice.",
        ):
            with self.subTest(text=text):
                self.assertEqual(samples(text, "account_number"), [])


def approved(text: str) -> list[dict]:
    """Every finding analyze_privacy makes in ``text``, approved, as the export reads them."""
    return [
        {**finding, "id": index, "start_offset": finding["start"], "review_status": "approved", "source": "detector"}
        for index, finding in enumerate(findings(text), start=1)
    ]


HYPERLINK_TYPE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink"


def docx_package(body: str, relationship: str, core_title: str) -> bytes:
    w = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    buffer = BytesIO()
    with ZipFile(buffer, "w", ZIP_DEFLATED) as archive:
        archive.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/word/document.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
            "</Types>",
        )
        archive.writestr(
            "_rels/.rels",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
            'Target="word/document.xml"/></Relationships>',
        )
        archive.writestr(
            "word/_rels/document.xml.rels",
            '<?xml version="1.0" encoding="UTF-8"?>'
            f'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">{relationship}'
            "</Relationships>",
        )
        archive.writestr(
            "word/document.xml",
            f'<?xml version="1.0" encoding="UTF-8"?><w:document xmlns:w="{w}"><w:body>'
            f'<w:p><w:r><w:t xml:space="preserve">{escape(body.strip())}</w:t></w:r></w:p></w:body></w:document>',
        )
        archive.writestr(
            "docProps/core.xml",
            '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
            f'xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>{escape(core_title)}</dc:title></cp:coreProperties>',
        )
    return buffer.getvalue()


def pdf_saying(line: str) -> bytes:
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 72), line, fontsize=9)
    data = doc.tobytes()
    doc.close()
    return data


class LabelledIdentifierLeavesEverywhereTests(unittest.TestCase):
    """Approving a labelled English case or account number removes the bare
    identifier wherever it stands, and the output scan still refuses a copy
    the redactor did not reach -- as when the sample was the bare identifier.
    A file name, a URL and a suffixed or prefixed copy are not whole-token
    repeats, so no finding of their own covers them."""

    CASE_TEXT = (
        "Claim No. KB-2026-000418 was issued. See the attached KB-2026-000418.pdf and "
        "https://portal.example/KB-2026-000418 today, order KB-2026-000418/1 and Claim-KB-2026-000418.\n"
    )
    ACCOUNT_TEXT = "Pay to account number 41095528 by Friday and quote 41095528 on the remittance.\n"

    def test_every_copy_of_an_approved_case_number_leaves_the_deliverable(self):
        rendered = plan_text(self.CASE_TEXT, approved(self.CASE_TEXT), {"approved"})
        self.assertNotIn("000418", rendered)
        self.assertIn("[CASE_NUMBER_1].pdf", rendered)

    def test_a_bare_repeat_of_an_approved_account_number_leaves_the_deliverable(self):
        rendered = plan_text(self.ACCOUNT_TEXT, approved(self.ACCOUNT_TEXT), {"approved"})
        self.assertNotIn("41095528", rendered)

    def test_the_output_scan_refuses_a_copy_the_redactor_did_not_reach(self):
        for text, survivor, category in (
            (self.CASE_TEXT, "see KB-2026-000418.pdf", "case_or_investigation_number"),
            (self.CASE_TEXT, "https://portal.example/KB-2026-000418", "case_or_investigation_number"),
            (self.CASE_TEXT, "/KB-2026-000418", "case_or_investigation_number"),
            (self.CASE_TEXT, "order KB-2026-000418/1", "case_or_investigation_number"),
            (self.CASE_TEXT, "Claim-KB-2026-000418", "case_or_investigation_number"),
            (self.ACCOUNT_TEXT, "quote 41095528", "account_number"),
        ):
            with self.subTest(survivor=survivor):
                labelled = [f for f in approved(text) if f["category"] == category and " " in f["sample"]]
                self.assertTrue(labelled)
                leaks = scan_pdf(pdf_saying(survivor), labelled)
                self.assertEqual({leak["category"] for leak in leaks}, {category})

    def test_a_short_numeric_identifier_still_leaves_everywhere(self):
        text = "Case No: 4821 was listed; case 4821 is stayed.\n"
        rendered = plan_text(text, approved(text), {"approved"})
        self.assertNotIn("4821", rendered)

    def test_a_copy_outside_the_extracted_text_is_redacted_in_the_docx(self):
        """A hyperlink target and a document property carry the identifier
        where no finding could be made: the extracted text never reads them."""
        body = "Claim No. KB-2026-000418 was issued against the defendant company.\n"
        source = docx_package(
            body,
            relationship=f'<Relationship Id="rId9" Type="{HYPERLINK_TYPE}" '
            'Target="https://portal.example/KB-2026-000418" TargetMode="External"/>',
            core_title="Bundle for KB-2026-000418",
        )
        approved_findings = [f for f in approved(body) if f["category"] == "case_or_investigation_number"]
        self.assertEqual([f["sample"] for f in approved_findings], ["Claim No. KB-2026-000418"])
        self.assertTrue(scan_docx(source, approved_findings))
        with TemporaryDirectory() as directory:
            path = Path(directory) / "claim.docx"
            path.write_bytes(source)
            _, targets, _ = redaction_inputs(body, approved_findings, {"approved"})
            output = redact_docx(path, targets)
        self.assertEqual(scan_docx(output, approved_findings), [])
        with ZipFile(BytesIO(output)) as archive:
            for name in archive.namelist():
                with self.subTest(part=name):
                    self.assertNotIn(b"000418", archive.read(name))


class EnglishCompanyFormTests(unittest.TestCase):
    def test_an_llp_is_a_company(self):
        """An English law firm's form; the firm name is its partners' surnames."""
        self.assertEqual(
            samples("the solicitors are Penhallow & Grieve LLP, who act for her.", "company_name"),
            ["Penhallow & Grieve LLP"],
        )


class EnglishFixturesTests(unittest.TestCase):
    """The six English gold documents, read for what the gold audit cannot see."""

    @classmethod
    def setUpClass(cls):
        gold = json.loads(GOLD.read_text(encoding="utf-8"))
        cls.documents = [
            (doc, (GOLD.parent / doc["path"]).resolve())
            for doc in gold["documents"]
            if "/english/" in doc["path"]
        ]

    def test_the_gold_set_carries_every_english_fixture(self):
        self.assertEqual(len(ENGLISH_FIXTURES), 6)
        self.assertEqual(sorted(path for _doc, path in self.documents), ENGLISH_FIXTURES)

    def test_every_labelled_identifier_is_covered_end_to_end(self):
        """Full span, every occurrence: the audit's substring match scores a
        fragment as a hit, and the uncovered characters are what ships."""
        for doc, path in self.documents:
            text = path.read_text(encoding="utf-8")
            risk_map = findings(text)
            for label in doc["labels"]:
                spans = [(f["start"], f["end"]) for f in risk_map if f["category"] == label["category"]]
                start = text.find(label["text"])
                self.assertNotEqual(start, -1, label)
                while start != -1:
                    with self.subTest(document=doc["id"], label=label["text"], at=start):
                        self.assertTrue(fully_covered(spans, start, start + len(label["text"])))
                    start = text.find(label["text"], start + 1)

    def test_no_labelled_identifier_survives_the_preview(self):
        for doc, path in self.documents:
            text = path.read_text(encoding="utf-8")
            preview = build_redacted_preview(text, findings(text), max_chars=len(text) * 2)
            for label in doc["labels"]:
                with self.subTest(document=doc["id"], label=label["text"]):
                    self.assertNotIn(label["text"], preview)

    def test_no_finding_spans_a_line_break(self):
        """A finding across a line break matches nothing in a DOCX part, whose
        text has no newline at a paragraph boundary, so it could never be
        removed from the deliverable. Read off the SPAN: a name's sample is
        rebuilt from its words, so the sample alone cannot show it."""
        for path in ENGLISH_FIXTURES:
            text = path.read_text(encoding="utf-8")
            for finding in findings(text):
                with self.subTest(fixture=path.name, sample=finding["sample"]):
                    self.assertNotIn("\n", text[finding["start"]:finding["end"]])
                    self.assertNotIn("\n", finding["sample"])


class TurkishIsUntouchedTests(unittest.TestCase):
    """The English rules run on every document, so they must add nothing to a
    Turkish one: no finding, and through the findings no change to the
    language answer the release gate reads."""

    def without_english(self, text: str) -> dict:
        rules = [rule for rule in privacy.DETECTION_RULES if rule not in ENGLISH_RULES]
        with mock.patch.object(privacy, "DETECTION_RULES", rules), mock.patch.object(
            privacy, "detect_english_person_names", lambda _text: []
        ):
            return analyze_privacy("belge.txt", text)

    def test_the_english_rules_add_nothing_to_the_turkish_fixtures(self):
        paths = sorted(FIXTURES.glob("*.txt")) + sorted((FIXTURES / "nda").glob("*.txt"))
        self.assertGreaterEqual(len(paths), 18)
        for path in paths:
            text = path.read_text(encoding="utf-8")
            with self.subTest(fixture=path.name):
                with_english = analyze_privacy("belge.txt", text)
                baseline = self.without_english(text)
                self.assertEqual(with_english["risk_map"], baseline["risk_map"])
                self.assertEqual(with_english["language_coverage"], baseline["language_coverage"])

    def test_the_english_rules_are_really_removed_by_the_baseline(self):
        """The comparison above is vacuous if the patch removes nothing."""
        text = (FIXTURES / "english" / "gold_en_witness_statement.txt").read_text(encoding="utf-8")
        self.assertLess(len(self.without_english(text)["risk_map"]), len(findings(text)))


class TheLanguageGateIsNotLoosenedTests(unittest.TestCase):
    """English findings give a reviewer something to approve. They do not make
    the Turkish-tuned gate able to clear an English document."""

    def test_a_fully_reviewed_english_document_is_still_refused(self):
        english = [path for path in ENGLISH_FIXTURES if path.name != "gold_en_annex_form.txt"]
        self.assertEqual(len(english), 5)
        for path in english:
            text = path.read_text(encoding="utf-8")
            profile = analyze_privacy(path.name, text)
            with self.subTest(fixture=path.name):
                self.assertFalse(profile["language_coverage"]["supported"])
                self.assertTrue(profile["risk_map"])
                refreshed = refresh_release_state(
                    profile,
                    redaction_completed=True,
                    human_review_approved=True,
                    unresolved_critical_count=0,
                    direct_identifiers_remaining=False,
                    remaining_findings=[],
                )
                self.assertFalse(refreshed["external_llm_gate"]["allowed"])
                self.assertIn(privacy.LANGUAGE_COVERAGE_CONDITION, refreshed["external_llm_gate"]["failed_conditions"])

    def test_an_english_annex_in_labelled_fields_is_now_held_for_review(self):
        """The annex still reads as Turkish -- its fields carry too few English
        function words to trip the language measure -- so what holds it is
        that its identifiers are now findings."""
        text = (FIXTURES / "english" / "gold_en_annex_form.txt").read_text(encoding="utf-8")
        profile = analyze_privacy("ek.txt", text)
        self.assertTrue(profile["language_coverage"]["supported"])
        self.assertEqual(
            {f["category"] for f in profile["risk_map"]}, {"natural_person_name", "address", "phone_number"}
        )
        self.assertNotEqual(profile["residual_risk"]["level"], "Low")
        self.assertFalse(profile["external_llm_gate"]["allowed"])
        self.assertTrue(profile["human_review_required"])


if __name__ == "__main__":
    unittest.main()
