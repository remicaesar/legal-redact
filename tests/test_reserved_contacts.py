"""Synthetic contact details must come from ranges no real person can hold.

The eval corpus and the test fixtures are published with the repository, so a
phone number or an email domain in them has to be one that can never ring a
real subscriber or be registered by a stranger. The convention and how each
range was chosen is in ``eval/README.md`` ("Reserved contact ranges").

The allowed ranges are written out HERE rather than imported from
``eval/generate_corpus.py``: a test that read the generator's own constants
would move with the generator and could never fail.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

from tests.test_accuracy_eval import load_generator

PROJECT_DIR = Path(__file__).resolve().parents[1]

# Ofcom's drama ranges: 020 7946 0000-0999 (London) and 07700 900000-900999.
UK_RESERVED = re.compile(r"(?:\+44 ?20 ?|020 ?)7946 ?0\d{3}|(?:\+44 ?|0)7700 ?900\d{3}")
# NANPA reserves 555-0100 through 555-0199 in every area code for fictional use.
NANP_RESERVED = re.compile(r"(?:\+1[ .-]?)?(?:\(\d{3}\) ?|\d{3}[ .-])555[ .-]01\d{2}")
# The 52x mobile block is allocated to no operator in BTK's numbering report.
TR_MOBILE_RESERVED = re.compile(r"(?:\+90 ?|0)52\d ?\d{3} ?\d{2} ?\d{2}")
# Geographic codes BTK has allocated (NMS numbering report, 23/09/2026). A
# Turkish landline in a fixture must use a code outside this list.
TR_ALLOCATED_AREA_CODES = frozenset("""
    212 216 222 224 226 228 232 236 242 246 248 252 256 258 262 264 266 272
    274 276 282 284 286 288 312 318 322 324 326 328 332 338 342 344 346 348
    352 354 356 358 362 364 366 368 370 372 374 376 378 380 382 384 386 388
    412 414 416 422 424 426 428 432 434 436 438 442 446 452 454 456 458 462
    464 466 472 474 476 478 482 484 486 488
""".split())
# RFC 2606 / RFC 6761: the example second-level names and the reserved TLDs.
RESERVED_EMAIL_DOMAIN = re.compile(
    r"(?:[a-z0-9-]+\.)*(?:example\.(?:com|org|net)|[a-z0-9-]+\.(?:test|example|invalid|localhost))"
)

EMAIL = re.compile(r"[\w.%+-]+@([\w-]+(?:\.[\w-]+)+)")
INTERNATIONAL = re.compile(r"\+\d{2}[\d ]{8,14}\d")
# The operator code may sit in brackets, "(0522) 604 17 38" or "+90 (522) ...",
# the way contract party blocks print it; without them here such a number was
# invisible to this scan whatever range it came from.
TR_MOBILE = re.compile(r"(?<![\d(])(?:\+90 ?)?\(?0?5\d{2}\)?[ .-]?\d{3}[ .-]?\d{2}[ .-]?\d{2}(?!\d)")
TR_LANDLINE = re.compile(r"(?<!\d)\(?0([2-4]\d{2})\)?[ .-]\d{3}[ .-]?\d{2}[ .-]?\d{2}(?!\d)")
UK_NATIONAL = re.compile(r"(?<!\d)0[1-9]\d{1,3} \d{3,4} ?\d{3,4}(?!\d)")
NANP = re.compile(r"(?<![\d+])(?:\+1[ .-]?)?(?:\([2-9]\d{2}\) ?|[2-9]\d{2}[ .-])[2-9]\d{2}[ .-]\d{4}(?!\d)")


def contact_problems(text: str) -> list[str]:
    """Every phone or email shape in `text` that is outside the reserved ranges."""
    problems = []
    for match in EMAIL.finditer(text):
        if not RESERVED_EMAIL_DOMAIN.fullmatch(match.group(1).lower()):
            problems.append(f"email domain {match.group(1)!r} in {match.group(0)!r}")
    for pattern in (INTERNATIONAL, TR_MOBILE, UK_NATIONAL):
        for match in pattern.finditer(text):
            number = match.group(0).strip().replace("(", "").replace(")", "")
            if pattern is TR_MOBILE and not number.replace(" ", "").lstrip("+9").startswith("0"):
                # Ten bare digits starting 5 are a VKN far more often than a
                # phone; a phone written without its trunk 0 is still caught
                # by INTERNATIONAL when it carries +90.
                continue
            if not (UK_RESERVED.fullmatch(number) or TR_MOBILE_RESERVED.fullmatch(number)):
                problems.append(f"phone {number!r}")
    for match in NANP.finditer(text):
        if not NANP_RESERVED.fullmatch(match.group(0)):
            problems.append(f"North American number {match.group(0)!r}")
    for match in TR_LANDLINE.finditer(text):
        if match.group(1) in TR_ALLOCATED_AREA_CODES:
            problems.append(f"landline {match.group(0)!r} uses allocated area code 0{match.group(1)}")
    return problems


class GeneratedCorpusUsesReservedContactsTests(unittest.TestCase):
    """What eval/generate_corpus.py emits, checked as labels and as raw text."""

    @classmethod
    def setUpClass(cls):
        cls.manifest, cls.texts = load_generator().build()

    def labels(self, category):
        return [
            label["text"]
            for doc in self.manifest["documents"]
            for label in doc["labels"]
            if label["category"] == category
        ]

    def test_every_generated_phone_is_in_a_reserved_range(self):
        phones = self.labels("phone_number")
        self.assertEqual(len(phones), 28)  # an empty list must not pass
        for phone in phones:
            with self.subTest(phone=phone):
                self.assertTrue(
                    UK_RESERVED.fullmatch(phone) or TR_MOBILE_RESERVED.fullmatch(phone),
                    f"{phone!r} is not in a reserved range",
                )

    def test_every_generated_email_uses_a_reserved_domain(self):
        emails = self.labels("email_address")
        self.assertEqual(len(emails), 44)
        for email in emails:
            with self.subTest(email=email):
                self.assertTrue(RESERVED_EMAIL_DOMAIN.fullmatch(email.rsplit("@", 1)[1]))

    def test_no_unlabelled_contact_detail_hides_in_the_text(self):
        for name, text in self.texts.items():
            with self.subTest(document=name):
                self.assertEqual(contact_problems(text), [])


class FixturesUseReservedContactsTests(unittest.TestCase):
    """The hand-written fixtures and gold labels follow the same rule."""

    # Recursive, and JSON as well as text: tests/fixtures/nda/ carries the
    # end-to-end NDA and the identifiers its test expects to see removed.
    PATHS = sorted((PROJECT_DIR / "tests" / "fixtures").rglob("*.txt")) + sorted(
        (PROJECT_DIR / "tests" / "fixtures").rglob("*.json")
    ) + [
        PROJECT_DIR / "gold" / "gold_labels.example.json",
        PROJECT_DIR / "samples" / "build_turkish_samples.py",
    ]

    def test_fixtures_carry_no_assignable_contact_detail(self):
        self.assertGreater(len(self.PATHS), 10)
        for path in self.PATHS:
            with self.subTest(path=path.name):
                self.assertEqual(contact_problems(path.read_text(encoding="utf-8")), [])

    def test_the_shipped_samples_carry_no_assignable_contact_detail(self):
        """What the app loads, read the way the app reads it."""
        from legal_analyzer.extraction import extract_text

        documents = sorted((PROJECT_DIR / "samples" / "documents").glob("*.docx"))
        self.assertGreaterEqual(len(documents), 5)
        for path in documents:
            with self.subTest(path=path.name):
                text, _warning = extract_text(path)
                self.assertEqual(contact_problems(text), [])

    def test_the_scanner_rejects_real_ranges(self):
        # The scan above passes on silence, so prove it can speak.
        for real in (
            "Tel: 0532 111 22 33",
            "Tel: (0532) 111 22 33",
            "+90 (532) 111 22 33",
            "+44 20 7123 4567",
            "020 7123 4567",
            "07700 123456",
            "0212 345 67 89",
            "x@exampleco.co.uk",
            "x@ornekhukuk.av.tr",
            "x@acme.com",
            "(212) 456-7890",
            "212-456-0147",
        ):
            with self.subTest(real=real):
                self.assertNotEqual(contact_problems(real), [])
        for reserved in (
            "Tel: 0522 111 22 33",
            "Tel: (0522) 111 22 33",
            "+90 (522) 111 22 33",
            "+44 20 7946 0123",
            "020 7946 0123",
            "07700 900318",
            "0299 345 67 89",
            "x@ornekhukuk.example.org",
            "x@a.example",
            "(212) 555-0147",
            "+1 212 555 0199",
        ):
            with self.subTest(reserved=reserved):
                self.assertEqual(contact_problems(reserved), [])


if __name__ == "__main__":
    unittest.main()
