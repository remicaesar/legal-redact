"""The synthetic Turkish NDA, end to end: upload, review, reviewed DOCX.

The document is `tests/fixtures/nda/mock_nda_tr.txt`, the text of the mock
mutual NDA between Kestane Yazılım A.Ş. and Defne Lojistik Ltd. Şti. that the
semantic-redaction work was reported on. Reviewed in the Studio, clauses 2-4
came back as [PRIVILEGED_CONTENT_n_REDACTED], the penalty clause as
[CRIMINAL_ALLEGATION_n_REDACTED], and "Çankaya Vergi Dairesi" as a court. The
identifiers were all caught; the legal substance was gone.

Each test drives the real HTTP surfaces: the DOCX is uploaded through
/api/upload, decided through review-batch, marked redacted and approved, and
the reviewed DOCX the export endpoint returns is read back paragraph by
paragraph from its `w:t` runs -- the text a reader of the delivered file sees.
The decision used is the worst case: every finding approved, which is what the
Studio's "approve all" does and what deleted the clauses.

What must be gone and what must still read is listed in
`mock_nda_tr.expected.json`, beside the fixture.
"""

from __future__ import annotations

import json
import re
import sqlite3
import unittest
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock
from defusedxml import ElementTree
from xml.sax.saxutils import escape
from zipfile import ZIP_DEFLATED, ZipFile

import tests.env_setup  # noqa: F401  -- sets LEGAL_ANALYZER_SECRET_KEY before app is imported
import app as app_module
from legal_analyzer import redaction_policy
from legal_analyzer.semantic_preservation import legal_semantic_preservation
from legal_analyzer.taxonomy import CATEGORIES, SUBCATEGORIES
from legal_analyzer.turkish_names import tr_fold
from werkzeug.security import generate_password_hash

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "nda"
NDA_TEXT = (FIXTURE_DIR / "mock_nda_tr.txt").read_text(encoding="utf-8")
EXPECTED = json.loads((FIXTURE_DIR / "mock_nda_tr.expected.json").read_text(encoding="utf-8"))
W_NAMESPACE = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
PLACEHOLDER_RE = re.compile(r"\[([A-Z_]+)_(\d+)\]")


def nda_docx(path: Path) -> None:
    """The fixture as a DOCX, one paragraph per line, the way the original is laid out."""
    paragraphs = "".join(
        f"<w:p><w:r><w:t xml:space=\"preserve\">{escape(line)}</w:t></w:r></w:p>"
        for line in NDA_TEXT.splitlines()
    )
    with ZipFile(path, "w", ZIP_DEFLATED) as archive:
        archive.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/word/document.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
            "</Types>",
        )
        archive.writestr(
            "_rels/.rels",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
            'Target="word/document.xml"/></Relationships>',
        )
        archive.writestr(
            "word/document.xml",
            f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<w:document xmlns:w="{W_NAMESPACE}"><w:body>{paragraphs}</w:body></w:document>',
        )


def identifiers() -> list[str]:
    return [value for values in EXPECTED["identifiers"].values() for value in values]


class NdaHarness(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tmp_path = Path(self.tmp.name)
        db_path = self.tmp_path / "nda.db"
        previous = (app_module.DB_PATH, app_module.EXPORT_DIR)
        app_module.DB_PATH = db_path
        app_module.EXPORT_DIR = self.tmp_path / "exports"
        app_module.READY_DB_PATHS.discard(db_path.resolve())

        def restore() -> None:
            app_module.DB_PATH, app_module.EXPORT_DIR = previous
            app_module.READY_DB_PATHS.discard(db_path.resolve())

        self.addCleanup(restore)

        conn = sqlite3.connect(db_path)
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
        conn.commit()
        conn.close()

        self.client = app_module.app.test_client()
        self.client.post("/login", json={"username": "reviewer", "password": "secret"})

        source = self.tmp_path / "Mock-NDA-TR.docx"
        nda_docx(source)
        with mock.patch.object(app_module, "UPLOAD_DIR", self.tmp_path / "uploads"):
            response = self.client.post(
                "/api/upload",
                data={"file": (BytesIO(source.read_bytes()), source.name)},
                content_type="multipart/form-data",
            )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.document = response.get_json()
        self.base = f"/api/document/{self.document['id']}"

    def findings(self) -> list[dict]:
        response = self.client.get(self.base)
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        return response.get_json()["findings"]

    def decide(self, action: str, **selector) -> int:
        response = self.client.post(
            f"{self.base}/findings/review-batch", json={"action": action, "only_pending": True, **selector}
        )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        return response.get_json()["updated"]

    def exported_paragraphs(self) -> list[str]:
        """Mark redacted, approve, and read the reviewed DOCX back one paragraph per line."""
        for action in ("mark_redacted", "approve"):
            response = self.client.post(f"{self.base}/review", json={"action": action})
            self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        response = self.client.get(f"{self.base}/redacted-export?format=docx")
        self.assertEqual(response.status_code, 200, response.get_data()[:400])
        exported = self.tmp_path / "reviewed.docx"
        exported.write_bytes(response.get_data())
        response.close()
        with ZipFile(exported) as archive:
            root = ElementTree.fromstring(archive.read("word/document.xml"))
        return [
            "".join(node.text or "" for node in paragraph.iter(f"{{{W_NAMESPACE}}}t"))
            for paragraph in root.iter(f"{{{W_NAMESPACE}}}p")
        ]

    def approve_all_export(self) -> list[str]:
        self.assertGreater(self.decide("approve"), 0)
        return self.exported_paragraphs()


class TheReportedMisclassificationsAreGoneTests(NdaHarness):
    """The three failures, asserted on what the upload stored."""

    def test_no_clause_of_the_nda_is_reported_as_privileged(self) -> None:
        privileged = [f["sample"] for f in self.findings() if f["category"] == "privileged_or_confidential"]
        self.assertEqual(privileged, [])

    def test_the_penalty_clause_is_not_a_criminal_allegation(self) -> None:
        criminal = [f["sample"] for f in self.findings() if f["category"] == "criminal_allegation"]
        self.assertEqual(criminal, [])

    def test_the_tax_office_is_a_tax_authority_and_the_court_a_commercial_court(self) -> None:
        by_sample = {f["sample"]: f["placeholder"] for f in self.findings() if f["category"] == "court_or_authority"}
        self.assertRegex(by_sample["Çankaya Vergi Dairesi"], r"^\[TAX_AUTHORITY_\d+\]$")
        self.assertRegex(by_sample["İstanbul Anadolu 4. Asliye Ticaret Mahkemesi"], r"^\[COMMERCIAL_COURT_\d+\]$")
        self.assertFalse([p for p in by_sample.values() if "COURT_AUTHORITY" in p], by_sample)

    def test_the_penalty_amount_is_typed_as_one(self) -> None:
        amounts = [f for f in self.findings() if f["category"] == "money_amount"]
        self.assertEqual([(f["sample"], f["placeholder"]) for f in amounts], [("250.000 TL", "[PENALTY_AMOUNT_1]")])

    def test_the_release_gate_stays_closed_until_the_identifiers_are_decided(self) -> None:
        """Privacy is the floor: nothing here may make the NDA clearable unreviewed."""
        detail = self.client.get(self.base).get_json()
        gate = detail["privacy_profile"]["external_llm_gate"]
        self.assertFalse(gate["allowed"])
        failed = {condition["id"] for condition in gate["conditions"] if not condition["passed"]}
        self.assertTrue({"no_critical_findings", "no_direct_identifiers", "human_review_approved"} <= failed, failed)


class ApproveAllExportTests(NdaHarness):
    """Every finding approved -- the worst case -- and the delivered file read back."""

    def setUp(self) -> None:
        super().setUp()
        self.paragraphs = self.approve_all_export()
        self.delivered = "\n".join(self.paragraphs)

    def test_no_identifier_reaches_the_delivered_file(self) -> None:
        folded = tr_fold(self.delivered)
        for value in identifiers():
            with self.subTest(identifier=value):
                if " " in value or not value.isalpha():
                    self.assertNotIn(tr_fold(value), folded)
                else:
                    # A one-word alias ("Kestane", "Defne") is checked as a word.
                    self.assertIsNone(re.search(rf"(?<!\w){re.escape(tr_fold(value))}(?!\w)", folded))

    def test_every_legal_provision_still_reads_word_for_word(self) -> None:
        for element, anchor in EXPECTED["legal_provisions"].items():
            with self.subTest(element=element):
                self.assertTrue(
                    any(anchor in paragraph for paragraph in self.paragraphs),
                    f"{element} is not in the delivered file: {anchor!r}",
                )

    def test_nothing_is_removed_as_a_whole_passage(self) -> None:
        self.assertNotIn("_REDACTED]", self.delivered)
        for retired in ("[PRIVILEGED_CONTENT_", "[CRIMINAL_ALLEGATION_", "[COURT_AUTHORITY_", "[REDACTED]"):
            self.assertNotIn(retired, self.delivered)

    def test_the_penalty_clause_loses_only_its_amount(self) -> None:
        self.assertIn(
            "Gizlilik yükümlülüğünü ihlal eden taraf, diğer tarafa her bir ihlal için "
            "[PENALTY_AMOUNT_1] cezai şart ödemeyi kabul eder.",
            self.delivered,
        )

    def test_roles_survive_beside_the_pseudonymised_names(self) -> None:
        signatory = next(p for p in self.paragraphs if p.startswith("Temsilcisi:") and "Genel Müdür" in p)
        self.assertRegex(signatory, r"^Temsilcisi: \[PERSON_\d+\], Genel Müdür, TCKN: \[NATIONAL_ID_\d+\]$")
        self.assertIn("Vergi Dairesi: [TAX_AUTHORITY_1], [TAX_NUMBER_", self.delivered)

    def test_one_company_is_one_placeholder_everywhere_it_is_named(self) -> None:
        """Full name, defined short name and the account holder are one entity."""
        holder = next(p for p in self.paragraphs if p.startswith("Hesap sahibi:"))
        kestane = holder.removeprefix("Hesap sahibi: ")
        self.assertRegex(kestane, r"^\[COMPANY_\d+\]$")
        parties = next(p for p in self.paragraphs if p.startswith("İşbu Karşılıklı Gizlilik Sözleşmesi"))
        self.assertIn(f'bir tarafta {kestane} (bundan sonra "{kestane}" olarak anılacaktır)', parties)
        notice = next(p for p in self.paragraphs if "adına irtibat kişisi" in p)
        self.assertIn(f"{kestane} adına irtibat kişisi", notice)

        defne = re.search(r'diğer tarafta (\[COMPANY_\d+\]) \(bundan sonra "\1" olarak anılacaktır\)', parties)
        self.assertIsNotNone(defne, parties)
        self.assertNotEqual(defne.group(1), kestane)
        self.assertIn(f"{defne.group(1)} adına irtibat kişisi", notice)

        # Exactly two companies and four people are named, and the file says so.
        kinds = {}
        for kind, number in PLACEHOLDER_RE.findall(self.delivered):
            kinds.setdefault(kind, set()).add(number)
        self.assertEqual(len(kinds["COMPANY"]), 2, kinds)
        self.assertEqual(len(kinds["PERSON"]), 4, kinds)
        self.assertEqual(len(kinds["NATIONAL_ID"]), 2, kinds)
        self.assertEqual(len(kinds["MERSIS"]), 2, kinds)

    def test_the_signature_date_keeps_one_placeholder(self) -> None:
        dates = set(re.findall(r"\[(?:SIGNATURE_)?DATE_\d+\]", self.delivered))
        self.assertEqual(len(dates), 1, dates)

    def test_both_dimensions_score_the_delivered_file_as_usable(self) -> None:
        semantic = legal_semantic_preservation(NDA_TEXT, self.delivered)
        self.assertEqual((semantic.kept, semantic.terms), (semantic.terms, semantic.terms), semantic.by_kind)
        self.assertGreater(semantic.terms, 30)
        leaked = [value for value in identifiers() if value in self.delivered]
        self.assertEqual(leaked, [])


class PolicyFollowingReviewTests(NdaHarness):
    """LEGAL_CONTRACT_REVIEW's other amount option: keep the exact amount.

    The profile recommends; a person decides. Retaining the amount -- what the
    policy recommends with preserve_amounts set -- keeps "250.000 TL" while
    every identifier is still removed.
    """

    def test_retaining_the_amount_keeps_it_and_nothing_else(self) -> None:
        profile = redaction_policy.build_profile(redaction_policy.LEGAL_CONTRACT_REVIEW, preserve_amounts=True)
        self.assertEqual(profile.treatment("PENALTY_AMOUNT"), redaction_policy.PRESERVE)

        self.assertEqual(self.decide("retain", categories=["money_amount"]), 1)
        self.assertGreater(self.decide("approve"), 0)
        delivered = "\n".join(self.exported_paragraphs())
        self.assertIn("her bir ihlal için 250.000 TL cezai şart ödemeyi kabul eder.", delivered)
        self.assertEqual([value for value in identifiers() if value in delivered], [])


if __name__ == "__main__":
    unittest.main()
