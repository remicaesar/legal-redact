"""An approved finding redacts the place where it was found.

The shared resolver (``docx_redactor.resolve_redaction``) used to have one way
to reach a character: every case-insensitive match of an approved sample,
widened to word bounds. Two things followed from that, and these tests pin the
replacement for each:

* A sample's text in a DIFFERENT ROLE was redacted on the strength of that
  finding alone. The bare surname "Kartal" approved for a party rewrote the
  district inside a court the reviewer had retained: "Ankara [PERSON_1] 3.
  Asliye Hukuk Mahkemesi". A retained finding now keeps its text, and the
  reviewed-DOCX export refuses -- naming both decisions -- rather than ship a
  file still carrying the approved item (the output scan would refuse it too).
* Two findings with the same text took one placeholder between them, whichever
  target was listed first: the bare mentions of two different people both named
  "Kartal" both read [PERSON_1]. Each finding is now anchored at its own
  occurrence, in the canvas and in every DOCX part alike.

What did not change, and must not: every other match of an approved sample is
still redacted (the backstop), so a repeat detection missed ("EKİN KARTAL",
"ekin kartal" after a detected "Ekin Kartal") never ships.

Every name, court and district use here is synthetic.
"""

import json
import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from xml.sax.saxutils import escape
from zipfile import ZIP_DEFLATED, ZipFile

import pymupdf  # type: ignore

import tests.env_setup  # noqa: F401  -- sets LEGAL_ANALYZER_SECRET_KEY before app is imported
import app as app_module
from legal_analyzer.docx_redactor import Occurrence, RedactionTarget, redact_docx, resolve_redaction
from legal_analyzer.extraction import extract_docx_text, extract_text, normalize_layout
from legal_analyzer.pdf_redactor import find_pdf_regions
from legal_analyzer.privacy import REDACTED_REVIEW_STATUSES, analyze_privacy
from legal_analyzer.redaction_plan import held_by_retained, plan_text, redaction_inputs
from werkzeug.security import generate_password_hash

# "Kartal" is the party's surname at its bare mention, and the district in the
# second court -- "Ankara Kartal 3. ..." keeps the district with the venue.
DIFFERENT_ROLE = (
    "Davacı Ekin Kartal vekili aracılığıyla dava açmıştır.\n"
    "Kartal 5. Asliye Hukuk Mahkemesine dilekçe sunmuştur.\n"
    "Dosya Ankara Kartal 3. Asliye Hukuk Mahkemesinden gönderilmiştir."
)
# The span ends at the stem "Mahkemesi"; the case ending of "Mahkemesinden" is
# outside it, as for every court rule (privacy._AUTHORITY_CASE_END).
RETAINED_COURT = "Ankara Kartal 3. Asliye Hukuk Mahkemesi"

# The detector finds "Ekin Kartal", and name propagation finds its capitalised
# repeat "EKİN KARTAL"; the lowercased repeat is found by nobody, so only the
# backstop removes it.
UNDETECTED_REPEAT = (
    "Davacı Ekin Kartal vekili aracılığıyla dava açmıştır.\n"
    "Davacı vekili, EKİN KARTAL adına beyanda bulunmuştur. ekin kartal imzası mevcuttur."
)

# Two people share a surname, and each is later named by it alone: the first
# bare "Kartal" is Ekin's, the second Deniz's.
SHARED_SURNAME = (
    "Davacı Ekin Kartal dava açmıştır.\n"
    "Kartal 5. Asliye Hukuk Mahkemesine başvurmuştur.\n"
    "Davalı Deniz Kartal cevap vermiştir.\n"
    "Kartal 3. Asliye Hukuk Mahkemesine itiraz etmiştir."
)

WORD_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
</Types>
"""
RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>
"""


def _part(root: str, lines: list[str]) -> str:
    body = "".join(f'<w:p><w:r><w:t xml:space="preserve">{escape(line)}</w:t></w:r></w:p>' for line in lines)
    inner = f"<w:body>{body}</w:body>" if root == "document" else body
    return f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:{root} xmlns:w="{WORD_NS}">{inner}</w:{root}>'


def write_docx(path: Path, body: str, header: str | None = None) -> None:
    """One paragraph per line; an optional header part, read before the body."""
    with ZipFile(path, "w", ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", CONTENT_TYPES)
        archive.writestr("_rels/.rels", RELS)
        archive.writestr("word/document.xml", _part("document", body.splitlines()))
        if header is not None:
            archive.writestr("word/header1.xml", _part("hdr", header.splitlines()))


def rows_for(text: str, decide=lambda finding: "approved") -> list[dict]:
    """The detector's findings on ``text`` as stored rows, decided by ``decide``."""
    return [
        dict(
            finding,
            id=index + 1,
            review_status=decide(finding),
            source="detector",
            start_offset=finding["start"],
            end_offset=finding["end"],
            replacement_text=None,
        )
        for index, finding in enumerate(analyze_privacy("dilekce.txt", text)["risk_map"])
    ]


class OwnPositionTests(unittest.TestCase):
    def test_a_retained_finding_keeps_text_an_approved_sample_shares_with_it(self):
        """The district inside a retained court is not the approved surname."""
        rows = rows_for(DIFFERENT_ROLE, lambda f: "retained" if f["category"] == "court_or_authority" else "approved")
        self.assertIn(RETAINED_COURT, [row["sample"] for row in rows])
        rendered = plan_text(DIFFERENT_ROLE, rows, REDACTED_REVIEW_STATUSES)
        self.assertIn("Dosya Ankara Kartal 3. Asliye Hukuk Mahkemesinden", rendered)
        # The bare surname itself, where it was found, still goes.
        self.assertIn("[PERSON_1] 5. Asliye Hukuk Mahkemesine", rendered)

    def test_a_held_match_is_reported_naming_both_findings(self):
        rows = rows_for(DIFFERENT_ROLE, lambda f: "retained" if f["category"] == "court_or_authority" else "approved")
        holds = held_by_retained(DIFFERENT_ROLE, rows, REDACTED_REVIEW_STATUSES)
        court = next(row for row in rows if row["sample"] == RETAINED_COURT)
        surname = next(row for row in rows if row["sample"] == "Kartal")
        self.assertEqual([(h.finding_id, h.retained_finding_id) for h in holds], [(surname["id"], court["id"])])
        self.assertEqual((holds[0].sample, holds[0].retained_sample), ("Kartal", RETAINED_COURT))

    def test_a_dismissed_finding_holds_nothing_back(self):
        """"Not sensitive as a court" is not "keep every word in it"."""
        rows = rows_for(DIFFERENT_ROLE, lambda f: "dismissed" if f["category"] == "court_or_authority" else "approved")
        rendered = plan_text(DIFFERENT_ROLE, rows, REDACTED_REVIEW_STATUSES)
        self.assertIn("Dosya Ankara [PERSON_1] 3. Asliye Hukuk", rendered)
        self.assertEqual(held_by_retained(DIFFERENT_ROLE, rows, REDACTED_REVIEW_STATUSES), [])

    def test_an_undetected_repeat_of_a_genuine_finding_is_still_redacted(self):
        rows = rows_for(UNDETECTED_REPEAT)
        self.assertEqual(
            [row["sample"] for row in rows if row["category"] == "natural_person_name"], ["Ekin Kartal", "EKİN KARTAL"]
        )
        rendered = plan_text(UNDETECTED_REPEAT, rows, REDACTED_REVIEW_STATUSES)
        self.assertNotIn("EKİN KARTAL", rendered)
        self.assertNotIn("ekin kartal", rendered)
        self.assertEqual(rendered.count("[PERSON_1]"), 3, rendered)

    def test_each_mention_takes_the_placeholder_of_the_finding_made_there(self):
        rows = rows_for(SHARED_SURNAME)
        mentions = [row for row in rows if row["sample"] == "Kartal"]
        self.assertEqual([row["placeholder"] for row in mentions], ["[PERSON_1]", "[PERSON_2]"])
        rendered = plan_text(SHARED_SURNAME, rows, REDACTED_REVIEW_STATUSES)
        self.assertIn("[PERSON_1] [COURT_1] başvurmuştur", rendered)
        self.assertIn("[PERSON_2] [COURT_2] itiraz", rendered)

    def test_an_anchor_does_not_resolve_against_a_text_that_disagrees(self):
        """A rendering holding a different number of exact occurrences gets no own position."""
        found_at = Occurrence.at("Kartal ve Kartal", "Kartal", 10)
        self.assertEqual(found_at, Occurrence(1, 2))
        self.assertEqual(found_at.locate("Kartal ve Kartal", "Kartal"), (10, 16))
        self.assertIsNone(found_at.locate("Kartal ve Kartal ve Kartal", "Kartal"))
        self.assertIsNone(Occurrence.at("Kartal ve Kartal", "Kartal", 3))

    def test_an_unanchored_target_falls_back_to_the_backstop(self):
        text = "Kartal ve Kartal"
        unanchored = resolve_redaction(text, [RedactionTarget("Kartal", "[P1]")]).regions
        self.assertEqual([(start, end) for start, end, _, _ in unanchored], [(0, 6), (10, 16)])


class DocxAgreementTests(unittest.TestCase):
    """The reviewed DOCX resolves each anchor in the part it lands in, exactly as the canvas does."""

    def render(self, body: str, header: str | None = None) -> tuple[str, str]:
        with TemporaryDirectory() as tmp:
            source = Path(tmp) / "source.docx"
            write_docx(source, body, header)
            text, _, _ = extract_docx_text(source, 200_000)
            rows = rows_for(text)
            _, targets, retained = redaction_inputs(text, rows, REDACTED_REVIEW_STATUSES)
            exported = Path(tmp) / "exported.docx"
            exported.write_bytes(redact_docx(source, targets, retained=[span for span, _ in retained]))
            delivered, _, _ = extract_docx_text(exported, 200_000)
            return normalize_layout(plan_text(text, rows, REDACTED_REVIEW_STATUSES)), delivered

    def test_the_canvas_and_the_export_attribute_each_mention_alike(self):
        canvas, delivered = self.render(SHARED_SURNAME)
        self.assertEqual(canvas, delivered)
        self.assertIn("[PERSON_2] [COURT_2] itiraz", delivered)

    def test_an_anchor_lands_in_the_body_when_the_header_reads_first(self):
        """The header holds a mention too, so the body's occurrences start at index 1."""
        canvas, delivered = self.render(SHARED_SURNAME, header="Kartal Hukuk Bürosu dosya özeti")
        self.assertEqual(canvas, delivered)
        self.assertIn("[PERSON_2] [COURT_2] itiraz", delivered)


class ExportRefusalTests(unittest.TestCase):
    """The export refuses a file in which a retained finding keeps an approved item."""

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        tmp = Path(self.tmp.name)
        self.db_path = tmp / "offsets.db"
        originals = (app_module.DB_PATH, app_module.EXPORT_DIR)
        app_module.DB_PATH = self.db_path
        app_module.EXPORT_DIR = tmp / "exports"
        app_module.READY_DB_PATHS.discard(self.db_path.resolve())

        def restore():
            app_module.DB_PATH, app_module.EXPORT_DIR = originals
            app_module.READY_DB_PATHS.discard(self.db_path.resolve())

        self.addCleanup(restore)

        docx_path = tmp / "dilekce.docx"
        write_docx(docx_path, DIFFERENT_ROLE)
        text, _, _ = extract_docx_text(docx_path, 200_000)
        profile = analyze_privacy("dilekce.docx", text)
        conn = sqlite3.connect(self.db_path)
        conn.executescript(Path("db/schema.sql").read_text(encoding="utf-8"))
        conn.execute(
            "INSERT INTO users (username, password_hash, role) VALUES ('reviewer', ?, 'reviewer')",
            (generate_password_hash("secret"),),
        )
        conn.execute(
            """
            INSERT INTO documents (
                id, filename, filepath, file_extension, file_size, title, extraction_status, privacy_profile,
                residual_risk, review_status, ocr_status, redaction_completed, human_review_approved
            ) VALUES (1, 'dilekce.docx', ?, '.docx', ?, 'dilekce', 'Complete', ?, ?, 'approved', 'not_required', 1, 1)
            """,
            (str(docx_path), docx_path.stat().st_size, json.dumps(profile), profile["residual_risk"]["level"]),
        )
        for finding in profile["risk_map"]:
            status = "retained" if finding["sample"] == RETAINED_COURT else "approved"
            conn.execute(
                """
                INSERT INTO privacy_findings (
                    document_id, category, sample, risk, recommended_action, placeholder, review_status,
                    source, fingerprint, start_offset, end_offset
                ) VALUES (1, ?, ?, ?, ?, ?, ?, 'detector', ?, ?, ?)
                """,
                (
                    finding["category"],
                    finding["sample"],
                    finding["risk"],
                    finding["recommended_action"],
                    finding["placeholder"],
                    status,
                    f"fp{finding['start']}",
                    finding["start"],
                    finding["end"],
                ),
            )
        conn.commit()
        self.ids = {row[0]: row[1] for row in conn.execute("SELECT sample, id FROM privacy_findings")}
        conn.close()
        self.client = app_module.app.test_client()
        self.client.post("/login", json={"username": "reviewer", "password": "secret"})

    def test_the_export_refuses_and_names_both_decisions(self):
        response = self.client.get("/api/document/1/redacted-export?format=docx")
        self.assertEqual(response.status_code, 409)
        error = response.json["error"]
        self.assertIn("Two decisions disagree", error)
        self.assertIn(f"“Kartal” (finding #{self.ids['Kartal']})", error)
        self.assertIn(f"“{RETAINED_COURT}”, which you retained (finding #{self.ids[RETAINED_COURT]})", error)

        gate = self.client.get("/api/document/1").json["export_gate"]
        condition = next(c for c in gate["conditions"] if c["id"] == "no_approved_text_retained")
        self.assertFalse(condition["passed"])
        self.assertIn(RETAINED_COURT, condition["detail"])
        self.assertFalse(gate["allowed"])

    def test_the_refusal_audit_carries_ids_and_never_the_text(self):
        self.client.get("/api/document/1/redacted-export?format=docx")
        conn = sqlite3.connect(self.db_path)
        rows = conn.execute("SELECT metadata FROM audit_log WHERE action = 'export.redacted_docx'").fetchall()
        conn.close()
        self.assertTrue(rows)
        metadata = json.loads(rows[-1][0])
        self.assertEqual(metadata["held_count"], 1)
        self.assertEqual(metadata["retained_finding_ids"], [self.ids[RETAINED_COURT]])
        self.assertNotIn("Kartal", rows[-1][0])

    def test_approving_the_retained_finding_clears_the_export(self):
        self.client.post(f"/api/finding/{self.ids[RETAINED_COURT]}/review", json={"action": "approve"})
        self.client.post("/api/document/1/review", json={"action": "mark_redacted"})
        self.client.post("/api/document/1/review", json={"action": "approve"})
        response = self.client.get("/api/document/1/redacted-export?format=docx")
        self.assertEqual(response.status_code, 200, response.get_data()[:300])
        with TemporaryDirectory() as tmp:
            exported = Path(tmp) / "exported.docx"
            exported.write_bytes(response.get_data())
            delivered, _, _ = extract_docx_text(exported, 200_000)
        response.close()
        self.assertNotIn("Kartal", delivered)


class PdfOccurrenceLabelTests(unittest.TestCase):
    def build(self, lines: list[str]) -> tuple[Path, str]:
        path = Path(self.tmp.name) / "dilekce.pdf"
        with pymupdf.open() as doc:
            page = doc.new_page()
            for index, line in enumerate(lines):
                page.insert_text((72, 72 + 24 * index), line, fontsize=11)
            doc.save(path)
        text, _ = extract_text(path)
        return path, text

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_the_box_where_the_finding_was_found_is_told_from_its_repeat(self):
        path, text = self.build(["Davaci Ekin Kartal geldi.", "Tanik Ekin Kartal da geldi."])
        start = text.rindex("Ekin Kartal")
        finding = {"id": 7, "category": "natural_person_name", "sample": "Ekin Kartal", "source": "detector", "start_offset": start}
        regions = find_pdf_regions(path, [finding], text)
        self.assertEqual(len(regions), 2)
        by_line = sorted(regions, key=lambda region: region.y0)
        self.assertEqual([region.occurrence for region in by_line], ["repeat", "finding"])

    def test_boxes_carry_no_label_when_the_page_and_the_text_disagree(self):
        path, text = self.build(["Davaci Ekin Kartal geldi.", "Tanik Ekin Kartal da geldi."])
        finding = {"id": 7, "category": "natural_person_name", "sample": "Ekin Kartal", "source": "detector", "start_offset": text.index("Ekin Kartal")}
        regions = find_pdf_regions(path, [finding], text + "\nEkin Kartal")
        self.assertEqual(len(regions), 2)
        self.assertEqual({region.occurrence for region in regions}, {None})

    def test_a_case_variant_of_a_bare_surname_is_never_labelled_where_it_was_found(self):
        path, text = self.build(["Kartaltepe Mah. Lale Sok. No 4", "Davaci Kartal geldi.", "KARTAL"])
        finding = {"id": 7, "category": "natural_person_name", "sample": "Kartal", "source": "detector", "start_offset": text.index("Kartal ")}
        regions = find_pdf_regions(path, [finding], text)
        by_line = sorted(regions, key=lambda region: region.y0)
        self.assertEqual(len(by_line), 2)
        self.assertEqual([region.occurrence for region in by_line], [None, None])

    def test_boxes_carry_no_label_when_the_text_reads_the_lines_in_another_order(self):
        path, text = self.build(["Davaci Ekin Kartal geldi.", "Tanik Ekin Kartal da geldi."])
        first, second = [line for line in text.splitlines() if line.strip()]
        swapped = f"{second}\n{first}\n"
        finding = {"id": 7, "category": "natural_person_name", "sample": "Ekin Kartal", "source": "detector", "start_offset": swapped.index("Ekin Kartal")}
        regions = find_pdf_regions(path, [finding], swapped)
        self.assertEqual(len(regions), 2)
        self.assertEqual({region.occurrence for region in regions}, {None})


class PdfPageReadOnceTests(unittest.TestCase):
    """A name found N times is N findings of N hits each; labelling them must not
    read the page once per hit (N^2 full-page parses inside the request)."""

    def test_each_page_is_read_once_however_many_hits_it_holds(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "dilekce.pdf"
            with pymupdf.open() as doc:
                for lines in (["Davaci Ekin Kartal geldi.", "Tanik Ekin Kartal da geldi."], ["Vekil Ekin Kartal imzaladi."]):
                    page = doc.new_page()
                    for index, line in enumerate(lines):
                        page.insert_text((72, 72 + 24 * index), line, fontsize=11)
                doc.save(path)
            text, _ = extract_text(path)
            starts = [index for index in range(len(text)) if text.startswith("Ekin Kartal", index)]
            findings = [
                {"id": number, "category": "natural_person_name", "sample": "Ekin Kartal", "source": "detector", "start_offset": start}
                for number, start in enumerate(starts, start=1)
            ]
            reads: list[int] = []
            get_text = pymupdf.Page.get_text

            def counting_get_text(page, option="text", *args, **kwargs):
                if option == "rawdict":
                    reads.append(page.number)
                return get_text(page, option, *args, **kwargs)

            with patch.object(pymupdf.Page, "get_text", counting_get_text):
                regions = find_pdf_regions(path, findings, text)
        self.assertEqual(len(starts), 3)
        self.assertEqual(len(regions), 9)
        self.assertEqual(sorted(reads), [0, 1])
        # The cache is per page: each finding is labelled at its own box, on its own page.
        for finding in findings:
            own = [region for region in regions if region.finding_id == finding["id"] and region.occurrence == "finding"]
            self.assertEqual(len(own), 1)
            self.assertEqual(sum(1 for region in regions if region.finding_id == finding["id"] and region.occurrence == "repeat"), 2)
        self.assertEqual(
            sorted((region.page_number, round(region.y0, 6)) for region in regions if region.occurrence == "finding"),
            sorted({(region.page_number, round(region.y0, 6)) for region in regions}),
        )


class PdfOccurrenceApiTests(unittest.TestCase):
    """The label survives into the region list the studio reads."""

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        tmp = Path(self.tmp.name)
        self.db_path = tmp / "pdf.db"
        originals = (app_module.DB_PATH, app_module.EXPORT_DIR)
        app_module.DB_PATH = self.db_path
        app_module.EXPORT_DIR = tmp / "exports"
        app_module.READY_DB_PATHS.discard(self.db_path.resolve())

        def restore():
            app_module.DB_PATH, app_module.EXPORT_DIR = originals
            app_module.READY_DB_PATHS.discard(self.db_path.resolve())

        self.addCleanup(restore)
        pdf_path = tmp / "dilekce.pdf"
        with pymupdf.open() as doc:
            page = doc.new_page()
            page.insert_text((72, 72), "Davaci Ekin Kartal geldi.", fontsize=11)
            page.insert_text((72, 96), "Tanik Ekin Kartal da geldi.", fontsize=11)
            doc.save(pdf_path)
        text, _ = extract_text(pdf_path)
        conn = sqlite3.connect(self.db_path)
        conn.executescript(Path("db/schema.sql").read_text(encoding="utf-8"))
        conn.execute(
            "INSERT INTO users (username, password_hash, role) VALUES ('reviewer', ?, 'reviewer')",
            (generate_password_hash("secret"),),
        )
        conn.execute(
            """
            INSERT INTO documents (id, filename, filepath, file_extension, file_size, title, extraction_status, ocr_status)
            VALUES (1, 'dilekce.pdf', ?, '.pdf', ?, 'dilekce', 'Complete', 'not_required')
            """,
            (str(pdf_path), pdf_path.stat().st_size),
        )
        start = text.index("Ekin Kartal")
        conn.execute(
            """
            INSERT INTO privacy_findings (
                document_id, category, sample, risk, recommended_action, placeholder, review_status, source,
                fingerprint, start_offset, end_offset
            ) VALUES (1, 'natural_person_name', 'Ekin Kartal', 'HIGH', 'Replace with consistent pseudonym', '[PERSON_1]',
                      'approved', 'detector', 'fp1', ?, ?)
            """,
            (start, start + len("Ekin Kartal")),
        )
        conn.commit()
        conn.close()
        self.client = app_module.app.test_client()
        self.client.post("/login", json={"username": "reviewer", "password": "secret"})

    def test_generated_boxes_are_listed_with_their_occurrence(self):
        generated = self.client.post("/api/document/1/pdf/regions/generate", json={})
        self.assertEqual(generated.status_code, 200, generated.get_data()[:300])
        regions = sorted(self.client.get("/api/document/1/pdf/regions").json["regions"], key=lambda r: r["rect"]["y0"])
        self.assertEqual([region["occurrence"] for region in regions], ["finding", "repeat"])


if __name__ == "__main__":
    unittest.main()
