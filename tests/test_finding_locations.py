"""Where each finding sits in a DOCX, for the Word add-in's "Show in document".

The panel searches one story of the open document and selects the ``index``-th
of ``count`` hits. These tests pin that the story and the occurrence come from
the export's own anchoring (``docx_redactor.anchor_parts``), so the panel lands
on the occurrence the export redacts as that finding's own -- including for a
text repeated within one story, where "select the first hit" lands on the wrong
place -- and that a finding that cannot be placed says so instead of guessing.

Every name, number and place here is synthetic.
"""

import json
import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from xml.sax.saxutils import escape
from zipfile import ZIP_DEFLATED, ZipFile

from werkzeug.security import generate_password_hash

import app as app_module
import tests.env_setup  # noqa: F401  -- sets LEGAL_ANALYZER_SECRET_KEY before app is imported
from legal_analyzer.docx_locations import finding_locations, searchable_in_word
from legal_analyzer.docx_redactor import (
    Occurrence,
    RedactionTarget,
    anchor_parts,
    part_streams,
)
from legal_analyzer.extraction import ZIP_MEMBER_READ_LIMIT, extract_docx_text
from legal_analyzer.privacy import analyze_privacy

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG = "http://schemas.openxmlformats.org/package/2006/relationships"
DECL = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'

CONTENT_TYPES = f"""{DECL}
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
</Types>"""
ROOT_RELS = f"""{DECL}
<Relationships xmlns="{PKG}">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>"""
DOCUMENT_RELS = f"""{DECL}
<Relationships xmlns="{PKG}">
  <Relationship Id="rId10" Type="{R}/header" Target="header1.xml"/>
  <Relationship Id="rId11" Type="{R}/footer" Target="/word/footer1.xml"/>
  <Relationship Id="rId12" Type="{R}/header" Target="header2.xml"/>
  <Relationship Id="rId13" Type="{R}/hyperlink" Target="https://example.com/" TargetMode="External"/>
</Relationships>"""


def paragraphs(lines: list[str]) -> str:
    return "".join(f'<w:p><w:r><w:t xml:space="preserve">{escape(line)}</w:t></w:r></w:p>' for line in lines)


def part(root: str, lines: list[str]) -> str:
    return f'{DECL}<w:{root} xmlns:w="{W}" xmlns:r="{R}">{paragraphs(lines)}</w:{root}>'


def notes(root: str, child: str, texts: list[str]) -> str:
    body = "".join(f'<w:{child} w:id="{index + 1}">{paragraphs([text])}</w:{child}>' for index, text in enumerate(texts))
    return f'{DECL}<w:{root} xmlns:w="{W}">{body}</w:{root}>'


# Two sections. Section 0 ends in a paragraph's sectPr and shows header1 as its
# primary header; section 1 is the body's own sectPr, showing footer1 on its
# first page and header1 again. header2 is related but no section shows it.
BODY_SECTION_0 = ["Davacı Ekin Kartal dava açmıştır.", "Kartal, Davacı sıfatıyla imzalamıştır."]
BODY_SECTION_1 = ["Davacı vekili Deniz Kurgusal beyanda bulunmuştur."]
HEADER_1 = ["Kurgu Hukuk Bürosu - Av. Deniz Kurgusal"]
HEADER_2 = ["Arşiv kopyası: Ekin Kartal"]
FOOTER_1 = ["Esas No: 2024/1187"]
FOOTNOTES = ["Vekaletname Ekin Kartal adına düzenlenmiştir."]
ENDNOTES = ["Dosya Masalkent 3. Asliye Hukuk Mahkemesine gönderilmiştir."]
COMMENTS = ["Deniz Kurgusal ile görüşülecek."]


def document_xml() -> str:
    section_0 = (
        f'<w:p><w:pPr><w:sectPr><w:headerReference w:type="default" r:id="rId10"/></w:sectPr></w:pPr>'
        f'<w:r><w:t>{escape(BODY_SECTION_0[1])}</w:t></w:r></w:p>'
    )
    section_1 = (
        '<w:sectPr><w:headerReference w:type="default" r:id="rId10"/>'
        '<w:footerReference w:type="first" r:id="rId11"/></w:sectPr>'
    )
    body = paragraphs(BODY_SECTION_0[:1]) + section_0 + paragraphs(BODY_SECTION_1) + section_1
    return f'{DECL}<w:document xmlns:w="{W}" xmlns:r="{R}"><w:body>{body}</w:body></w:document>'


def write_multi_story(path: Path) -> None:
    with ZipFile(path, "w", ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", CONTENT_TYPES)
        archive.writestr("_rels/.rels", ROOT_RELS)
        archive.writestr("word/_rels/document.xml.rels", DOCUMENT_RELS)
        archive.writestr("word/document.xml", document_xml())
        archive.writestr("word/header1.xml", part("hdr", HEADER_1))
        archive.writestr("word/header2.xml", part("hdr", HEADER_2))
        archive.writestr("word/footer1.xml", part("ftr", FOOTER_1))
        archive.writestr("word/footnotes.xml", notes("footnotes", "footnote", FOOTNOTES))
        archive.writestr("word/endnotes.xml", notes("endnotes", "endnote", ENDNOTES))
        archive.writestr("word/comments.xml", notes("comments", "comment", COMMENTS))


def rows_at(text: str, samples: list[str]) -> list[dict]:
    """A finding row at every exact occurrence of each sample in ``text``."""
    rows = []
    for sample in samples:
        start = text.find(sample)
        while start != -1:
            rows.append({"id": len(rows) + 1, "sample": sample, "start_offset": start, "end_offset": start + len(sample)})
            start = text.find(sample, start + 1)
    return rows


def kth_hit(story_text: str, sample: str, index: int) -> int:
    start = -1
    for _ in range(index + 1):
        start = story_text.find(sample, start + 1)
    return start


class MultiStoryLocationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "dilekce.docx"
        write_multi_story(self.path)
        self.text, warning, _ = extract_docx_text(self.path, 200_000)
        self.assertIsNone(warning)
        self.rows = rows_at(self.text, ["Deniz Kurgusal", "Ekin Kartal", "Kartal", "Davacı", "Esas No: 2024/1187"])
        self.by_id = {location["finding_id"]: location for location in finding_locations(self.path, self.text, self.rows)}

    def located(self, sample: str) -> list[dict]:
        return [self.by_id[row["id"]] for row in self.rows if row["sample"] == sample]

    def test_each_story_is_named_with_its_section_and_type(self):
        stories = {(loc["story"], loc["section"], loc["header_footer_type"]) for loc in self.located("Deniz Kurgusal")}
        self.assertEqual(
            stories,
            {("header", 0, "Primary"), ("body", None, None), ("comments", None, None)},
        )
        (footer,) = self.located("Esas No: 2024/1187")
        self.assertEqual((footer["story"], footer["section"], footer["header_footer_type"]), ("footer", 1, "FirstPage"))
        notes_and_headers = {loc["story"] for loc in self.located("Ekin Kartal")}
        self.assertEqual(notes_and_headers, {"header", "body", "footnotes"})

    def test_a_text_repeated_in_one_story_gets_a_distinct_occurrence_each(self):
        """`Davacı` twice and the bare `Kartal` twice in the body: first-hit selection would be wrong."""
        body = [loc for loc in self.located("Davacı") if loc["story"] == "body"]
        self.assertEqual(sorted((loc["index"], loc["count"]) for loc in body), [(0, 3), (1, 3), (2, 3)])
        bare = [loc for loc in self.located("Kartal") if loc["story"] == "body"]
        self.assertEqual({loc["count"] for loc in bare}, {2})
        self.assertEqual(sorted(loc["index"] for loc in bare), [0, 1])

    def test_occurrences_follow_reading_order_within_a_story(self):
        """The k-th finding of a text in a story, by position, is the k-th hit -- not just some hit."""
        for sample in ("Davacı", "Kartal", "Deniz Kurgusal", "Ekin Kartal"):
            rows = sorted((row for row in self.rows if row["sample"] == sample), key=lambda row: row["start_offset"])
            by_part: dict[str, list[int]] = {}
            for row in rows:
                location = self.by_id[row["id"]]
                by_part.setdefault(location["part"], []).append(location["index"])
            for part_name, indexes in by_part.items():
                self.assertEqual(indexes, list(range(len(indexes))), (sample, part_name))

    def test_the_kth_hit_in_the_named_story_is_the_finding(self):
        with ZipFile(self.path) as archive:
            streams, _ = part_streams(archive)
        story_text = {name: text for name, _, text in streams}
        for row in self.rows:
            location = self.by_id[row["id"]]
            self.assertIsNotNone(location["story"], row)
            hit = kth_hit(story_text[location["part"]], row["sample"], location["index"])
            self.assertNotEqual(hit, -1, row)
            self.assertEqual(story_text[location["part"]].count(row["sample"]), location["count"])

    def test_the_placement_is_the_one_the_export_anchors(self):
        """Same function, same answer: the part and local occurrence anchor_parts hands the redactor."""
        targets = [
            RedactionTarget(row["sample"], "[X]", False, Occurrence.at(self.text, row["sample"], row["start_offset"]))
            for row in self.rows
        ]
        with ZipFile(self.path) as archive:
            anchored = anchor_parts(archive, targets)
        for position, row in enumerate(self.rows):
            homes = [
                (name, part_targets[position].found_at)
                for name, (part_targets, _) in anchored.items()
                if part_targets[position].found_at is not None
            ]
            location = self.by_id[row["id"]]
            self.assertEqual(homes, [(location["part"], Occurrence(location["index"], location["count"]))], row)

    def test_a_header_no_section_shows_is_not_searchable(self):
        (orphan,) = [loc for loc in self.located("Ekin Kartal") if loc["part"] == "word/header2.xml"]
        self.assertEqual(orphan["story"], "header")
        self.assertIsNone(orphan["section"])
        self.assertFalse(orphan["searchable"])

    def test_every_reachable_story_is_searchable(self):
        for location in self.by_id.values():
            if location["part"] not in ("word/header2.xml", "word/comments.xml"):
                self.assertTrue(location["searchable"], location)
                self.assertIsNone(location["comment_id"], location)

    def test_a_finding_in_a_comment_names_the_comment_and_is_not_searchable(self):
        (comment,) = [loc for loc in self.located("Deniz Kurgusal") if loc["story"] == "comments"]
        self.assertFalse(comment["searchable"])
        self.assertEqual((comment["comment_id"], comment["index"], comment["count"]), (1, 0, 1))

    def test_a_finding_whose_offset_no_longer_frames_it_is_not_placed(self):
        stale = {"id": 99, "sample": "Ekin Kartal", "start_offset": 0, "end_offset": 11}
        reviewer_added = {"id": 100, "sample": "Masalkent", "start_offset": None, "end_offset": None}
        locations = finding_locations(self.path, self.text, [stale, reviewer_added])
        for location in locations:
            self.assertIsNone(location["story"])
            self.assertIsNone(location["index"])
            self.assertIsNone(location["count"])
            self.assertTrue(location["searchable"])

    def test_an_unreadable_file_places_nothing(self):
        broken = Path(self.tmp.name) / "broken.docx"
        broken.write_bytes(b"not a zip")
        (location,) = finding_locations(broken, self.text, self.rows[:1])
        self.assertIsNone(location["story"])


class CommentLocationTests(unittest.TestCase):
    def test_the_occurrence_is_counted_within_its_own_comment(self):
        """Three hits across two comments: each is placed by comment id and its index in that comment."""
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "yorumlu.docx"
            with ZipFile(path, "w", ZIP_DEFLATED) as archive:
                archive.writestr("[Content_Types].xml", CONTENT_TYPES)
                archive.writestr("_rels/.rels", ROOT_RELS)
                archive.writestr("word/document.xml", part("document", ["Dilekçe metni."]))
                comments = (
                    f'{DECL}<w:comments xmlns:w="{W}">'
                    f'<w:comment w:id="7">{paragraphs(["Ekin Kartal aranacak."])}</w:comment>'
                    f'<w:comment w:id="12">{paragraphs(["Ekin Kartal ve yine Ekin Kartal."])}</w:comment>'
                    "</w:comments>"
                )
                archive.writestr("word/comments.xml", comments)
            text, _, _ = extract_docx_text(path, 200_000)
            rows = rows_at(text, ["Ekin Kartal"])
            located = [
                (loc["story"], loc["comment_id"], loc["index"], loc["count"], loc["searchable"])
                for loc in finding_locations(path, text, rows)
            ]
        self.assertEqual(
            located,
            [("comments", 7, 0, 1, False), ("comments", 12, 0, 2, False), ("comments", 12, 1, 2, False)],
        )


class TrackedSectionChangeTests(unittest.TestCase):
    def test_a_tracked_section_change_is_not_counted_as_a_section(self):
        """Word keeps a tracked layout change's old properties in a w:sectPr inside w:sectPrChange."""
        old_properties = (
            '<w:sectPrChange w:id="1" w:author="Kurgu"><w:sectPr>'
            '<w:headerReference w:type="default" r:id="rId12"/></w:sectPr></w:sectPrChange>'
        )
        body = (
            f'<w:p><w:pPr><w:sectPr><w:headerReference w:type="default" r:id="rId10"/>{old_properties}'
            f"</w:sectPr></w:pPr><w:r><w:t>{escape(BODY_SECTION_0[0])}</w:t></w:r></w:p>"
            + paragraphs(BODY_SECTION_1)
            + '<w:sectPr><w:footerReference w:type="first" r:id="rId11"/></w:sectPr>'
        )
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "izlenen.docx"
            write_multi_story(path)
            with ZipFile(path) as source:
                members = {name: source.read(name) for name in source.namelist()}
            members["word/document.xml"] = (
                f'{DECL}<w:document xmlns:w="{W}" xmlns:r="{R}"><w:body>{body}</w:body></w:document>'
            ).encode()
            with ZipFile(path, "w", ZIP_DEFLATED) as archive:
                for name, data in members.items():
                    archive.writestr(name, data)
            text, _, _ = extract_docx_text(path, 200_000)
            rows = rows_at(text, ["Esas No: 2024/1187", "Arşiv kopyası"])
            footer, orphan = finding_locations(path, text, rows)
        self.assertEqual((footer["story"], footer["section"], footer["header_footer_type"]), ("footer", 1, "FirstPage"))
        self.assertEqual((orphan["part"], orphan["section"], orphan["searchable"]), ("word/header2.xml", None, False))


class OversizedMemberTests(unittest.TestCase):
    def test_a_part_past_the_intake_cap_places_nothing(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "buyuk.docx"
            write_multi_story(path)
            text, _, _ = extract_docx_text(path, 200_000)
            rows = rows_at(text, ["Deniz Kurgusal"])
            with ZipFile(path, "a", ZIP_DEFLATED) as archive:
                archive.writestr("customXml/item1.xml", b"<a>" + b" " * ZIP_MEMBER_READ_LIMIT + b"</a>")
            locations = finding_locations(path, text, rows)
        self.assertTrue(locations)
        for location in locations:
            self.assertIsNone(location["story"])
            self.assertIsNone(location["index"])
            self.assertTrue(location["searchable"])


class SearchabilityTests(unittest.TestCase):
    def test_what_word_search_cannot_take_is_flagged(self):
        self.assertTrue(searchable_in_word("Deniz Kurgusal"))
        self.assertFalse(searchable_in_word(""))
        self.assertFalse(searchable_in_word("a" * 256))
        self.assertTrue(searchable_in_word("a" * 255))
        for text in ("Ad\tSoyad", "Ad\nSoyad", "Ad^pSoyad"):
            self.assertFalse(searchable_in_word(text), text)


class BundledSampleTests(unittest.TestCase):
    def test_every_detected_finding_in_each_docx_sample_is_placed(self):
        for sample in sorted(Path("samples/documents").glob("*.docx")):
            with self.subTest(sample=sample.name):
                text, _, _ = extract_docx_text(sample, 200_000)
                risk_map = analyze_privacy(sample.name, text)["risk_map"]
                rows = [
                    {"id": index, "sample": f["sample"], "start_offset": f["start"], "end_offset": f["end"]}
                    for index, f in enumerate(risk_map)
                ]
                self.assertTrue(rows)
                locations = finding_locations(sample, text, rows)
                for row, location in zip(rows, locations):
                    self.assertEqual(location["story"], "body", row)
                    self.assertLess(location["index"], location["count"], row)
                # The repeated texts get distinct occurrences, never two rows on one hit.
                hits = [(loc["part"], row["sample"], loc["index"]) for row, loc in zip(rows, locations)]
                self.assertEqual(len(hits), len(set(hits)))


class FindingLocationsApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        tmp = Path(self.tmp.name)
        self.db_path = tmp / "locations.db"
        original = app_module.DB_PATH
        app_module.DB_PATH = self.db_path
        app_module.READY_DB_PATHS.discard(self.db_path.resolve())

        def restore():
            app_module.DB_PATH = original
            app_module.READY_DB_PATHS.discard(self.db_path.resolve())

        self.addCleanup(restore)

        docx_path = tmp / "dilekce.docx"
        write_multi_story(docx_path)
        text, _, _ = extract_docx_text(docx_path, 200_000)
        pdf_path = tmp / "makbuz.pdf"
        pdf_path.write_bytes(b"%PDF-1.4\n")
        conn = sqlite3.connect(self.db_path)
        conn.executescript(Path("db/schema.sql").read_text(encoding="utf-8"))
        for username, role in (("reviewer", "reviewer"), ("viewer", "viewer")):
            conn.execute(
                "INSERT INTO users (username, password_hash, role) VALUES (?, ?, ?)",
                (username, generate_password_hash("secret"), role),
            )
        for doc_id, name, path, extension in ((1, "dilekce.docx", docx_path, ".docx"), (2, "makbuz.pdf", pdf_path, ".pdf")):
            conn.execute(
                """
                INSERT INTO documents (id, filename, filepath, file_extension, file_size, title, extraction_status,
                                       privacy_profile, review_status, ocr_status)
                VALUES (?, ?, ?, ?, ?, ?, 'Complete', ?, 'pending_review', 'not_required')
                """,
                (doc_id, name, str(path), extension, path.stat().st_size, name, json.dumps({})),
            )
        self.rows = rows_at(text, ["Deniz Kurgusal", "Davacı"])
        for row in self.rows:
            conn.execute(
                """
                INSERT INTO privacy_findings (id, document_id, category, sample, risk, recommended_action, placeholder,
                                              review_status, source, fingerprint, start_offset, end_offset)
                VALUES (?, 1, 'natural_person_name', ?, 'HIGH', 'Redact', '[PERSON_1]', 'pending', 'detector', ?, ?, ?)
                """,
                (row["id"], row["sample"], f"fp{row['id']}", row["start_offset"], row["end_offset"]),
            )
        conn.commit()
        conn.close()
        self.client = app_module.app.test_client()

    def login(self, username: str) -> None:
        self.client.post("/login", json={"username": username, "password": "secret"})

    def test_it_returns_every_finding_placed_and_no_text(self):
        self.login("reviewer")
        response = self.client.get("/api/document/1/finding-locations")
        self.assertEqual(response.status_code, 200)
        payload = response.json
        self.assertEqual(payload["source"], "extraction")
        self.assertEqual(sorted(loc["finding_id"] for loc in payload["locations"]), [row["id"] for row in self.rows])
        self.assertTrue(all(loc["story"] for loc in payload["locations"]))
        body = response.get_data(as_text=True)
        for text in ("Kurgusal", "Davac", "Kartal"):
            self.assertNotIn(text, body)

    def test_a_viewer_may_navigate(self):
        self.login("viewer")
        self.assertEqual(self.client.get("/api/document/1/finding-locations").status_code, 200)

    def test_anonymous_is_refused(self):
        self.assertEqual(self.client.get("/api/document/1/finding-locations").status_code, 401)

    def test_a_pdf_is_refused_and_a_missing_document_is_404(self):
        self.login("reviewer")
        self.assertEqual(self.client.get("/api/document/2/finding-locations").status_code, 415)
        self.assertEqual(self.client.get("/api/document/99/finding-locations").status_code, 404)

    def test_a_missing_source_file_is_404(self):
        self.login("reviewer")
        conn = sqlite3.connect(self.db_path)
        conn.execute("UPDATE documents SET filepath = ? WHERE id = 1", (str(Path(self.tmp.name) / "gone.docx"),))
        conn.commit()
        conn.close()
        self.assertEqual(self.client.get("/api/document/1/finding-locations").status_code, 404)


if __name__ == "__main__":
    unittest.main()
