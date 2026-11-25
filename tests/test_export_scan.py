"""A reviewed export never leaves the machine with an approved item still in it.

After every finding was approved and the document signed off, the reviewed DOCX
still carried the client's email in its hyperlink relationship and the name in a
tracked deletion, a field code, an image description, a tooltip, a bookmark and
a document variable; the reviewed PDF kept its outline title, an attachment, a
``mailto:`` link and a page-label prefix. Export QA passed every one, because
both QA passes looked only where the redactor edits -- and the download never ran
QA at all.

What these tests pin:

1. ``export_scan`` reads the OUTPUT bytes everywhere text can be, not where the
   redactor writes (``ScanBreadthTests``, ``PdfScanBreadthTests``), and a leak it
   reports never carries the sample.
2. The build path every download and the matter archive go through refuses a
   file the scan finds an approved item in, with an audited 409 that names parts
   and counts only, and records no artifact (``RefusedDownloadTests``,
   ``MatterArchiveTests``, ``RefusalAuditTests``).
3. Both QA endpoints run the same scan, and one rule decides the
   ``redacted_verified`` stamp: a clean pass, nothing else (``QaVerificationTests``).
4. The two vectors ordinary Word documents hit -- the hyperlink relationship and
   tracked deletions / field codes -- are redacted, so those files still export
   (``OrdinaryWordVectorsTests``). So are the rest the scan first refused: image
   descriptions, tooltips, bookmarks, content controls, document variables and
   non-standard document properties in a DOCX; outline titles, attachments,
   links and page labels in a PDF (``RedactedVectorExportTests``).
5. Every bundled sample still exports after approve-all and sign-off: a refusal
   there would be a false hit (``BundledSamplesStillExportTests``). So does a
   file carrying the parts Word writes beside the body -- people.xml, a
   bibliography customXml item, a SharePoint content-type schema item, a
   glossary -- which intake now reads, so what they hold is detected, approved
   and gone from the export (``WordAuthoredPartsExportTests``). So does the
   snapshot a Word add-in sends with the add-in's own word/webextensions/ parts
   in it (``WordAddInPartsExportTests``).

Every name, address and identifier here is synthetic; email domains are RFC
2606's example.com.
"""

from __future__ import annotations

import json
import sqlite3
import time
import unittest
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock
from xml.sax.saxutils import escape
from zipfile import ZIP_DEFLATED, ZipFile

import pymupdf
from defusedxml import ElementTree
from werkzeug.security import generate_password_hash

import tests.env_setup  # noqa: F401  -- sets LEGAL_ANALYZER_SECRET_KEY before app is imported
import app as app_module
from blueprints.export import verification_status
from legal_analyzer.classifier import TURKISH
from legal_analyzer.docx_redactor import (
    Occurrence,
    RedactionTarget,
    SampleMatcher,
    anchor_parts,
    redact_docx,
    scrub_external_targets,
)
from legal_analyzer.export_scan import scan_docx, scan_pdf
from legal_analyzer.extraction import extract_text
from legal_analyzer.pdf_redactor import PDF_REDACTION_MARK
from legal_analyzer.privacy import analyze_privacy
from legal_analyzer.taxonomy import CATEGORIES, SUBCATEGORIES

PROJECT_DIR = Path(__file__).resolve().parent.parent
SAMPLES_DIR = PROJECT_DIR / "samples" / "documents"
W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
WP = "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
HYPERLINK_TYPE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink"
NAME = "Deniz Kurgusal"
EMAIL = "deniz.kurgusal@example.com"
BODY = (
    "İşbu dilekçe davacı vekili Av. Deniz Kurgusal tarafından hazırlanmıştır. Davacı ile davalı "
    "arasındaki uyuşmazlık hakkında mahkemeye sunulan bu belge ilgili kanun hükümlerine göre "
    "düzenlenmiştir ve taraflar arasında imzalanan sözleşme hükümleri geçerlidir. İletişim için "
    "deniz.kurgusal@example.com adresine yazılabilir."
)
SECRETS = ("kurgusal",)
# Short given names that occur by chance inside the standard attribute values of every Word package.
SHORT_NAMES = ("Ali", "Ela", "Ata", "Ece", "Efe", "Er", "Mo", "Nat", "Al", "Nil")
# Common short Turkish and English given names, each a reviewer-added finding in the residual check.
COMMON_SHORT_NAMES = (
    'Ali', 'Ela', 'Ata', 'Ece', 'Efe', 'Tan', 'Er', 'Dal', 'Nail', 'Erin', 'Can', 'Cem',
    'Eda', 'Ege', 'Emre', 'Eren', 'Ferit', 'Gül', 'Naz', 'Nur', 'Oya', 'Ozan', 'Tuna', 'Ada',
    'Arda', 'Aras', 'Ayla', 'Aylin', 'Ayşe', 'Banu', 'Barış', 'Baran', 'Bora', 'Burak', 'Cansu', 'Defne',
    'Demet', 'Deniz', 'Derya', 'Dila', 'Doğa', 'Duru', 'Ebru', 'Elif', 'Emel', 'Emin', 'Esin', 'Esma',
    'Eylül', 'Fatma', 'Filiz', 'Gizem', 'Hakan', 'Hande', 'Hülya', 'İpek', 'İrem', 'Kaan', 'Kader', 'Kerem',
    'Lale', 'Leyla', 'Mert', 'Mine', 'Murat', 'Nalan', 'Nehir', 'Neşe', 'Nil', 'Oğuz', 'Okan', 'Onur',
    'Ömer', 'Özge', 'Pelin', 'Rana', 'Reha', 'Rüya', 'Sami', 'Seda', 'Selin', 'Sema', 'Serap', 'Sevgi',
    'Sinan', 'Su', 'Şule', 'Taner', 'Tolga', 'Tuba', 'Uğur', 'Umut', 'Yağmur', 'Yasin', 'Zehra', 'Zeki',
    'Ziya', 'Al', 'Ann', 'Amy', 'Ben', 'Bob', 'Dan', 'Don', 'Ed', 'Eli', 'Eve', 'Ian',
    'Ida', 'Jay', 'Jen', 'Jim', 'Joe', 'Jon', 'Kai', 'Kim', 'Lee', 'Leo', 'Liz', 'Max',
    'Meg', 'Mia', 'Ned', 'Pam', 'Pat', 'Ray', 'Rex', 'Roy', 'Sam', 'Sue', 'Tim', 'Tom',
    'Val', 'Zoe', 'Abe', 'Amos', 'Anna', 'Bea', 'Cal', 'Dee', 'Del', 'Dot', 'Eva', 'Fay',
    'Gus', 'Hal', 'Ivy', 'Jan', 'Kit', 'Lou', 'Mae', 'Mo', 'Nat', 'Noel', 'Ora', 'Pia',
    'Rod', 'Ron', 'Rose', 'Ted', 'Uma', 'Vic', 'Wes', 'Will', 'Mark', 'Hope', 'Grace', 'Faith',
    'Dawn', 'Hunter', 'Lane', 'Stone', 'Wade', 'Ward', 'Gray', 'Rich', 'Bill', 'Jack', 'Fern', 'Glen',
    'Rob', 'Art',
)
# Names Word also writes as whole attribute values: the stock theme's script
# fonts, "Times New Roman", built-in table styles ("Light Grid"), and the
# enumerations auto/single/double/center/left/right. Many are common Indian
# given names.
SYSTEM_VALUE_NAMES = (
    "Shruti", "Mangal", "Latha", "Vrinda", "Gautami", "Kartika", "Kalinga", "Raavi", "Tunga", "Nyala", "Roman", "Light",
    "Auto", "Single", "Double", "Center", "Left", "Right",
)
# Capitalised components of the bundled samples' camelCase values that are also names.
COMPONENT_NAMES = ("Boran", "Champa", "Asia", "Euphemia", "Angsana", "Penh")


def finding(sample: str, finding_id: int = 1, category: str = "natural_person_name", **extra) -> dict:
    return {"id": finding_id, "category": category, "sample": sample, "source": "detector", **extra}


APPROVED = [finding(NAME, 1), finding(EMAIL, 2, "email_address")]


def docx_bytes(
    extra_body: str = "",
    parts: dict[str, str | bytes] | None = None,
    relationships: str = "",
    body: str = BODY,
    package_relationships: str = "",
    content_types: str = "",
) -> bytes:
    """A real DOCX package: ``body``, then ``extra_body`` paragraphs, plus any extra parts.

    ``package_relationships`` are added to ``_rels/.rels`` and ``content_types``
    (``<Override>`` entries) to ``[Content_Types].xml``.
    """
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
            f"{content_types}</Types>",
        )
        archive.writestr(
            "_rels/.rels",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
            f'Target="word/document.xml"/>{package_relationships}</Relationships>',
        )
        if relationships:
            archive.writestr(
                "word/_rels/document.xml.rels",
                '<?xml version="1.0" encoding="UTF-8"?>'
                f'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">{relationships}'
                "</Relationships>",
            )
        archive.writestr(
            "word/document.xml",
            f'<?xml version="1.0" encoding="UTF-8"?><w:document xmlns:w="{W}" xmlns:r="{R}" xmlns:wp="{WP}">'
            f'<w:body><w:p><w:r><w:t xml:space="preserve">{escape(body)}</w:t></w:r></w:p>{extra_body}'
            "</w:body></w:document>",
        )
        for name, data in (parts or {}).items():
            archive.writestr(name, data)
    return buffer.getvalue()


MAILTO_RELATIONSHIP = (
    f'<Relationship Id="rId9" Type="{HYPERLINK_TYPE}" Target="mailto:{EMAIL}" TargetMode="External"/>'
)
HYPERLINK_PARAGRAPH = f'<w:p><w:hyperlink r:id="rId9"><w:r><w:t>{EMAIL}</w:t></w:r></w:hyperlink></w:p>'
TRACKED_DELETION = (
    '<w:p><w:del w:id="1" w:author="Reviewer" w:date="2000-01-01T00:00:00Z">'
    f"<w:r><w:delText>Eski vekil {NAME} idi.</w:delText></w:r></w:del></w:p>"
)
FIELD_CODE = (
    '<w:p><w:r><w:fldChar w:fldCharType="begin"/></w:r>'
    f'<w:r><w:instrText xml:space="preserve"> HYPERLINK "mailto:{EMAIL}" </w:instrText></w:r>'
    '<w:r><w:fldChar w:fldCharType="separate"/></w:r><w:r><w:t>e-posta</w:t></w:r>'
    '<w:r><w:fldChar w:fldCharType="end"/></w:r></w:p>'
)
# Vectors outside the visible text the redactor rewrites (RedactedVectorExportTests).
DOC_VARIABLE = {
    "word/settings.xml": f'<?xml version="1.0" encoding="UTF-8"?><w:settings xmlns:w="{W}">'
    f'<w:docVars><w:docVar w:name="Muvekkil" w:val="{NAME}"/></w:docVars></w:settings>'
}
# Word writes an image's alt text on wp:docPr and mirrors it onto the picture's own pic:cNvPr.
IMAGE_DESCRIPTION = (
    '<w:p><w:r><w:drawing><wp:inline>'
    f'<wp:docPr id="1" name="Resim 1" title="{NAME}" descr="{NAME} imza örneği"/>'
    '<a:graphic xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"><a:graphicData>'
    '<pic:pic xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture"><pic:nvPicPr>'
    f'<pic:cNvPr id="0" name="{NAME}.png" descr="{NAME} imza örneği"/></pic:nvPicPr></pic:pic>'
    "</a:graphicData></a:graphic></wp:inline></w:drawing></w:r></w:p>"
)
BOOKMARK = (
    f'<w:p><w:bookmarkStart w:id="0" w:name="{NAME.replace(" ", "_")}"/>'
    '<w:r><w:t>Ek</w:t></w:r><w:bookmarkEnd w:id="0"/></w:p>'
)
# What points at that bookmark: a cross-reference field and an internal hyperlink.
BOOKMARK_REFERENCES = (
    '<w:p><w:r><w:fldChar w:fldCharType="begin"/></w:r>'
    f'<w:r><w:instrText xml:space="preserve"> REF {NAME.replace(" ", "_")} \\h </w:instrText></w:r>'
    '<w:r><w:fldChar w:fldCharType="separate"/></w:r><w:r><w:t>Ek</w:t></w:r><w:r><w:fldChar w:fldCharType="end"/></w:r></w:p>'
    f'<w:p><w:hyperlink w:anchor="{NAME.replace(" ", "_")}"><w:r><w:t>Eke git</w:t></w:r></w:hyperlink></w:p>'
)
TOOLTIP = f'<w:p><w:hyperlink w:anchor="ek" w:tooltip="mailto:{EMAIL}"><w:r><w:t>Ek</w:t></w:r></w:hyperlink></w:p>'
CONTENT_CONTROL = (
    f'<w:sdt><w:sdtPr><w:alias w:val="{NAME} imzası"/><w:tag w:val="{NAME.replace(" ", "")}"/></w:sdtPr>'
    "<w:sdtContent><w:p><w:r><w:t>İmza</w:t></w:r></w:p></w:sdtContent></w:sdt>"
)
OTHER_PROPERTIES = {
    "docProps/core2.xml": f'<cp:coreProperties xmlns:cp="urn:example:{NAME.replace(" ", "%20")}"><c>{NAME}</c></cp:coreProperties>',
    "docProps/notes.txt": f"Müvekkil: {NAME}",
}
# What the redactor still does not rewrite, so the scan still refuses it: a
# style named after the client, and a legacy VML shape's alt text.
STYLE_NAME = {
    "word/styles.xml": f'<?xml version="1.0" encoding="UTF-8"?><w:styles xmlns:w="{W}">'
    f'<w:style w:type="paragraph" w:styleId="Muvekkil"><w:name w:val="{NAME}"/></w:style></w:styles>'
}
VML_ALT_TEXT = f'<w:p><w:r><w:pict><v:shape xmlns:v="urn:schemas-microsoft-com:vml" alt="{NAME} imzası"/></w:pict></w:r></w:p>'


def pdf_bytes(vector: str = "") -> bytes:
    """A text PDF carrying BODY, plus one place outside the page text that holds an approved item."""
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_textbox(pymupdf.Rect(72, 72, 520, 400), BODY, fontsize=11, fontname="helv")
    if vector == "outline":
        doc.set_toc([[1, f"{NAME} - Dilekçe", 1]])
    elif vector == "attachment":
        # Named after the client, as attachments often are: no report may echo the name.
        doc.embfile_add(f"{NAME}.txt", f"Muvekkil: {NAME}".encode(), filename=f"{NAME}.txt")
    elif vector == "link":
        page.insert_link({"kind": pymupdf.LINK_URI, "from": pymupdf.Rect(72, 650, 200, 660), "uri": f"mailto:{EMAIL}"})
    elif vector == "page_label":
        doc.set_page_labels([{"startpage": 0, "prefix": f"{NAME} ", "style": "D", "firstpagenum": 1}])
    elif vector == "annotation":
        page.add_text_annot(pymupdf.Point(80, 700), f"Not: {NAME}")
    elif vector == "info":
        doc.set_metadata({"author": NAME})
    elif vector == "xmp":
        doc.set_xml_metadata(f'<x:xmpmeta xmlns:x="adobe:ns:meta/"><dc:creator>{NAME}</dc:creator></x:xmpmeta>')
    elif vector == "piece_info":
        # Private application data on the catalog: no surface the redactor rewrites.
        doc.xref_set_key(doc.pdf_catalog(), "PieceInfo", f"<</Uygulama <</Private ({NAME})>>>>")
    elif vector == "hex_string":
        info = int(doc.xref_get_key(-1, "Info")[1].split()[0]) if doc.xref_get_key(-1, "Info")[0] == "xref" else None
        if info is None:
            doc.set_metadata({"producer": "x"})
            info = int(doc.xref_get_key(-1, "Info")[1].split()[0])
        doc.xref_set_key(info, "Client", "<" + NAME.encode("utf-16-be").hex() + ">")
    data = doc.tobytes()
    doc.close()
    return data


class ScanBreadthTests(unittest.TestCase):
    """scan_docx reads every part of the output, however and wherever the text is stored."""

    def leaks(self, data: bytes, findings=APPROVED) -> list[dict]:
        return scan_docx(data, findings)

    def parts(self, data: bytes, findings=APPROVED) -> set[str]:
        """Parts an approved item was FOUND in; an unreadable part is not a find."""
        return {leak["part"] for leak in self.leaks(data, findings) if leak["finding_id"] is not None}

    def test_a_clean_redacted_body_has_no_leak(self):
        clean = docx_bytes(body=BODY.replace(NAME, "[KISI_1]").replace(EMAIL, "[EPOSTA_1]"))
        self.assertEqual(self.leaks(clean), [])

    def test_every_xml_member_is_read_whatever_its_name_or_path(self):
        creator = f'<?xml version="1.0"?><cp:coreProperties xmlns:cp="urn:x"><dc:creator xmlns:dc="urn:y">{NAME}</dc:creator></cp:coreProperties>'
        cases = {
            "docProps/core2.xml": creator,
            "customXml/deep/odd/item.xml": f"<root>{NAME}</root>",
            "word/glossary/document.xml": f'<w:glossaryDocument xmlns:w="{W}"><w:t>{NAME}</w:t></w:glossaryDocument>',
            # Entity-escaped, so only reading them AS XML finds the name.
            "word/vmlDrawing1.vml": '<xml><v:shape xmlns:v="urn:v" alt="&#68;eniz Kurgusal"/></xml>',
            "word/opaque.bin.dat": "<?xml version='1.0'?><note>&#68;eniz Kurgusal</note>",
            "word/_rels/footer1.xml.rels": f'<Relationships><Relationship Id="r" Target="mailto:{EMAIL}" TargetMode="External"/></Relationships>',
        }
        for part, xml in cases.items():
            with self.subTest(part=part):
                self.assertIn(part, self.parts(docx_bytes(parts={part: xml})))

    def test_text_nodes_and_attribute_values_are_both_searched(self):
        cases = {
            "text node": f"<a><b>{NAME}</b></a>",
            "attribute value": f'<a title="{NAME}"/>',
            "entity-escaped text": "<a>&#68;eniz&#32;Kurgusal</a>",
            "entity-escaped attribute": '<a v="Deniz&#x20;Kurgusal"/>',
            "text split across elements": "<a><r>Deniz Kur</r><r>gusal</r></a>",
            "percent-encoded attribute": '<a href="mailto:deniz.kurgusal%40example.com"/>',
            "bookmark-style underscores": '<a name="Deniz_Kurgusal"/>',
            "comment": f"<a><!-- {NAME} --></a>",
            "CDATA": f"<a><![CDATA[{NAME}]]></a>",
            "processing instruction": f"<?note {NAME}?><a/>",
        }
        for label, xml in cases.items():
            with self.subTest(label):
                self.assertIn("word/extra.xml", self.parts(docx_bytes(parts={"word/extra.xml": xml})))

    def test_case_turkish_fold_and_whitespace_do_not_hide_a_survivor(self):
        for variant in ("DENİZ KURGUSAL", "DENIZ KURGUSAL", "deniz   kurgusal", "Deniz Kurgusal", "Deniz\nKurgusal"):
            with self.subTest(variant=variant):
                xml = f"<a>{escape(variant)}</a>"
                self.assertIn("word/extra.xml", self.parts(docx_bytes(parts={"word/extra.xml": xml})))

    def test_a_casefold_only_equivalence_is_caught(self):
        # PDF and OCR text carry the "ﬁ" ligature; only the tr_fold arm equates it with "fi".
        fikret = [finding("Fikret Kurgusal", 3)]
        self.assertIn("word/extra.xml", self.parts(docx_bytes(parts={"word/extra.xml": "<a>\ufb01kret Kurgusal</a>"}), fikret))

    def test_members_past_the_byte_budget_are_refused_unread(self):
        inner = BytesIO()
        with ZipFile(inner, "w", ZIP_DEFLATED) as archive:
            archive.writestr("xl/sharedStrings.xml", "<sst>" + "0" * 4096 + "</sst>")
        data = docx_bytes(parts={"word/embeddings/sheet1.xlsx": inner.getvalue()})
        with mock.patch("legal_analyzer.export_scan.MAX_SCANNED_BYTES", len(data) + 1024):
            leaks = self.leaks(data)
        self.assertIn(
            {"part": "word/embeddings/sheet1.xlsx/xl/sharedStrings.xml", "finding_id": None, "category": "unreadable"},
            leaks,
        )

    def test_a_text_member_that_is_not_xml_is_searched(self):
        # An altChunk carries imported HTML, RTF or plain text into the document.
        for part, data in {
            "word/afchunk.htm": f"<html><body>{NAME}</body></html>".encode(),
            "word/afchunk.txt": f"Müvekkil {NAME}".encode("utf-8"),
            "word/afchunk.rtf": b"{\\rtf1 Deniz Kurgusal}",
        }.items():
            with self.subTest(part=part):
                self.assertIn(part, self.parts(docx_bytes(parts={part: data})))

    def test_pathological_markup_is_scanned_in_linear_time(self):
        # Unterminated comments, and one tag that is a single unbroken word, each
        # used to make the tokenizer or the attribute pattern quadratic (13 s and 53 s).
        for label, xml in {
            "unterminated comments": "<a>" + "<!--x" * 20000 + NAME,
            "unbroken tag": "<a " + "b" * 100000 + f' t="{NAME}">',
        }.items():
            with self.subTest(label):
                started = time.monotonic()
                parts = self.parts(docx_bytes(parts={"word/extra.xml": xml}))
                self.assertLess(time.monotonic() - started, 5)
                self.assertIn("word/extra.xml", parts)

    def test_a_utf16_part_without_a_byte_order_mark_is_decoded(self):
        for encoding in ("utf-16-le", "utf-16-be"):
            with self.subTest(encoding=encoding):
                xml = f"<a>{NAME}</a>".encode(encoding)
                self.assertIn("word/extra.xml", self.parts(docx_bytes(parts={"word/extra.xml": xml})))

    def test_a_binary_member_is_not_searched(self):
        # An EMF image or an obfuscated font: NUL-bearing bytes in which a short
        # sample turns up by chance. They are not text and are not decoded as text.
        for part in ("word/media/image1.emf", "word/fonts/font1.odttf"):
            with self.subTest(part=part):
                data = b"\x01\x00\x00\x00" + f"Not: {NAME}".encode("utf-16-le")
                self.assertEqual(self.leaks(docx_bytes(body="", parts={part: data})), [])

    def test_a_part_declaring_entities_fails_closed(self):
        # "&a;&b;" assembles the name at parse time; no search of the source can.
        xml = '<?xml version="1.0"?><!DOCTYPE a [<!ENTITY a "Den"><!ENTITY b "iz Kurgusal">]><a>&a;&b;</a>'
        leaks = self.leaks(docx_bytes(parts={"word/extra.xml": xml}))
        self.assertIn({"part": "word/extra.xml", "finding_id": None, "category": "unreadable"}, leaks)

    def test_a_part_name_is_echoed_only_in_the_standard_shape(self):
        # The member's name is no finding, so only the shape rule keeps it out of
        # the report; its content leaks, so it is reported.
        data = docx_bytes(parts={"docProps/Mehmet Masalcı.xml": f"<a>{NAME}</a>", "word/extra.xml": f"<a>{NAME}</a>"})
        leaks = self.leaks(data)
        parts = {leak["part"] for leak in leaks}
        self.assertIn("word/extra.xml", parts)
        self.assertTrue(any(part.startswith("member #") for part in parts), parts)
        self.assertNotIn("masalc", json.dumps(leaks, ensure_ascii=False).casefold())

    def test_a_standard_shaped_name_carrying_a_sample_is_reported_by_position(self):
        typed = [finding("kurgusal", 4, source="reviewer")]
        leaks = self.leaks(docx_bytes(parts={"docProps/kurgusal.xml": "<a>kurgusal</a>"}), typed)
        self.assertTrue(leaks)
        self.assertNotIn("kurgusal", json.dumps(leaks).casefold())

    def test_a_utf16_part_is_decoded(self):
        xml = f'<?xml version="1.0" encoding="UTF-16"?><a>{NAME}</a>'.encode("utf-16")
        self.assertIn("word/extra.xml", self.parts(docx_bytes(parts={"word/extra.xml": xml})))

    def test_a_part_that_cannot_be_decoded_fails_closed(self):
        leaks = self.leaks(docx_bytes(parts={"word/extra.xml": b"<a>\xff\xfe\xfa broken</a>"}))
        self.assertIn({"part": "word/extra.xml", "finding_id": None, "category": "unreadable"}, leaks)

    def test_a_package_nested_in_the_package_is_opened(self):
        inner = BytesIO()
        with ZipFile(inner, "w") as archive:
            archive.writestr("xl/sharedStrings.xml", f"<sst><si><t>{NAME}</t></si></sst>")
        data = docx_bytes(parts={"word/embeddings/sheet1.xlsx": inner.getvalue()})
        self.assertIn("word/embeddings/sheet1.xlsx/xl/sharedStrings.xml", self.parts(data))

    def test_an_attribute_value_matches_a_sample_only_as_a_whole_word(self):
        short_names = [finding(name, index, source="reviewer") for index, name in enumerate(SHORT_NAMES, start=10)]
        boilerplate = {
            "word/fontTable.xml": f'<w:fonts xmlns:w="{W}"><w:font w:name="Calibri"/></w:fonts>',
            "word/settings.xml": f'<w:settings xmlns:w="{W}" xmlns:sl="http://schemas.openxmlformats.org/schemaLibrary/2006/main">'
            '<w:compat><w:compatSetting w:name="enableOpenTypeFeatures"/></w:compat></w:settings>',
            "word/styles.xml": f'<w:styles xmlns:w="{W}"><w:style w:styleId="IntenseReference"><w:name w:val="Intense Reference"/>'
            '<w:tblStylePr w:type="neCell"/></w:style></w:styles>',
            "word/_rels/document.xml.rels": '<Relationships><Relationship Id="rId1" '
            'Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="x"/></Relationships>',
        }
        self.assertEqual(self.leaks(docx_bytes(body="", parts=boilerplate), short_names), [])
        ela = [finding("Ela", 11, source="reviewer")]
        for label, value in {
            "mailto": "mailto:ela.yilmaz@example.com",
            "document variable": "Ela Yılmaz",
            "bookmark": "_Ela_Yilmaz",
            "namespace URI": "urn:example:Ela",
            "standard-prefixed URI": "http://schemas.openxmlformats.org/Ela/2006",
            "upper case": "ELA YILMAZ",
        }.items():
            with self.subTest(label):
                xml = f'<a xmlns:x="{value}"/>' if "URI" in label else f'<a v="{escape(value)}"/>'
                self.assertIn("word/extra.xml", self.parts(docx_bytes(body="", parts={"word/extra.xml": xml}), ela))
        # Only the tr_fold arm equates the "ﬁ" ligature with "fi".
        fikret = [finding("Fikret", 12, source="reviewer")]
        self.assertIn("word/extra.xml", self.parts(docx_bytes(body="", parts={"word/extra.xml": '<a v="Sayın \ufb01kret"/>'}), fikret))
        # A lowercase leading component is not a capitalised one (see CamelCaseComponentTests).
        self.assertEqual(self.parts(docx_bytes(body="", parts={"word/extra.xml": '<a v="elaYilmaz"/>'}), ela), set())
        # A style's name is still searched like any attribute.
        strong = [finding("Strong", 9)]
        styles = f'<w:styles xmlns:w="{W}"><w:style w:styleId="Strong"><w:name w:val="Strong"/></w:style></w:styles>'
        self.assertIn("word/styles.xml", self.parts(docx_bytes(parts={"word/styles.xml": styles}), strong))

    def test_a_text_node_keeps_substring_matching(self):
        ela = [finding("Ela", 11, source="reviewer")]
        self.assertIn("word/extra.xml", self.parts(docx_bytes(body="", parts={"word/extra.xml": "<a>elaYilmaz</a>"}), ela))

    def test_text_is_joined_within_a_paragraph_and_never_across_one(self):
        ali = [finding("Ali", 8, source="reviewer")]
        for label, xml in {
            "paragraphs": "<w:p><w:r><w:t>Ankara</w:t></w:r></w:p><w:p><w:r><w:t>Lira</w:t></w:r></w:p>",
            "table cells": "<w:tr><w:tc><w:p><w:r><w:t>Ankara</w:t></w:r></w:p></w:tc><w:tc><w:t>Lira</w:t></w:tc></w:tr>",
        }.items():
            with self.subTest(label):
                self.assertEqual(self.parts(docx_bytes(body="", extra_body=xml), ali), set())
        split = '<w:p><w:r><w:t>Deniz Kur</w:t></w:r><w:r><w:rPr><w:b/></w:rPr><w:t>gusal</w:t></w:r></w:p>'
        self.assertIn("word/document.xml", self.parts(docx_bytes(body="", extra_body=split)))

    def test_a_bare_surname_uses_the_redactors_capitalised_whole_word_predicate(self):
        surname = [finding("Kale", 7)]
        self.assertEqual(self.parts(docx_bytes(parts={"word/extra.xml": '<a v="kalemine teslim"/>'}), surname), set())
        self.assertEqual(self.parts(docx_bytes(parts={"word/extra.xml": '<a v="Kalemine teslim"/>'}), surname), set())
        self.assertIn("word/extra.xml", self.parts(docx_bytes(parts={"word/extra.xml": '<a v="Sayın Kale"/>'}), surname))
        # A reviewer-typed finding of the same shape keeps substring matching, as the redactor does.
        typed = [finding("Kale", 7, source="reviewer")]
        self.assertIn("word/extra.xml", self.parts(docx_bytes(parts={"word/extra.xml": "<a>Kalemine</a>"}), typed))

    def test_only_the_findings_the_export_removes_are_searched(self):
        kept = [finding(NAME, 1, review_status="retained"), finding(EMAIL, 2, "email_address", review_status="dismissed")]
        self.assertEqual(self.leaks(docx_bytes(), kept), [])
        typed = [finding(NAME, 1, review_status="added_by_reviewer")]
        self.assertIn("word/document.xml", self.parts(docx_bytes(), typed))

    def test_a_leak_names_the_part_and_never_carries_the_sample(self):
        data = docx_bytes(parts={"docProps/Deniz Kurgusal.xml": f"<a>{NAME}</a>", **DOC_VARIABLE})
        leaks = self.leaks(data)
        self.assertTrue(leaks)
        for leak in leaks:
            self.assertEqual(set(leak), {"part", "finding_id", "category"})
        dumped = json.dumps(leaks, ensure_ascii=False).casefold()
        for secret in SECRETS:
            self.assertNotIn(secret, dumped)
        # The member whose NAME carries the sample is reported by position.
        self.assertTrue(any(leak["part"].startswith("member #") for leak in leaks), leaks)
        self.assertIn("package member names", {leak["part"] for leak in leaks})


class ExternalTargetAgreesWithScanTests(unittest.TestCase):
    """The rels scrubber neutralises every external target the scan's attribute rule would flag."""

    def test_a_lowercase_bare_surname_in_an_external_target_is_neutralised(self):
        surname = [finding("Kale", 7)]
        targets = [RedactionTarget("Kale", "[KISI_1]", standalone=True)]
        for target in ("https://kale-hukuk.example.com/", "mailto:kale@example.com", "https://example.com/_kale_"):
            with self.subTest(target=target):
                relationship = f'<Relationship Id="rId9" Type="{HYPERLINK_TYPE}" Target="{target}" TargetMode="External"/>'
                with TemporaryDirectory() as tmp:
                    source = Path(tmp) / "kale.docx"
                    source.write_bytes(docx_bytes(body="Sayın Kale dilekçeyi imzaladı.", relationships=relationship))
                    output = redact_docx(source, targets)
                self.assertEqual(scan_docx(output, surname), [])
                with ZipFile(BytesIO(output)) as archive:
                    self.assertNotIn(target, archive.read("word/_rels/document.xml.rels").decode())


    def test_only_an_external_relationship_target_is_neutralised(self):
        # An internal target is a part path the package needs; only TargetMode="External" is a URL for the reader.
        rels = (
            '<Relationships><Relationship Id="rId1" Type="x" Target="mailto:deniz.kurgusal@example.com"/>'
            '<Relationship Id="rId2" Type="x" Target="mailto:deniz.kurgusal@example.com" TargetMode="External"/></Relationships>'
        ).encode()
        scrubbed = scrub_external_targets(rels, [RedactionTarget(EMAIL, "[EPOSTA_1]")]).decode()
        self.assertIn('Id="rId1" Type="x" Target="mailto:deniz.kurgusal@example.com"', scrubbed)
        self.assertIn('Id="rId2" Type="x" Target="#redacted" TargetMode="External"', scrubbed)


class PdfScanBreadthTests(unittest.TestCase):
    """scan_pdf reads every place a PDF can carry text, not only the page text."""

    def test_each_surface_outside_the_page_text_is_named(self):
        cases = {
            "outline": "outline",
            "attachment": "embedded file #1",
            "link": "page 1 links",
            "page_label": "page labels",
            "annotation": "page 1 annotations",
            "info": "info",
            "xmp": "xmp",
        }
        for vector, part in cases.items():
            with self.subTest(vector=vector):
                data = pdf_bytes(vector)
                parts = {leak["part"] for leak in scan_pdf(data, APPROVED) if leak["finding_id"] is not None}
                self.assertIn(part, parts)

    def test_hex_and_utf16_object_strings_are_decoded(self):
        parts = {leak["part"] for leak in scan_pdf(pdf_bytes("hex_string"), APPROVED) if leak["finding_id"] is not None}
        self.assertTrue(any(part.startswith("object ") for part in parts), parts)

    def test_an_attachment_that_declares_no_size_is_refused_unread(self):
        real = pymupdf.Document.embfile_info

        def no_size(doc, item):
            info = dict(real(doc, item))
            info.pop("size", None)
            return info

        with mock.patch.object(pymupdf.Document, "embfile_info", no_size):
            leaks = scan_pdf(pdf_bytes("attachment"), APPROVED)
        self.assertIn({"part": "embedded file #1", "finding_id": None, "category": "unreadable"}, leaks)

    def test_a_stream_that_inflates_past_the_budget_is_refused_unread(self):
        data = pdf_bytes()
        with pymupdf.open(stream=data, filetype="pdf") as doc:
            xref = doc.get_new_xref()
            doc.update_object(xref, "<<>>")
            doc.update_stream(xref, b"0" * 4_000_000)  # compressed to a few kilobytes
            data = doc.tobytes()
        with mock.patch("legal_analyzer.export_scan.MAX_SCANNED_BYTES", 1_000_000):
            leaks = scan_pdf(data, APPROVED)
        self.assertIn({"part": f"object {xref} stream", "finding_id": None, "category": "unreadable"}, leaks)

    def test_an_attached_pdf_is_budgeted_before_its_pages_are_read(self):
        attached = pymupdf.open()
        attached.new_page().insert_text((72, 72), "Ek sayfa")
        xref = attached.get_new_xref()
        attached.update_object(xref, "<<>>")
        attached.update_stream(xref, b"0" * 4_000_000)
        outer = pymupdf.open(stream=pdf_bytes(), filetype="pdf")
        outer.embfile_add("ek.pdf", attached.tobytes(deflate=True), filename="ek.pdf")
        data = outer.tobytes()
        attached.close()
        outer.close()
        read = []
        real = pymupdf.Page.get_text

        def recording(page, *args, **kwargs):
            read.append(page.parent.page_count)
            return real(page, *args, **kwargs)

        with mock.patch("legal_analyzer.export_scan.MAX_SCANNED_BYTES", 1_000_000), mock.patch.object(pymupdf.Page, "get_text", recording):
            leaks = scan_pdf(data, APPROVED)
        self.assertIn({"part": f"embedded file #1/object {xref} stream", "finding_id": None, "category": "unreadable"}, leaks)
        self.assertEqual(len(read), 1, "only the outer page's text may be read")

    def test_an_attachment_understating_its_size_is_refused_before_it_is_inflated(self):
        doc = pymupdf.open(stream=pdf_bytes(), filetype="pdf")
        doc.embfile_add("ek.txt", b"0" * 4_000_000, filename="ek.txt")
        data = doc.tobytes(deflate=True)
        doc.close()
        doc = pymupdf.open(stream=data, filetype="pdf")
        stream = next(x for x in range(1, doc.xref_length()) if doc.xref_get_key(x, "Type") == ("name", "/EmbeddedFile"))
        # The declared size lies, in both places PyMuPDF reads it from.
        doc.xref_set_key(stream, "Params", "<</Size 10>>")
        doc.xref_set_key(stream, "DL", "10")
        data = doc.tobytes()
        doc.close()
        inflated = []
        real = pymupdf.Document.embfile_get

        def recording(document, item):
            inflated.append(item)
            return real(document, item)

        with pymupdf.open(stream=data, filetype="pdf") as check:
            self.assertEqual(check.embfile_info(0)["size"], 10)
        with mock.patch("legal_analyzer.export_scan.MAX_SCANNED_BYTES", 1_000_000), mock.patch.object(pymupdf.Document, "embfile_get", recording):
            leaks = scan_pdf(data, APPROVED)
        self.assertIn({"part": f"object {stream} stream", "finding_id": None, "category": "unreadable"}, leaks)
        self.assertEqual(inflated, [])

    def test_an_unreadable_pdf_fails_closed(self):
        self.assertEqual(
            scan_pdf(b"%PDF-1.7 not really", APPROVED),
            [{"part": "document", "finding_id": None, "category": "unreadable"}],
        )


class ExportHarness(unittest.TestCase):
    """Upload, review, sign off and export through the real HTTP surfaces."""

    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tmp_path = Path(self.tmp.name)
        db_path = self.tmp_path / "scan.db"
        previous = (app_module.DB_PATH, app_module.EXPORT_DIR)
        app_module.DB_PATH = db_path
        app_module.EXPORT_DIR = self.tmp_path / "exports"
        app_module.READY_DB_PATHS.discard(db_path.resolve())

        def restore() -> None:
            app_module.DB_PATH, app_module.EXPORT_DIR = previous
            app_module.READY_DB_PATHS.discard(db_path.resolve())

        self.addCleanup(restore)
        conn = sqlite3.connect(db_path)
        conn.executescript((PROJECT_DIR / "db" / "schema.sql").read_text(encoding="utf-8"))
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
            ("reviewer", generate_password_hash("synthetic-password"), "reviewer"),
        )
        self.matter_id = conn.execute("INSERT INTO matters (name) VALUES ('Synthetic export matter')").lastrowid
        conn.commit()
        conn.close()
        self.client = app_module.app.test_client()
        response = self.client.post("/login", json={"username": "reviewer", "password": "synthetic-password"})
        self.assertIn(response.status_code, (200, 302))

    def upload(self, name: str, data: bytes) -> int:
        with mock.patch.object(app_module, "UPLOAD_DIR", self.tmp_path / "uploads"):
            response = self.client.post(
                "/api/upload",
                data={"file": (BytesIO(data), name), "matter_id": str(self.matter_id)},
                content_type="multipart/form-data",
            )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        return response.get_json()["id"]

    def review_and_sign_off(self, doc_id: int, pdf: bool = False) -> None:
        if pdf:
            response = self.client.post(f"/api/document/{doc_id}/pdf/regions/generate", json={})
            self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
            response = self.client.post(f"/api/document/{doc_id}/pdf/regions/review-batch", json={"action": "approve"})
            self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        response = self.client.post(
            f"/api/document/{doc_id}/findings/review-batch", json={"action": "approve", "only_pending": True}
        )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        for action in ("mark_redacted", "approve"):
            response = self.client.post(f"/api/document/{doc_id}/review", json={"action": action})
            self.assertEqual(response.status_code, 200, response.get_data(as_text=True))

    def signed_off(self, name: str, data: bytes) -> int:
        doc_id = self.upload(name, data)
        self.review_and_sign_off(doc_id, pdf=name.endswith(".pdf"))
        return doc_id

    def export(self, doc_id: int, export_format: str = "docx"):
        response = self.client.get(f"/api/document/{doc_id}/redacted-export?format={export_format}")
        self.addCleanup(response.close)
        return response

    def qa(self, doc_id: int, export_format: str = "docx") -> dict:
        response = self.client.get(f"/api/document/{doc_id}/redacted-export/qa?format={export_format}")
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        return response.get_json()

    def approved_findings(self, doc_id: int) -> list[dict]:
        conn = sqlite3.connect(app_module.DB_PATH)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id, category, sample, source, review_status FROM privacy_findings "
            "WHERE document_id = ? AND review_status IN ('approved', 'added_by_reviewer')",
            (doc_id,),
        ).fetchall()
        conn.close()
        return [dict(row) for row in rows]

    def rows(self, sql: str, *params) -> list[tuple]:
        conn = sqlite3.connect(app_module.DB_PATH)
        rows = conn.execute(sql, params).fetchall()
        conn.close()
        return rows


class OrdinaryWordVectorsTests(ExportHarness):
    """Part 4: the hyperlink relationship and tracked deletions / field codes are redacted, so these export."""

    def exported(self, data: bytes) -> tuple[int, bytes]:
        doc_id = self.signed_off("dilekce.docx", data)
        response = self.export(doc_id)
        self.assertEqual(response.status_code, 200, response.get_data()[:400])
        output = response.get_data()
        self.assertEqual(scan_docx(output, self.approved_findings(doc_id)), [])
        return doc_id, output

    def test_a_typed_email_hyperlink_exports_with_its_target_neutralised(self):
        _, output = self.exported(docx_bytes(HYPERLINK_PARAGRAPH, relationships=MAILTO_RELATIONSHIP))
        with ZipFile(BytesIO(output)) as archive:
            rels = ElementTree.fromstring(archive.read("word/_rels/document.xml.rels"))
        (relationship,) = list(rels)
        # Kept, so the w:hyperlink's r:id still resolves; only the target changed.
        self.assertEqual(relationship.get("Id"), "rId9")
        self.assertEqual(relationship.get("TargetMode"), "External")
        self.assertEqual(relationship.get("Target"), "#redacted")

    def test_an_external_target_carrying_no_approved_item_is_left_alone(self):
        unrelated = f'<Relationship Id="rId9" Type="{HYPERLINK_TYPE}" Target="https://example.org/mevzuat" TargetMode="External"/>'
        _, output = self.exported(docx_bytes(relationships=unrelated))
        with ZipFile(BytesIO(output)) as archive:
            self.assertIn(b'Target="https://example.org/mevzuat"', archive.read("word/_rels/document.xml.rels"))

    def test_a_tracked_deletion_exports_redacted(self):
        _, output = self.exported(docx_bytes(TRACKED_DELETION))
        with ZipFile(BytesIO(output)) as archive:
            root = ElementTree.fromstring(archive.read("word/document.xml"))
        deleted = "".join(node.text or "" for node in root.iter(f"{{{W}}}delText"))
        self.assertTrue(deleted.startswith("Eski vekil "), deleted)
        self.assertNotIn("Kurgusal", deleted)

    def test_a_field_code_exports_redacted(self):
        _, output = self.exported(docx_bytes(FIELD_CODE))
        with ZipFile(BytesIO(output)) as archive:
            root = ElementTree.fromstring(archive.read("word/document.xml"))
        code = "".join(node.text or "" for node in root.iter(f"{{{W}}}instrText"))
        self.assertIn("HYPERLINK", code)
        self.assertNotIn("kurgusal", code.casefold())

    def test_all_of_them_together_export(self):
        self.exported(docx_bytes(HYPERLINK_PARAGRAPH + TRACKED_DELETION + FIELD_CODE, relationships=MAILTO_RELATIONSHIP))


WP_DOC_PR = f"{{{WP}}}docPr"
PIC_C_NV_PR = "{http://schemas.openxmlformats.org/drawingml/2006/picture}cNvPr"


def w(name: str) -> str:
    return f"{{{W}}}{name}"


class RedactedVectorExportTests(ExportHarness):
    """The places the scan used to refuse are redacted, so these files export through the real endpoint.

    Each export is the build path's own scan passing; each test then reads the
    file back for the change that made it pass, and for the package still
    holding together.
    """

    exported = OrdinaryWordVectorsTests.exported

    def document(self, output: bytes):
        with ZipFile(BytesIO(output)) as archive:
            for name in archive.namelist():
                if name.endswith((".xml", ".rels")) and archive.read(name).strip():
                    ElementTree.fromstring(archive.read(name))  # every XML part still parses
            return ElementTree.fromstring(archive.read("word/document.xml"))

    def test_an_image_description_title_and_name_export_redacted(self):
        _, output = self.exported(docx_bytes(IMAGE_DESCRIPTION))
        root = self.document(output)
        for element in (next(root.iter(WP_DOC_PR)), next(root.iter(PIC_C_NV_PR))):
            values = " ".join(element.get(key) or "" for key in ("descr", "title", "name"))
            self.assertNotIn("kurgusal", values.casefold())
        description = next(root.iter(WP_DOC_PR)).get("descr")
        self.assertTrue(description.endswith(" imza örneği"), description)
        self.assertEqual(next(root.iter(WP_DOC_PR)).get("name"), "Resim 1")

    def test_a_tooltip_exports_redacted(self):
        encoded = TOOLTIP.replace(EMAIL, EMAIL.replace("@", "%40"))
        for label, paragraph in {"plain": TOOLTIP, "percent-encoded": encoded}.items():
            with self.subTest(label):
                _, output = self.exported(docx_bytes(paragraph))
                (hyperlink,) = [node for node in self.document(output).iter(w("hyperlink")) if node.get(w("anchor")) == "ek"]
                self.assertNotIn("kurgusal", hyperlink.get(w("tooltip")).casefold())

    def test_a_bookmark_is_renamed_with_everything_that_points_at_it(self):
        for label, prefix in {"bookmark": "", "hidden bookmark": "_"}.items():
            with self.subTest(label):
                vector = (BOOKMARK + BOOKMARK_REFERENCES).replace("Deniz_Kurgusal", prefix + "Deniz_Kurgusal")
                _, output = self.exported(docx_bytes(vector))
                root = self.document(output)
                (start,), (end,) = list(root.iter(w("bookmarkStart"))), list(root.iter(w("bookmarkEnd")))
                renamed = start.get(w("name"))
                self.assertEqual(renamed, f"{prefix}Redacted_1")
                # The pair still matches by id, and the link and the cross-reference still resolve.
                self.assertEqual(start.get(w("id")), end.get(w("id")))
                self.assertEqual([node.get(w("anchor")) for node in root.iter(w("hyperlink"))], [renamed])
                code = "".join(node.text or "" for node in root.iter(w("instrText")))
                self.assertEqual(code.split(), ["REF", renamed, "\\h"])

    def test_a_simple_field_follows_its_renamed_bookmark(self):
        simple_field = f'<w:p><w:fldSimple w:instr=" REF {NAME.replace(" ", "_")} \\h "><w:r><w:t>Ek</w:t></w:r></w:fldSimple></w:p>'
        _, output = self.exported(docx_bytes(BOOKMARK + simple_field))
        root = self.document(output)
        renamed = next(root.iter(w("bookmarkStart"))).get(w("name"))
        self.assertEqual(renamed, "Redacted_1")
        self.assertEqual(next(root.iter(w("fldSimple"))).get(w("instr")).split(), ["REF", renamed, "\\h"])

    def test_a_content_control_exports_with_its_title_redacted_and_its_tag_emptied(self):
        _, output = self.exported(docx_bytes(CONTENT_CONTROL))
        root = self.document(output)
        alias = next(root.iter(w("alias"))).get(w("val"))
        self.assertNotIn("kurgusal", alias.casefold())
        self.assertTrue(alias.endswith(" imzası"), alias)
        self.assertEqual(next(root.iter(w("tag"))).get(w("val")), "")

    def test_a_document_variable_exports_without_its_name_or_value(self):
        _, output = self.exported(docx_bytes(parts=DOC_VARIABLE))
        with ZipFile(BytesIO(output)) as archive:
            settings = ElementTree.fromstring(archive.read("word/settings.xml"))
        (variable,) = list(settings.iter(w("docVar")))
        self.assertEqual((variable.get(w("name")), variable.get(w("val"))), ("Redacted_1", ""))

    def test_non_standard_document_properties_export_emptied(self):
        _, output = self.exported(docx_bytes(parts=OTHER_PROPERTIES))
        with ZipFile(BytesIO(output)) as archive:
            # Kept, so a relationship or content-type override naming them still resolves.
            self.assertEqual(ElementTree.fromstring(archive.read("docProps/core2.xml")).tag, "Properties")
            self.assertEqual(archive.read("docProps/notes.txt"), b"")

    def pdf_exported(self, data: bytes) -> pymupdf.Document:
        doc_id = self.signed_off("dilekce.pdf", data)
        response = self.export(doc_id, "pdf")
        self.assertEqual(response.status_code, 200, response.get_data()[:400])
        self.assertEqual(scan_pdf(response.get_data(), self.approved_findings(doc_id)), [])
        doc = pymupdf.open(stream=response.get_data(), filetype="pdf")
        self.addCleanup(doc.close)
        return doc

    def test_an_outline_title_exports_redacted(self):
        doc = self.pdf_exported(pdf_bytes("outline"))
        self.assertEqual(doc.get_toc(), [[1, f"{PDF_REDACTION_MARK} - Dilekçe", 1]])

    def test_an_attachment_is_removed(self):
        doc = self.pdf_exported(pdf_bytes("attachment"))
        self.assertEqual(doc.embfile_count(), 0)

    def test_a_link_carrying_an_approved_item_is_removed_and_another_kept(self):
        source = pymupdf.open(stream=pdf_bytes("link"), filetype="pdf")
        source[0].insert_link({"kind": pymupdf.LINK_URI, "from": pymupdf.Rect(72, 670, 200, 680), "uri": "https://example.org/mevzuat"})
        data = source.tobytes()
        source.close()
        doc = self.pdf_exported(data)
        self.assertEqual([link.get("uri") for link in doc[0].get_links()], ["https://example.org/mevzuat"])

    def test_a_page_label_prefix_exports_redacted(self):
        doc = self.pdf_exported(pdf_bytes("page_label"))
        kind, labels = doc.xref_get_key(doc.pdf_catalog(), "PageLabels")
        self.assertEqual(labels.upper(), f"<</Nums[0<</P{pymupdf.get_pdf_str(PDF_REDACTION_MARK + ' ')}/S/D>>]>>".upper())

    def test_a_utf16_page_label_prefix_exports_redacted(self):
        source = pymupdf.open(stream=pdf_bytes(), filetype="pdf")
        # The dash is outside PDFDocEncoding, so the prefix is written as UTF-16 hex.
        source.xref_set_key(source.pdf_catalog(), "PageLabels", f"<</Nums[0<</P{pymupdf.get_pdf_str(NAME + ' – ')}/S/D>>]>>")
        data = source.tobytes()
        source.close()
        self.assertIn(b"FEFF", data)
        doc = self.pdf_exported(data)
        self.assertIn(pymupdf.get_pdf_str(PDF_REDACTION_MARK + " – ").upper(), doc.xref_object(doc.pdf_catalog()).upper())


class RefusedDownloadTests(ExportHarness):
    """Parts 1-2: what is still not redacted is refused, with an audited 409 and no artifact."""

    VECTORS = {
        "style name": {"parts": STYLE_NAME},
        "VML alt text": {"extra_body": VML_ALT_TEXT},
        # The redactor empties a non-standard properties part; its member NAME is still the client's.
        "properties part named after the client": {"parts": {"docProps/Deniz Kurgusal.xml": "<a/>"}},
    }

    def test_each_unredacted_docx_vector_refuses_the_download(self):
        for label, vector in self.VECTORS.items():
            with self.subTest(label):
                doc_id = self.signed_off(f"{label.replace(' ', '_')}.docx", docx_bytes(**vector))
                response = self.export(doc_id)
                self.assertEqual(response.status_code, 409, response.get_data()[:300])
                self.assertTrue(response.get_json()["error"].startswith("Export refused: "), response.get_json())
                self.assertEqual(
                    self.rows("SELECT COUNT(*) FROM document_artifacts WHERE document_id = ? AND artifact_type = 'redacted_docx'", doc_id),
                    [(0,)],
                )

    def test_the_mask_style_is_refused_too(self):
        doc_id = self.signed_off("stil.docx", docx_bytes(parts=STYLE_NAME))
        response = self.client.get(f"/api/document/{doc_id}/redacted-export?format=docx&style=mask")
        self.addCleanup(response.close)
        self.assertEqual(response.status_code, 409)

    def test_each_unredacted_pdf_vector_refuses_the_download(self):
        for vector in ("piece_info",):
            with self.subTest(vector):
                doc_id = self.signed_off(f"{vector}.pdf", pdf_bytes(vector))
                response = self.export(doc_id, "pdf")
                self.assertEqual(response.status_code, 409, response.get_data()[:300])
                self.assertTrue(response.get_json()["error"].startswith("Export refused: "))
                self.assertEqual(
                    self.rows("SELECT COUNT(*) FROM document_artifacts WHERE document_id = ? AND artifact_type = 'redacted_pdf'", doc_id),
                    [(0,)],
                )
                saved = list((app_module.EXPORT_DIR / f"document_{doc_id}").glob("*.pdf"))
                self.assertEqual(saved, [], "a refused PDF must not be written to the export folder")

    def test_a_clean_pdf_still_exports(self):
        doc_id = self.signed_off("temiz.pdf", pdf_bytes())
        response = self.export(doc_id, "pdf")
        self.assertEqual(response.status_code, 200, response.get_data()[:300])
        self.assertEqual(scan_pdf(response.get_data(), self.approved_findings(doc_id)), [])


class MatterArchiveTests(ExportHarness):
    def test_the_archive_holds_the_clean_document_and_leaves_out_the_leaking_one(self):
        clean_id = self.signed_off("a_temiz.docx", docx_bytes())
        leaking_id = self.signed_off("b_stil.docx", docx_bytes(parts=STYLE_NAME))
        response = self.client.get(f"/api/matter/{self.matter_id}/redacted-export")
        self.addCleanup(response.close)
        self.assertEqual(response.status_code, 200, response.get_data()[:300])
        self.assertEqual(response.headers["X-Exported-Count"], "1")
        self.assertEqual(response.headers["X-Held-Back-Count"], "1")
        with ZipFile(BytesIO(response.get_data())) as archive:
            self.assertEqual(archive.namelist(), ["a_temiz_redacted.docx"])
            for name in archive.namelist():
                self.assertEqual(scan_docx(archive.read(name), self.approved_findings(clean_id)), [])
        self.assertEqual(
            self.rows("SELECT result FROM audit_log WHERE document_id = ? AND action = 'export.redacted_docx'", leaking_id),
            [("blocked",)],
        )

    def test_a_matter_whose_only_document_leaks_exports_nothing(self):
        self.signed_off("stil.docx", docx_bytes(parts=STYLE_NAME))
        response = self.client.get(f"/api/matter/{self.matter_id}/redacted-export")
        self.addCleanup(response.close)
        self.assertEqual(response.status_code, 409)


class RefusalAuditTests(ExportHarness):
    """The refusal's audit row and message name parts and counts, never a value."""

    def assert_no_secret(self, text: str) -> None:
        for secret in (*SECRETS, EMAIL.split("@")[0]):
            self.assertNotIn(secret, text.casefold())

    def test_a_docx_refusal_writes_parts_and_counts_only(self):
        # A member whose own name is the client's: the label must not echo it.
        data = docx_bytes(parts={"docProps/Deniz Kurgusal.xml": f"<a>{NAME}</a>", **STYLE_NAME})
        doc_id = self.signed_off("stil.docx", data)
        response = self.export(doc_id)
        self.assertEqual(response.status_code, 409)
        self.assert_no_secret(response.get_data(as_text=True))
        rows = self.rows(
            "SELECT result, metadata FROM audit_log WHERE document_id = ? AND action = 'export.redacted_docx'", doc_id
        )
        self.assertEqual([result for result, _ in rows], ["blocked"])
        metadata = json.loads(rows[0][1])
        self.assertEqual(metadata["reason"], "output_scan")
        self.assertGreater(metadata["item_count"], 0)
        self.assertIn("word/styles.xml", metadata["parts"])
        self.assert_no_secret(rows[0][1])

    def test_a_pdf_refusal_writes_parts_and_counts_only(self):
        doc_id = self.signed_off("ek.pdf", pdf_bytes("piece_info"))
        response = self.export(doc_id, "pdf")
        self.assertEqual(response.status_code, 409)
        self.assert_no_secret(response.get_data(as_text=True))
        rows = self.rows(
            "SELECT result, metadata FROM audit_log WHERE document_id = ? AND action = 'export.redacted_pdf'", doc_id
        )
        self.assertEqual([result for result, _ in rows], ["blocked"])
        self.assertTrue(any(part.startswith("object ") for part in json.loads(rows[0][1])["parts"]), rows)
        self.assert_no_secret(rows[0][1])


class QaVerificationTests(ExportHarness):
    """Part 3: both QA endpoints run the scan, and only a clean pass is stamped verified."""

    def stamps(self, doc_id: int) -> set[str]:
        return {
            row[0]
            for row in self.rows("SELECT DISTINCT verification_status FROM finding_evidence WHERE document_id = ?", doc_id)
        }

    def scan_check(self, report: dict) -> dict:
        return next(check for check in report["checks"] if check["name"] == "Approved items absent from every part of the file")

    def test_one_rule_for_the_stamp(self):
        self.assertEqual(verification_status({"overall_status": "pass"}), "redacted_verified")
        for status in ("warn", "fail", None):
            with self.subTest(status=status):
                self.assertEqual(verification_status({"overall_status": status}), "qa_warning")

    def test_a_docx_whose_qa_fails_for_a_reason_other_than_leakage_is_not_verified(self):
        # An unprefixed revision author: the redactor's prefixed-attribute scrub
        # leaves it and QA's identity check reports it, so QA's own
        # overall_status is "fail" while leakage_count is 0 -- the report the old
        # condition stamped. "Yazar" is no finding, so the export still goes out.
        font_table = f'<?xml version="1.0"?><w:fonts xmlns:w="{W}" author="Yazar"/>'
        doc_id = self.signed_off("fontlar.docx", docx_bytes(parts={"word/fontTable.xml": font_table}))
        self.assertEqual(self.export(doc_id).status_code, 200)
        report = self.qa(doc_id)
        self.assertEqual(report["overall_status"], "fail")
        self.assertEqual(report["leakage_count"], 0)
        self.assertEqual(self.stamps(doc_id), {"qa_warning"})

    def test_a_clean_docx_is_verified(self):
        doc_id = self.signed_off("temiz.docx", docx_bytes())
        report = self.qa(doc_id)
        self.assertEqual(report["overall_status"], "pass", report["checks"])
        self.assertEqual(self.scan_check(report)["status"], "pass")
        self.assertEqual(self.stamps(doc_id), {"redacted_verified"})

    def test_docx_qa_runs_the_scan(self):
        doc_id = self.signed_off("stil.docx", docx_bytes(parts=STYLE_NAME))
        report = self.qa(doc_id)
        self.assertEqual(report["leakage_count"], 0, "the text-part check cannot see a style name")
        self.assertEqual(self.scan_check(report)["status"], "fail")
        self.assertIn("word/styles.xml", self.scan_check(report)["detail"])
        self.assertEqual(report["overall_status"], "fail")
        self.assertEqual(self.stamps(doc_id), {"qa_warning"})

    def test_pdf_qa_runs_the_scan(self):
        doc_id = self.signed_off("uygulama.pdf", pdf_bytes("piece_info"))
        report = self.qa(doc_id, "pdf")
        self.assertEqual(report["leakage_count"], 0, "the page-text check cannot see the catalog's private data")
        self.assertEqual(self.scan_check(report)["status"], "fail")
        self.assertIn("object ", self.scan_check(report)["detail"])
        self.assertEqual(report["overall_status"], "fail")
        self.assertEqual(self.stamps(doc_id), {"qa_warning"})

    def test_a_clean_pdf_is_verified(self):
        doc_id = self.signed_off("temiz.pdf", pdf_bytes())
        report = self.qa(doc_id, "pdf")
        self.assertEqual(report["overall_status"], "pass", report["checks"])
        self.assertEqual(self.stamps(doc_id), {"redacted_verified"})


# The scanned receipt has no text layer and tesseract is not a test dependency,
# so its OCR is supplied the way a reviewer's corrected OCR is: as the run
# payload, laid out as the page reads (samples/documents/scanned_receipt.pdf).
RECEIPT_LINES = (
    "SYNTHETIC SAMPLE / KURGUSAL BELGE",
    "Recipient: Mira Fableton",
    "Organisation: PAPER COMET EXAMPLE LTD.",
    "Email: mira.fableton@example.com",
    "Address: 14 Imaginary Lane, Cloudborough, Fictionland",
    "Date: 12 September 2026",
    "Amount: GBP 750",
    "TCKN: 88356494872",
    "VKN: 3785737318",
)


def receipt_ocr_payload() -> dict:
    tokens = []
    for row, line in enumerate(RECEIPT_LINES):
        x = 0.08
        y = 0.1 + 0.04 * row
        for word in line.split():
            width = 0.012 * len(word)
            tokens.append({"text": word, "x0": x, "y0": y, "x1": x + width, "y1": y + 0.02, "confidence": 0.95})
            x += width + 0.01
    return {"pages": [{"page_number": 1, "text": "\n".join(RECEIPT_LINES), "confidence": 0.95, "tokens": tokens}]}


class BundledSamplesStillExportTests(ExportHarness):
    """The precision guard: approve-all and sign-off, and every bundled sample still exports.

    A refusal here is a false hit, not something to loosen the matching for.
    """

    def test_every_bundled_docx_sample_exports(self):
        samples = sorted(SAMPLES_DIR.glob("*.docx"))
        self.assertGreaterEqual(len(samples), 5)
        for path in samples:
            with self.subTest(sample=path.name):
                doc_id = self.signed_off(path.name, path.read_bytes())
                self.assertGreater(len(self.approved_findings(doc_id)), 0)
                response = self.export(doc_id)
                self.assertEqual(response.status_code, 200, response.get_data()[:400])
                self.assertEqual(self.qa(doc_id)["overall_status"], "pass")

    def test_reviewer_added_short_names_export(self):
        # "Ali" is inside "Calibri" and "schemaLibrary", "Ela" inside ".../relationships",
        # "Ece" inside "neCell", in the attribute values of every bundled DOCX.
        for path in sorted(SAMPLES_DIR.glob("*.docx")):
            with self.subTest(sample=path.name):
                doc_id = self.upload(path.name, path.read_bytes())
                for name in SHORT_NAMES:
                    response = self.client.post(f"/api/document/{doc_id}/findings", json={"text": name})
                    self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
                self.review_and_sign_off(doc_id)
                # The 200 is the build path's own scan, with the replacements it wrote, passing.
                response = self.export(doc_id)
                self.assertEqual(response.status_code, 200, response.get_data()[:400])

    def test_the_bundled_scanned_receipt_exports(self):
        doc_id = self.upload("scanned_receipt.pdf", (SAMPLES_DIR / "scanned_receipt.pdf").read_bytes())
        for step, body in (("queue", {}), ("run", receipt_ocr_payload()), ("accept", {})):
            response = self.client.post(f"/api/document/{doc_id}/ocr/{step}", json=body)
            self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.review_and_sign_off(doc_id, pdf=True)
        self.assertGreater(len(self.approved_findings(doc_id)), 0)
        response = self.export(doc_id, "pdf")
        self.assertEqual(response.status_code, 200, response.get_data()[:400])
        self.assertEqual(self.qa(doc_id, "pdf")["overall_status"], "pass")


# Parts Word writes into ordinary files that intake used to leave unread: any one
# of them made extraction Partial, queued the document for OCR and refused
# sign-off until a reviewer pasted its text in by hand. Each fixture puts its
# identifiers in that part alone; the body (BODY) never names them.
OFFICE_RELS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
W15 = "http://schemas.microsoft.com/office/word/2012/wordml"
BIBLIOGRAPHY_NS = "http://schemas.openxmlformats.org/officeDocument/2006/bibliography"
DATASTORE_NS = "http://schemas.openxmlformats.org/officeDocument/2006/customXml"
PACKAGE_RELS = "http://schemas.openxmlformats.org/package/2006/relationships"


def relationship(rel_id: str, rel_type: str, target: str) -> str:
    return f'<Relationship Id="{rel_id}" Type="{OFFICE_RELS}/{rel_type}" Target="{target}"/>'


def relationships_part(*entries: str) -> str:
    return f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="{PACKAGE_RELS}">{"".join(entries)}</Relationships>'


# A comment's author, as Word records it: on the comment, and in word/people.xml
# with the account's presence id. Only people.xml holds it as text.
COMMENT_AUTHOR = "Selin Hayaloğlu"
COMMENT_AUTHOR_EMAIL = "selin.hayaloglu@example.com"
PEOPLE_PARTS = {
    "word/comments.xml": f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:comments xmlns:w="{W}">'
    f'<w:comment w:id="0" w:author="{COMMENT_AUTHOR}" w:date="2026-09-14T10:12:00Z" w:initials="SH">'
    "<w:p><w:r><w:t>Bu madde yeniden kontrol edilmeli.</w:t></w:r></w:p></w:comment></w:comments>",
    "word/people.xml": f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w15:people xmlns:w15="{W15}">'
    f'<w15:person w15:author="{COMMENT_AUTHOR}"><w15:presenceInfo w15:providerId="AD" '
    f'w15:userId="S::{COMMENT_AUTHOR_EMAIL}::7c1d2e3f-4a5b-6c7d-8e9f-0a1b2c3d4e5f"/></w15:person></w15:people>',
}
PEOPLE_RELATIONSHIPS = relationship("rId20", "comments", "comments.xml") + (
    '<Relationship Id="rId21" Type="http://schemas.microsoft.com/office/2011/relationships/people" Target="people.xml"/>'
)
# A source added through References > Citations: Word keeps the bibliography in
# an unbound customXml item, paired with its itemProps descriptor. Its author is
# stored as name parts, surname first (b:Author/b:Author/b:NameList/b:Person).
INTERVIEWEE = "Kemal Uydurmaz"
INTERVIEWEE_EMAIL = "kemal.uydurmaz@example.org"
SOURCE_AUTHOR_FIRST, SOURCE_AUTHOR_LAST = "Feride", "Masalcan"
SOURCE_AUTHOR = f"{SOURCE_AUTHOR_FIRST} {SOURCE_AUTHOR_LAST}"
BIBLIOGRAPHY_PARTS = {
    "customXml/item1.xml": f'<?xml version="1.0" encoding="UTF-8" standalone="no"?>'
    f'<b:Sources xmlns:b="{BIBLIOGRAPHY_NS}" xmlns="{BIBLIOGRAPHY_NS}" SelectedStyle="\\APASixthEditionOfficeOnline.xsl" '
    'StyleName="APA" Version="6"><b:Source><b:Tag>Uyd26</b:Tag><b:SourceType>Interview</b:SourceType>'
    "<b:Guid>{3B2A1C0D-9E8F-4A7B-8C6D-5E4F3A2B1C0D}</b:Guid>"
    f"<b:Author><b:Author><b:NameList><b:Person><b:Last>{SOURCE_AUTHOR_LAST}</b:Last>"
    f"<b:First>{SOURCE_AUTHOR_FIRST}</b:First></b:Person></b:NameList></b:Author></b:Author>"
    f"<b:Title>{INTERVIEWEE} ile görüşme</b:Title><b:Year>2026</b:Year>"
    f"<b:Comments>{INTERVIEWEE_EMAIL}</b:Comments></b:Source></b:Sources>",
    "customXml/itemProps1.xml": f'<?xml version="1.0" encoding="UTF-8" standalone="no"?>'
    f'<ds:datastoreItem ds:itemID="{{8F7E6D5C-4B3A-4291-8E7D-6C5B4A392817}}" xmlns:ds="{DATASTORE_NS}">'
    f'<ds:schemaRefs><ds:schemaRef ds:uri="{BIBLIOGRAPHY_NS}"/></ds:schemaRefs></ds:datastoreItem>',
    "customXml/_rels/item1.xml.rels": relationships_part(relationship("rId1", "customXmlProps", "itemProps1.xml")),
}
BIBLIOGRAPHY_RELATIONSHIPS = relationship("rId22", "customXml", "../customXml/item1.xml")
# A file saved from a SharePoint library: the content-type schema item, a stand-in
# for the one SharePoint writes, full of English field labels -- and one
# property value that names a person.
CT_NS = "http://schemas.microsoft.com/office/2006/metadata/contentType"
MA_NS = "http://schemas.microsoft.com/office/2006/metadata/properties/metaAttributes"
XSD_NS = "http://www.w3.org/2001/XMLSchema"
MATTER_OWNER_EMAIL = "leyla.kurmaca@example.com"
SHAREPOINT_FIELDS = (
    ("TaxCatchAll", "Taxonomy Catch All Column", "Select one or more values from the list."),
    ("Author", "Created By", "The person who created this item in the library."),
    ("ContentType", "Content Type", "The type of content that is stored in this item."),
    ("MatterOwner", "Matter Owner", f"Contact {MATTER_OWNER_EMAIL} for any question about this matter."),
)
SHAREPOINT_PARTS = {
    "customXml/item2.xml": f'<?xml version="1.0" encoding="UTF-8" standalone="no"?>'
    f'<ct:contentTypeSchema xmlns:ct="{CT_NS}" xmlns:ma="{MA_NS}" ct:_="" ma:_="" ma:contentTypeName="Document" '
    'ma:contentTypeID="0x010100A1B2C3D4E5F60718293A4B5C6D7E8F90" ma:contentTypeVersion="4" '
    'ma:contentTypeDescription="Create a new document." ma:contentTypeScope="" '
    'ma:versionID="5f4e3d2c1b0a99887766554433221100">'
    f'<xsd:schema xmlns:xsd="{XSD_NS}" targetNamespace="http://schemas.microsoft.com/office/2006/metadata/properties" '
    'ma:root="true" ma:fieldsID="0a1b2c3d4e5f60718293a4b5c6d7e8f9"><xsd:element name="properties"><xsd:complexType>'
    "<xsd:sequence><xsd:element name=\"documentManagement\"><xsd:complexType><xsd:all>"
    + "".join(
        f'<xsd:element name="{name}" ma:index="{index}" nillable="true" ma:displayName="{label}" '
        f'ma:description="{description}" ma:internalName="{name}"/>'
        for index, (name, label, description) in enumerate(SHAREPOINT_FIELDS, start=8)
    )
    + "</xsd:all></xsd:complexType></xsd:element></xsd:sequence></xsd:complexType></xsd:element></xsd:schema>"
    "</ct:contentTypeSchema>",
    "customXml/itemProps2.xml": f'<?xml version="1.0" encoding="UTF-8" standalone="no"?>'
    f'<ds:datastoreItem ds:itemID="{{2C3D4E5F-6A7B-4C8D-9E0F-1A2B3C4D5E6F}}" xmlns:ds="{DATASTORE_NS}">'
    f'<ds:schemaRefs><ds:schemaRef ds:uri="{CT_NS}"/></ds:schemaRefs></ds:datastoreItem>',
    "customXml/_rels/item2.xml.rels": relationships_part(relationship("rId1", "customXmlProps", "itemProps2.xml")),
}
SHAREPOINT_RELATIONSHIPS = relationship("rId24", "customXml", "../customXml/item2.xml")
# A Quick Part saved with the file: Word stores building blocks in a glossary
# document with its own styles, settings and fonts.
SIGNATORY = "Nurten Masalcı"
SIGNATORY_EMAIL = "nurten.masalci@example.net"
GLOSSARY_PARTS = {
    "word/glossary/document.xml": f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    f'<w:glossaryDocument xmlns:w="{W}"><w:docParts><w:docPart><w:docPartPr><w:name w:val="İmza Bloğu"/>'
    '<w:category><w:name w:val="Genel"/><w:gallery w:val="quickParts"/></w:category>'
    '<w:guid w:val="{5D4C3B2A-1F0E-4D9C-8B7A-695847362514}"/></w:docPartPr><w:docPartBody>'
    f"<w:p><w:r><w:t>Av. {SIGNATORY}</w:t></w:r></w:p><w:p><w:r><w:t>{SIGNATORY_EMAIL}</w:t></w:r></w:p>"
    "</w:docPartBody></w:docPart></w:docParts></w:glossaryDocument>",
    "word/glossary/styles.xml": f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:styles xmlns:w="{W}">'
    '<w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/></w:style></w:styles>',
    "word/glossary/settings.xml": f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:settings xmlns:w="{W}"/>',
    "word/glossary/webSettings.xml": f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:webSettings xmlns:w="{W}"/>',
    "word/glossary/fontTable.xml": f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:fonts xmlns:w="{W}">'
    '<w:font w:name="Calibri"/></w:fonts>',
    "word/glossary/_rels/document.xml.rels": relationships_part(
        relationship("rId1", "styles", "styles.xml"),
        relationship("rId2", "settings", "settings.xml"),
        relationship("rId3", "webSettings", "webSettings.xml"),
        relationship("rId4", "fontTable", "fontTable.xml"),
    ),
}
GLOSSARY_RELATIONSHIPS = relationship("rId23", "glossaryDocument", "glossary/document.xml")
WORD_AUTHORED_FAMILIES = {
    "people": (PEOPLE_PARTS, PEOPLE_RELATIONSHIPS, (COMMENT_AUTHOR, COMMENT_AUTHOR_EMAIL)),
    "bibliography": (BIBLIOGRAPHY_PARTS, BIBLIOGRAPHY_RELATIONSHIPS, (INTERVIEWEE, INTERVIEWEE_EMAIL, SOURCE_AUTHOR)),
    "glossary": (GLOSSARY_PARTS, GLOSSARY_RELATIONSHIPS, (SIGNATORY, SIGNATORY_EMAIL)),
    "sharepoint": (SHAREPOINT_PARTS, SHAREPOINT_RELATIONSHIPS, (MATTER_OWNER_EMAIL,)),
}


def word_authored_docx(*families: str, parts: dict[str, str] | None = None, body: str = BODY) -> bytes:
    """BODY plus the Word-authored parts of each named family, related as Word relates them."""
    chosen = [WORD_AUTHORED_FAMILIES[family] for family in families]
    return docx_bytes(
        parts={name: xml for family_parts, _, _ in chosen for name, xml in family_parts.items()} | (parts or {}),
        relationships="".join(rels for _, rels, _ in chosen),
        body=body,
    )


class WordAuthoredPartsExportTests(ExportHarness):
    """people.xml, customXml items and the glossary are read at intake, so these files sign off and export.

    Detection reads their text; the export's own scrubbing and the output scan
    stay the backstop, and must agree with what detection found.
    """

    def document_row(self, doc_id: int) -> tuple:
        (row,) = self.rows(
            "SELECT extraction_status, extraction_warning, ocr_status, review_status FROM documents WHERE id = ?", doc_id
        )
        return row

    def detected_samples(self, doc_id: int) -> set[str]:
        return {sample for (sample,) in self.rows("SELECT sample FROM privacy_findings WHERE document_id = ?", doc_id)}

    def test_intake_reads_each_part_and_detects_what_only_it_holds(self):
        for family, (_, _, samples) in WORD_AUTHORED_FAMILIES.items():
            with self.subTest(family=family):
                doc_id = self.upload(f"{family}.docx", word_authored_docx(family))
                self.assertEqual(self.document_row(doc_id), ("Complete", None, "not_required", "pending_review"))
                for sample in samples:
                    self.assertNotIn(sample, BODY)
                    self.assertIn(sample, self.detected_samples(doc_id))

    def test_a_sharepoint_schema_item_does_not_decide_the_language(self):
        # Its English labels are detection input, not the document's language:
        # the Turkish body still reads as Turkish, and the item's email is still found.
        doc_id = self.upload("sharepoint.docx", word_authored_docx("sharepoint"))
        ((profile,),) = self.rows("SELECT privacy_profile FROM documents WHERE id = ?", doc_id)
        coverage = json.loads(profile)["language_coverage"]
        self.assertEqual((coverage["language"], coverage["supported"]), (TURKISH, True))
        self.assertIn(MATTER_OWNER_EMAIL, self.detected_samples(doc_id))

    def test_each_part_signs_off_and_exports_with_no_approved_item_left(self):
        for family, (family_parts, _, samples) in WORD_AUTHORED_FAMILIES.items():
            with self.subTest(family=family):
                # review_and_sign_off asserts mark_redacted and approve are both 200.
                doc_id = self.signed_off(f"{family}.docx", word_authored_docx(family))
                approved = self.approved_findings(doc_id)
                self.assertTrue(set(samples) <= {row["sample"] for row in approved})
                response = self.export(doc_id)
                self.assertEqual(response.status_code, 200, response.get_data()[:400])
                output = response.get_data()
                self.assertEqual(scan_docx(output, approved), [])
                with ZipFile(BytesIO(output)) as archive:
                    exported = {name: archive.read(name).decode("utf-8") for name in family_parts}
                for name, xml in exported.items():
                    for sample in samples:
                        self.assertNotIn(sample, xml, name)
                self.assertEqual(self.qa(doc_id)["overall_status"], "pass")

    def test_every_family_together_signs_off_and_exports(self):
        doc_id = self.signed_off("word.docx", word_authored_docx(*WORD_AUTHORED_FAMILIES))
        response = self.export(doc_id)
        self.assertEqual(response.status_code, 200, response.get_data()[:400])
        self.assertEqual(scan_docx(response.get_data(), self.approved_findings(doc_id)), [])

    def test_any_other_unread_part_still_makes_extraction_partial(self):
        # The families are read, not allow-listed: a part beside them that
        # nothing reads -- including a glossary part with no main-document
        # namesake -- still warns, by name, and alone.
        for stray in ("word/charts/chart1.xml", "word/glossary/charts/chart1.xml", "customXml/item1.bin"):
            with self.subTest(stray=stray):
                path = self.tmp_path / "stray.docx"
                path.write_bytes(word_authored_docx(*WORD_AUTHORED_FAMILIES, parts={stray: f"<c><v>{NAME}</v></c>"}))
                text, warning = extract_text(path)
                self.assertIsNotNone(warning)
                self.assertIn(f"1 document part(s) were not scanned for sensitive data: {stray}.", warning)
                self.assertEqual(analyze_privacy(path.name, text, warning)["extraction_status"]["status"], "Partial")

    def test_a_body_finding_keeps_its_place_when_people_xml_repeats_its_text(self):
        # Extraction now holds the author twice (body, then people.xml). The
        # redactor lays people.xml out the same way, so the body finding's
        # occurrence still resolves to the body instead of falling back to
        # redacting every match.
        path = self.tmp_path / "repeat.docx"
        path.write_bytes(word_authored_docx("people", body=f"{BODY} Dilekçeyi Av. {COMMENT_AUTHOR} imzalamıştır."))
        text, warning = extract_text(path)
        self.assertIsNone(warning)
        found_at = Occurrence.at(text, COMMENT_AUTHOR, text.index(COMMENT_AUTHOR))
        self.assertEqual(found_at, Occurrence(0, 2))
        with ZipFile(path) as archive:
            parts = anchor_parts(archive, [RedactionTarget(COMMENT_AUTHOR, "[PERSON_1]", found_at=found_at)])
        self.assertEqual(parts["word/document.xml"][0][0].found_at, Occurrence(0, 1))
        self.assertIsNone(parts["word/people.xml"][0][0].found_at)


# An Office add-in's own parts, as Word puts them in the document snapshot the
# add-in sends: the task pane list, the add-in's manifest reference, and the
# settings the add-in wrote (``Office.settings``, stored as JSON-encoded
# strings in we:property values). Only a property value holds the contact.
WE_NS = "http://schemas.microsoft.com/office/webextensions/webextension/2010/11"
WETP_NS = "http://schemas.microsoft.com/office/webextensions/taskpanes/2010/11"
ADDIN_CONTACT = "Zerrin Kurmacagil"
ADDIN_CONTACT_EMAIL = "zerrin.kurmacagil@example.com"
ADDIN_SETTING = escape(f'"Dosya sorumlusu Av. {ADDIN_CONTACT}, {ADDIN_CONTACT_EMAIL}"', {'"': "&quot;"})


def webextension_parts(*properties: tuple[str, str]) -> dict[str, str]:
    """The word/webextensions/ parts of a snapshot, with ``properties`` (name, escaped value) as the add-in's settings."""
    return {
        "word/webextensions/taskpanes.xml": f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<wetp:taskpanes xmlns:wetp="{WETP_NS}"><wetp:taskpane dockstate="right" visibility="1" width="350" row="4">'
        f'<wetp:webextensionref xmlns:r="{R}" r:id="rId1"/></wetp:taskpane></wetp:taskpanes>',
        "word/webextensions/_rels/taskpanes.xml.rels": relationships_part(
            '<Relationship Id="rId1" Type="http://schemas.microsoft.com/office/2011/relationships/webextension" '
            'Target="webextension1.xml"/>'
        ),
        "word/webextensions/webextension1.xml": f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<we:webextension xmlns:we="{WE_NS}" id="{{6B1C2D3E-4F5A-4B6C-8D7E-9F0A1B2C3D4E}}">'
        '<we:reference id="7d1e3c5a-2b4f-4e6d-9a8b-0c1d2e3f4a5b" version="1.0.0.0" store="developer" storeType="Registry"/>'
        "<we:alternateReferences/><we:properties>"
        + "".join(f'<we:property name="{name}" value="{value}"/>' for name, value in properties)
        + f'</we:properties><we:bindings/><we:snapshot xmlns:r="{R}"/></we:webextension>',
    }


WEBEXTENSION_PACKAGE_RELATIONSHIP = (
    '<Relationship Id="rId9" Type="http://schemas.microsoft.com/office/2011/relationships/webextensiontaskpanes" '
    'Target="word/webextensions/taskpanes.xml"/>'
)
WEBEXTENSION_CONTENT_TYPES = (
    '<Override PartName="/word/webextensions/taskpanes.xml" ContentType="application/vnd.ms-office.webextensiontaskpanes+xml"/>'
    '<Override PartName="/word/webextensions/webextension1.xml" ContentType="application/vnd.ms-office.webextension+xml"/>'
)
AUTO_SHOW_TASKPANE = ("Office.AutoShowTaskpaneWithDocument", "true")


def webextension_docx(*properties: tuple[str, str], parts: dict[str, str] | None = None, body: str = BODY) -> bytes:
    """BODY in a snapshot that carries the add-in's parts, related and typed as Word writes them."""
    return docx_bytes(
        parts=webextension_parts(*properties) | (parts or {}),
        body=body,
        package_relationships=WEBEXTENSION_PACKAGE_RELATIONSHIP,
        content_types=WEBEXTENSION_CONTENT_TYPES,
    )


class WordAddInPartsExportTests(ExportHarness):
    """word/webextensions/ parts are read at intake like the other data parts, so the snapshot signs off and exports."""

    document_row = WordAuthoredPartsExportTests.document_row
    detected_samples = WordAuthoredPartsExportTests.detected_samples

    def test_the_add_in_parts_do_not_make_intake_partial(self):
        # The trial's snapshot: the add-in wrote nothing but Word's own setting.
        doc_id = self.upload("addin.docx", webextension_docx(AUTO_SHOW_TASKPANE))
        self.assertEqual(self.document_row(doc_id), ("Complete", None, "not_required", "pending_review"))

    def test_text_only_a_property_holds_is_detected(self):
        doc_id = self.upload("addin.docx", webextension_docx(AUTO_SHOW_TASKPANE, ("matterContact", ADDIN_SETTING)))
        self.assertEqual(self.document_row(doc_id), ("Complete", None, "not_required", "pending_review"))
        for sample in (ADDIN_CONTACT, ADDIN_CONTACT_EMAIL):
            self.assertNotIn(sample, BODY)
            self.assertIn(sample, self.detected_samples(doc_id))

    def test_the_add_in_parts_do_not_decide_the_language(self):
        doc_id = self.upload("addin.docx", webextension_docx(AUTO_SHOW_TASKPANE, ("matterContact", ADDIN_SETTING)))
        ((profile,),) = self.rows("SELECT privacy_profile FROM documents WHERE id = ?", doc_id)
        coverage = json.loads(profile)["language_coverage"]
        self.assertEqual((coverage["language"], coverage["supported"]), (TURKISH, True))

    def test_the_snapshot_signs_off_and_exports_with_no_approved_item_left(self):
        for properties in ((AUTO_SHOW_TASKPANE,), (AUTO_SHOW_TASKPANE, ("matterContact", ADDIN_SETTING))):
            with self.subTest(properties=[name for name, _ in properties]):
                # review_and_sign_off asserts mark_redacted and approve are both 200.
                doc_id = self.signed_off("addin.docx", webextension_docx(*properties))
                approved = self.approved_findings(doc_id)
                response = self.export(doc_id)
                self.assertEqual(response.status_code, 200, response.get_data()[:400])
                output = response.get_data()
                self.assertEqual(scan_docx(output, approved), [])
                with ZipFile(BytesIO(output)) as archive:
                    exported = {name: archive.read(name).decode("utf-8") for name in webextension_parts()}
                for name, xml in exported.items():
                    for sample in (ADDIN_CONTACT, ADDIN_CONTACT_EMAIL):
                        self.assertNotIn(sample, xml, name)
                self.assertEqual(self.qa(doc_id)["overall_status"], "pass")

    def test_the_snapshot_signs_off_beside_every_other_data_part(self):
        doc_id = self.signed_off(
            "word.docx",
            word_authored_docx(
                *WORD_AUTHORED_FAMILIES, parts=webextension_parts(AUTO_SHOW_TASKPANE, ("matterContact", ADDIN_SETTING))
            ),
        )
        response = self.export(doc_id)
        self.assertEqual(response.status_code, 200, response.get_data()[:400])
        self.assertEqual(scan_docx(response.get_data(), self.approved_findings(doc_id)), [])

    def test_any_other_unread_part_still_makes_extraction_partial(self):
        # Only the task pane list and the add-in parts are read: anything else
        # under word/webextensions/, or anywhere else, still warns by name.
        for stray in ("word/charts/chart1.xml", "word/webextensions/media/image1.xml", "word/webextensions/webextension1.bin"):
            with self.subTest(stray=stray):
                path = self.tmp_path / "stray.docx"
                path.write_bytes(webextension_docx(AUTO_SHOW_TASKPANE, parts={stray: f"<c><v>{NAME}</v></c>"}))
                text, warning = extract_text(path)
                self.assertIsNotNone(warning)
                self.assertIn(f"1 document part(s) were not scanned for sensitive data: {stray}.", warning)
                self.assertEqual(analyze_privacy(path.name, text, warning)["extraction_status"]["status"], "Partial")


TURKISH_NAME = "Şükrü Çağlar"


def pdf_with_utf8_page_metadata(name: str = TURKISH_NAME) -> bytes:
    """A text PDF naming ``name``, whose PAGE carries a UTF-8 XMP stream holding it too.

    The redactor clears the document's XMP; a page's own /Metadata stream is
    an ordinary object it never looks at.
    """
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_textbox(pymupdf.Rect(72, 72, 520, 400), BODY.replace(NAME, name), fontsize=11, fontname="helv")
    xref = doc.get_new_xref()
    doc.update_object(xref, "<</Type /Metadata /Subtype /XML>>")
    doc.update_stream(xref, f'<x:xmpmeta xmlns:x="adobe:ns:meta/"><dc:creator>{name}</dc:creator></x:xmpmeta>'.encode("utf-8"))
    doc.xref_set_key(page.xref, "Metadata", f"{xref} 0 R")
    data = doc.tobytes()
    doc.close()
    return data


def pdf_with_catalog_string(encoded_hex: str) -> bytes:
    """A blank PDF whose catalog /PieceInfo carries one hex string, outside every content stream."""
    doc = pymupdf.open()
    doc.new_page()
    doc.xref_set_key(doc.pdf_catalog(), "PieceInfo", f"<</X <</Private <{encoded_hex}>>>>>")
    data = doc.tobytes()
    doc.close()
    return data


class EncodingTests(unittest.TestCase):
    """PDF strings and streams are searched in every encoding they may carry text in."""

    def objects_found(self, data: bytes, sample: str = TURKISH_NAME) -> set[str]:
        return {
            leak["part"]
            for leak in scan_pdf(data, [finding(sample, 5, source="reviewer")])
            if leak["finding_id"] is not None and leak["part"].startswith("object ")
        }

    def test_a_utf8_turkish_name_in_a_page_metadata_stream_is_found(self):
        self.assertTrue(self.objects_found(pdf_with_utf8_page_metadata()))

    def test_an_unmarked_utf8_object_string_is_found(self):
        self.assertTrue(self.objects_found(pdf_with_catalog_string(TURKISH_NAME.encode("utf-8").hex())))

    def test_a_bom_marked_utf16_hex_object_string_is_found(self):
        self.assertTrue(self.objects_found(pdf_with_catalog_string("FEFF" + TURKISH_NAME.encode("utf-16-be").hex())))


class DeduplicationTests(unittest.TestCase):
    """Rows of the same entity are searched once and every row is still reported."""

    def word_document(self) -> bytes:
        run = (
            '<w:r w:rsidR="00A1B2C3"><w:rPr><w:rFonts w:ascii="Calibri" w:hAnsi="Calibri"/><w:lang w:val="tr-TR"/></w:rPr>'
            '<w:t xml:space="preserve">İşbu sözleşme taraflar arasında imzalanmıştır. </w:t></w:r>'
        )
        paragraph = f'<w:p w:rsidR="00112233" w14:paraId="1A2B3C4D"><w:pPr><w:jc w:val="both"/></w:pPr>{run * 4}</w:p>'
        extra = paragraph * 40 * 30  # thirty pages
        return docx_bytes(extra_body=extra, parts=DOC_VARIABLE).replace(b"xmlns:wp=", b'xmlns:w14="urn:w14" xmlns:wp=', 1)

    def rows(self, count: int) -> list[dict]:
        samples = [(NAME, "natural_person_name"), (EMAIL, "email_address"), ("DENİZ KURGUSAL", "natural_person_name")]
        return [finding(samples[i % 3][0], i, samples[i % 3][1], source="reviewer") for i in range(count)]

    def test_rows_with_the_same_matching_behaviour_are_searched_once(self):
        data = self.word_document()
        calls = []
        real = SampleMatcher.found_as_word_in

        def counting(matcher, text):
            calls.append(1)
            return real(matcher, text)

        with mock.patch.object(SampleMatcher, "found_as_word_in", counting):
            few = scan_docx(data, self.rows(3))
            few_calls = len(calls)
            calls.clear()
            many = scan_docx(data, self.rows(750))
        # "DENİZ KURGUSAL" folds and patterns like "Deniz Kurgusal": two searches per part, whatever the row count.
        self.assertEqual(len(calls), few_calls)
        # Every row is still reported where its entity survives (the document variable).
        self.assertEqual({leak["finding_id"] for leak in few if leak["part"] == "word/settings.xml"}, {0, 2})
        self.assertEqual(
            {leak["finding_id"] for leak in many if leak["part"] == "word/settings.xml"},
            {i for i in range(750) if i % 3 != 1},
        )

    def test_a_thirty_page_document_with_750_rows_scans_quickly(self):
        data = self.word_document()
        started = time.monotonic()
        scan_docx(data, self.rows(750))
        self.assertLess(time.monotonic() - started, 2)


class MatchingScopeTests(unittest.TestCase):
    """What is matched, and how, in the places a reviewer-added short name collides with Word's own text."""

    def parts(self, data: bytes, findings) -> set[str]:
        return {leak["part"] for leak in scan_docx(data, findings) if leak["finding_id"] is not None}

    def test_the_xml_declaration_is_not_searched(self):
        tan = [finding("Tan", 6, source="reviewer")]
        prolog = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><a/>'
        self.assertNotIn("word/extra.xml", self.parts(docx_bytes(body="", parts={"word/extra.xml": prolog}), tan))
        # Any other processing instruction is still the document's and still searched.
        note = '<?xml version="1.0"?><?note Tan Bey?><a/>'
        self.assertIn("word/extra.xml", self.parts(docx_bytes(body="", parts={"word/extra.xml": note}), tan))

    def test_part_names_match_as_whole_words(self):
        for name in ("Er", "Dal", "Nail"):
            with self.subTest(name=name):
                short = [finding(name, 6, source="reviewer")]
                parts = self.parts(docx_bytes(body="", parts={"word/endnotes.xml": "<a/>", "word/stylesWithEffects.xml": "<a/>",
                                                           "word/media/thumbnail.xml": "<a/>"}), short)
                self.assertNotIn("package member names", parts)
        dal = [finding("Dal", 6, source="reviewer")]
        self.assertIn("package member names", self.parts(docx_bytes(body="", parts={"docProps/dal.xml": "<a/>"}), dal))

    def test_a_bare_surname_in_a_text_node_is_matched_capitalised_and_whole(self):
        surname = [finding("Kale", 7)]  # a detector bare surname: the redactor's standalone predicate
        self.assertEqual(self.parts(docx_bytes(body="Sayın avukat kalemine teslim etti.", parts={}), surname), set())
        self.assertEqual(self.parts(docx_bytes(body="Sayın avukat Kalemine teslim etti.", parts={}), surname), set())
        self.assertIn("word/document.xml", self.parts(docx_bytes(body="Sayın Kale teslim etti.", parts={}), surname))

    def test_a_lowercase_part_name_holding_a_bare_surname_is_reported_by_position(self):
        surname = [finding("Kale", 7)]
        leaks = scan_docx(docx_bytes(body="", parts={"docProps/kale.xml": "<a>Sayın Kale</a>"}), surname)
        self.assertTrue(leaks)
        self.assertNotIn("kale", json.dumps(leaks).casefold())


def refused_names_on_bundled_samples(names) -> dict[str, list[str]]:
    """``{sample: [names refused]}`` after approve-all plus every name as a reviewer-added finding, for each bundled DOCX."""
    refused = {}
    for path in sorted(SAMPLES_DIR.glob("*.docx")):
        text, warning = extract_text(path)
        detected = [
            dict(row, id=1000 + index, source=row.get("source") or "detector")
            for index, row in enumerate(analyze_privacy(path.name, text, warning)["risk_map"])
        ]
        added = [finding(name, index, "manual_sensitive_text", source="reviewer") for index, name in enumerate(names)]
        targets = [
            RedactionTarget(row["sample"], row.get("placeholder") or "[REDACTED]", row["category"] == "natural_person_name" and len(row["sample"].split()) == 1)
            for row in detected
        ] + [RedactionTarget(name, "[MANUAL_SENSITIVE_TEXT_REDACTED]") for name in names]
        leaks = scan_docx(redact_docx(path, targets), [*detected, *added], {target.replacement for target in targets})
        names_refused = sorted({names[leak["finding_id"]] for leak in leaks if leak["finding_id"] in range(len(names))})
        if names_refused:
            refused[path.name] = names_refused
    return refused


class PlaceholderAndValueExceptionTests(unittest.TestCase):
    """The export's own placeholder tokens, and two exact Word values, are not matched; nothing else changes."""

    def parts(self, parts: dict, sample: str, replacements=(), body: str = "") -> set[str]:
        leaks = scan_docx(docx_bytes(body=body, parts=parts), [finding(sample, 6, source="reviewer")], replacements)
        return {leak["part"] for leak in leaks if leak["finding_id"] is not None}

    def test_every_common_short_name_exports_on_every_bundled_docx(self):
        # All of them at once, each its own reviewer-added finding: more placeholders written than any one alone.
        for path in sorted(SAMPLES_DIR.glob("*.docx")):
            with self.subTest(sample=path.name):
                text, warning = extract_text(path)
                detected = [
                    dict(row, id=1000 + index, source=row.get("source") or "detector")
                    for index, row in enumerate(analyze_privacy(path.name, text, warning)["risk_map"])
                ]
                added = [finding(name, index, "manual_sensitive_text", source="reviewer") for index, name in enumerate(COMMON_SHORT_NAMES)]
                targets = [
                    RedactionTarget(row["sample"], row.get("placeholder") or "[REDACTED]", row["category"] == "natural_person_name" and len(row["sample"].split()) == 1)
                    for row in detected
                ] + [RedactionTarget(name, "[MANUAL_SENSITIVE_TEXT_REDACTED]") for name in COMMON_SHORT_NAMES]
                output = redact_docx(path, targets)
                leaks = scan_docx(output, [*detected, *added], {target.replacement for target in targets})
                refused = sorted({COMMON_SHORT_NAMES[leak["finding_id"]] for leak in leaks if leak["finding_id"] in range(len(COMMON_SHORT_NAMES))})
                self.assertEqual(refused, [], [leak for leak in leaks if leak["finding_id"] in range(len(COMMON_SHORT_NAMES))][:5])

    def test_only_an_exact_placeholder_token_this_export_wrote_is_removed(self):
        extra = lambda text: {"word/extra.xml": f"<a><r>{escape(text)}</r></a>"}
        self.assertEqual(self.parts(extra("Hizmet [PERSON_1] imzaladı."), "Er", {"[PERSON_1]"}), set())
        self.assertIn("word/extra.xml", self.parts(extra("[PERSON_1] Ali"), "Ali", {"[PERSON_1]"}))
        self.assertIn("word/extra.xml", self.parts(extra("[PERSON_12]"), "Er", {"[PERSON_1]"}))  # not a token this export wrote
        self.assertIn("word/extra.xml", self.parts(extra("[PERSON_1]"), "Er", ()))  # nothing written, nothing removed
        # A reviewer-typed replacement is not placeholder-shaped, so it is never taken out of what is read.
        self.assertIn("word/extra.xml", self.parts(extra("Ali Bey"), "Ali", {"Ali Bey"}))

    def test_nil_is_skipped_only_as_a_border_value(self):
        border = f'<w:styles xmlns:w="{W}"><w:tblBorders><w:top w:val="nil"/><w:insideH w:val="nil"/></w:tblBorders></w:styles>'
        self.assertEqual(self.parts({"word/styles.xml": border}, "Nil"), set())
        for label, xml in {
            "document variable Nil": f'<w:settings xmlns:w="{W}"><w:docVars><w:docVar w:name="Taraf" w:val="Nil"/></w:docVars></w:settings>',
            "document variable nil": f'<w:settings xmlns:w="{W}"><w:docVars><w:docVar w:name="Taraf" w:val="nil"/></w:docVars></w:settings>',
            "border colour nil": f'<w:styles xmlns:w="{W}"><w:top w:color="nil"/></w:styles>',
            "border value Nil Yılmaz": f'<w:styles xmlns:w="{W}"><w:top w:val="Nil Yılmaz"/></w:styles>',
            "non-Word element and attribute": '<x:styles xmlns:x="urn:example:x"><x:top x:val="nil"/></x:styles>',
            "non-Word attribute on a Word border": f'<w:styles xmlns:w="{W}" xmlns:x="urn:example:x"><w:top x:val="nil"/></w:styles>',
            "Word attribute on a non-Word element": f'<x:styles xmlns:x="urn:example:x" xmlns:w="{W}"><x:top w:val="nil"/></x:styles>',
        }.items():
            with self.subTest(label):
                part = "word/settings.xml" if "variable" in label else "word/styles.xml"
                self.assertIn(part, self.parts({part: xml}, "Nil"))

    def test_true_and_false_are_skipped_only_as_app_boolean_properties(self):
        app = lambda inner: {"docProps/app.xml": f'<Properties xmlns="urn:app">{inner}</Properties>'}
        self.assertEqual(self.parts(app("<ScaleCrop>false</ScaleCrop><SharedDoc>false</SharedDoc>"), "Al"), set())
        for label, parts in {
            "core text": {"docProps/core.xml": '<cp:coreProperties xmlns:cp="urn:cp"><dc:creator xmlns:dc="urn:dc">Al</dc:creator></cp:coreProperties>'},
            "custom property false": {"docProps/custom.xml": '<Properties xmlns="urn:custom"><ScaleCrop>false</ScaleCrop></Properties>'},
            "app non-boolean property": app("<Company>false</Company>"),
            "app boolean property holding a name": app("<ScaleCrop>Al</ScaleCrop>"),
            "app text after a boolean": app("<ScaleCrop>true</ScaleCrop>false"),
        }.items():
            with self.subTest(label):
                (part,) = parts
                self.assertIn(part, self.parts(parts, "Al"))


class XmlDeclarationTests(unittest.TestCase):
    """Only the prolog at offset 0 is skipped, and only its version, encoding and standalone."""

    def parts(self, xml: str) -> set[str]:
        leaks = scan_docx(docx_bytes(body="", parts={"word/extra.xml": xml}), [finding("Tan", 6, source="reviewer")])
        return {leak["part"] for leak in leaks if leak["finding_id"] is not None}

    def test_the_prolog_is_skipped_in_either_quoting(self):
        for prolog in ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>', "<?xml version='1.0' encoding='utf-8' standalone='no'?>"):
            with self.subTest(prolog):
                self.assertEqual(self.parts(prolog + "<a/>"), set())

    def test_a_name_in_any_other_xml_instruction_is_read(self):
        for label, xml in {
            "mid-part declaration": '<a/><?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
            "mid-part instruction": "<a/><?xml Tan Bey?>",
            "declaration after whitespace": ' <?xml version="1.0" standalone="yes"?><a/>',
            "malformed declaration": "<?xml Tan Bey?><a/>",
            "declaration without a version": '<?xml standalone="yes"?><a/>',
            "another pseudo-attribute": '<?xml version="1.0" encoding="UTF-8" note="Tan Bey"?><a/>',
        }.items():
            with self.subTest(label):
                self.assertIn("word/extra.xml", self.parts(xml))


class SystemValueTests(unittest.TestCase):
    """A font or enumeration name is skipped only as the exact system value of its own attribute."""

    def parts(self, part: str, xml: str, sample: str) -> set[str]:
        leaks = scan_docx(docx_bytes(body="", parts={part: xml}), [finding(sample, 6, source="reviewer")])
        return {leak["part"] for leak in leaks if leak["finding_id"] is not None}

    def test_each_is_skipped_on_the_attribute_word_writes_it_on(self):
        theme = '<a:theme xmlns:a="urn:a"><a:fontScheme><a:majorFont><a:latin typeface="Calibri Light"/><a:font script="{0}" typeface="{1}"/>'
        for sample, part, xml in (
            ("Shruti", "word/theme/theme1.xml", theme.format("Gujr", "Shruti") + "</a:majorFont></a:fontScheme></a:theme>"),
            ("Light", "word/theme/theme1.xml", theme.format("Khmr", "MoolBoran") + "</a:majorFont></a:fontScheme></a:theme>"),
            ("Light", "word/styles.xml", f'<w:styles xmlns:w="{W}"><w:latentStyles><w:lsdException w:name="Light Grid Accent 1"/>'
             '<w:lsdException w:name="Grid Table 1 Light"/></w:latentStyles><w:style w:type="table" w:styleId="LightList">'
             '<w:name w:val="Light List"/></w:style><w:style w:type="table" w:styleId="ListTable1Light-Accent6">'
             '<w:name w:val="List Table 1 Light Accent 6"/></w:style></w:styles>'),
            ("Light", "word/document.xml", f'<w:document xmlns:w="{W}"><w:tblPr><w:tblStyle w:val="LightGrid-Accent1"/></w:tblPr>'
             '<w:pPr><w:pStyle w:val="GridTable1Light"/></w:pPr></w:document>'),
            ("Light", "word/styles.xml", f'<w:styles xmlns:w="{W}"><w:style w:styleId="LightShading-Accent2"><w:basedOn w:val="LightShading"/>'
             '<w:next w:val="LightList"/><w:link w:val="ListTable1Light-Accent3"/><w:rPr><w:rStyle w:val="LightGrid"/></w:rPr></w:style></w:styles>'),
            ("Roman", "word/fontTable.xml", f'<w:fonts xmlns:w="{W}"><w:font w:name="Times New Roman"><w:family w:val="roman"/></w:font></w:fonts>'),
            ("Roman", "word/numbering.xml", f'<w:numbering xmlns:w="{W}"><w:lvl><w:numFmt w:val="lowerRoman"/></w:lvl>'
             '<w:lvl><w:numFmt w:val="upperRoman"/></w:lvl></w:numbering>'),
            ("Roman", "word/document.xml", f'<w:document xmlns:w="{W}"><w:sectPr><w:pgNumType w:fmt="lowerRoman"/></w:sectPr></w:document>'),
            ("Mangal", "word/styles.xml", f'<w:styles xmlns:w="{W}"><w:rFonts w:ascii="Mangal" w:cs="Mangal"/></w:styles>'),
            ("Asia", "word/styles.xml", f'<w:styles xmlns:w="{W}"><w:rFonts w:eastAsiaTheme="minorEastAsia"/></w:styles>'),
            ("Auto", "word/styles.xml", f'<w:styles xmlns:w="{W}"><w:shd w:color="auto" w:fill="auto"/><w:spacing w:lineRule="auto"/>'
             '<w:color w:val="auto"/><w:top w:color="auto"/><w:tblW w:type="auto"/></w:styles>'),
            ("Single", "word/styles.xml", f'<w:styles xmlns:w="{W}"><w:u w:val="single"/><w:bottom w:val="single"/></w:styles>'),
            ("Double", "word/styles.xml", f'<w:styles xmlns:w="{W}"><w:top w:val="double"/></w:styles>'),
            ("Center", "word/styles.xml", f'<w:styles xmlns:w="{W}"><w:jc w:val="center"/><w:tab w:val="center"/><w:vAlign w:val="center"/></w:styles>'),
            ("Left", "word/numbering.xml", f'<w:numbering xmlns:w="{W}"><w:lvlJc w:val="left"/></w:numbering>'),
            ("Right", "word/styles.xml", f'<w:styles xmlns:w="{W}"><w:tab w:val="right"/></w:styles>'),
        ):
            with self.subTest(sample):
                self.assertEqual(self.parts(part, xml, sample), set())

    def test_the_same_word_anywhere_else_is_read(self):
        settings = lambda value: f'<w:settings xmlns:w="{W}"><w:docVars><w:docVar w:name="Taraf" w:val="{value}"/></w:docVars></w:settings>'
        for label, (sample, part, xml) in {
            "font name in a document variable": ("Shruti", "word/settings.xml", settings("Shruti")),
            "enum word in a document variable": ("Center", "word/settings.xml", settings("center")),
            "number format in a document variable": ("Roman", "word/settings.xml", settings("lowerRoman")),
            "font name on an enum attribute": ("Shruti", "word/styles.xml", f'<w:styles xmlns:w="{W}"><w:jc w:val="Shruti"/></w:styles>'),
            "enum word on a font attribute": ("Auto", "word/fontTable.xml", f'<w:fonts xmlns:w="{W}"><w:font w:name="auto"/></w:fonts>'),
            "a person on a font attribute": ("Deniz Kurgusal", "word/theme/theme1.xml", f'<a:font xmlns:a="urn:a" typeface="{NAME}"/>'),
            "font name in a style's name": ("Roman", "word/styles.xml", f'<w:styles xmlns:w="{W}"><w:name w:val="Roman Numerals"/></w:styles>'),
            "font name in a text node": ("Mangal", "word/extra.xml", "<a>Sayın Mangal</a>"),
            "font name in a document property": ("Mangal", "docProps/core.xml", '<cp:coreProperties xmlns:cp="urn:cp"><c>Mangal</c></cp:coreProperties>'),
            "differently cased system value": ("Single", "word/styles.xml", f'<w:styles xmlns:w="{W}"><w:u w:val="Single"/></w:styles>'),
            "a style name not on the list": ("Light", "word/styles.xml", f'<w:styles xmlns:w="{W}"><w:style w:styleId="L"><w:name w:val="Light Table"/></w:style></w:styles>'),
            "a built-in style name as a form field's name": (
                "Light", "word/document.xml", f'<w:document xmlns:w="{W}"><w:ffData><w:name w:val="Light Grid"/></w:ffData></w:document>'
            ),
            "a built-in style name after its style closed": (
                "Light", "word/styles.xml", f'<w:styles xmlns:w="{W}"><w:style w:styleId="S"/><w:name w:val="Light Grid"/></w:styles>'
            ),
            "a style id not on the list": ("Light", "word/styles.xml", f'<w:styles xmlns:w="{W}"><w:style w:styleId="LightTable"/></w:styles>'),
            "a built-in style id on a non-style attribute": (
                "Light", "word/styles.xml", f'<w:styles xmlns:w="{W}"><w:style w:styleId="S"><w:name w:val="LightGrid"/></w:style></w:styles>'
            ),
            "a built-in style id in a document variable": (
                "Light", "word/settings.xml", f'<w:settings xmlns:w="{W}"><w:docVars><w:docVar w:name="T" w:val="LightGrid"/></w:docVars></w:settings>'
            ),
            "a built-in style name on another attribute": (
                "Light", "word/styles.xml", f'<w:styles xmlns:w="{W}"><w:style w:styleId="S"><w:basedOn w:val="Light Grid"/></w:style></w:styles>'
            ),
        }.items():
            with self.subTest(label):
                self.assertIn(part, self.parts(part, xml, sample))

    def test_every_one_exports_on_every_bundled_docx(self):
        self.assertEqual(refused_names_on_bundled_samples(SYSTEM_VALUE_NAMES + COMPONENT_NAMES), {})


class CamelCaseComponentTests(unittest.TestCase):
    """A name glued into an attribute identifier is found as a capitalised component, and redacted where the redactor writes."""

    def parts(self, value: str, sample: str) -> set[str]:
        xml = f'<a v="{escape(value)}"/>'
        leaks = scan_docx(docx_bytes(body="", parts={"word/extra.xml": xml}), [finding(sample, 6, source="reviewer")])
        return {leak["part"] for leak in leaks if leak["finding_id"] is not None}

    def test_a_capitalised_component_is_found(self):
        for sample, value in (
            ("Yılmaz", "elaYilmaz"),
            ("Ayşe", "AyseYilmaz"),
            ("Yılmaz", "AyseYilmaz"),
            ("Ela Yılmaz", "elaYilmaz"),
            ("Yılmaz", "mailto:elaYilmazHukuk@example.com"),
            ("Kurgusal", "_DenizKurgusal_2"),
        ):
            with self.subTest(sample=sample, value=value):
                self.assertIn("word/extra.xml", self.parts(value, sample))

    def test_nothing_but_a_capitalised_component_is_found(self):
        for sample, value in (
            ("Ela", "elaYilmaz"),  # the leading component is lowercase
            ("Light", "lightGray"),
            ("Yılmaz", "elayilmaz"),  # no boundary at all
            ("Ali", "schemaLibrary"),  # inside a component, not one
            ("Yılmaz", "ELAYILMAZ"),
        ):
            with self.subTest(sample=sample, value=value):
                self.assertEqual(self.parts(value, sample), set())

    def test_a_glued_name_is_redacted_where_the_redactor_rewrites_attributes(self):
        body = "Davacı Ela Yılmaz dilekçeyi imzaladı."
        bookmark = '<w:p><w:bookmarkStart w:id="3" w:name="elaYilmaz"/><w:r><w:t>Ek</w:t></w:r><w:bookmarkEnd w:id="3"/></w:p>'
        relationship = f'<Relationship Id="rId9" Type="{HYPERLINK_TYPE}" Target="mailto:elaYilmaz@example.com" TargetMode="External"/>'
        with TemporaryDirectory() as tmp:
            source = Path(tmp) / "ela.docx"
            source.write_bytes(docx_bytes(bookmark, relationships=relationship, body=body))
            output = redact_docx(source, [RedactionTarget("Yılmaz", "[KISI_1]")])
        self.assertEqual(scan_docx(output, [finding("Yılmaz", 6, source="reviewer")], {"[KISI_1]"}), [])
        with ZipFile(BytesIO(output)) as archive:
            self.assertIn(b'w:name="Redacted_1"', archive.read("word/document.xml"))
            self.assertIn(b'Target="#redacted"', archive.read("word/_rels/document.xml.rels"))


class LatestArtifactTests(ExportHarness):
    """The saved reviewed PDF is scanned against the findings approved NOW before it is served."""

    def saved(self) -> int:
        doc_id = self.signed_off("temiz.pdf", pdf_bytes())
        self.assertEqual(self.export(doc_id, "pdf").status_code, 200)
        response = self.client.get(f"/api/document/{doc_id}/pdf/export-artifact/latest")
        self.addCleanup(response.close)
        self.assertEqual(response.status_code, 200)
        return doc_id

    def latest(self, doc_id: int):
        response = self.client.get(f"/api/document/{doc_id}/pdf/export-artifact/latest")
        self.addCleanup(response.close)
        return response

    def test_a_finding_approved_after_the_save_refuses_the_saved_copy(self):
        doc_id = self.saved()
        response = self.client.post(f"/api/document/{doc_id}/findings", json={"text": "mahkemeye"})
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        # Adding a finding clears the sign-off; give it again so what refuses
        # the saved copy below is the scan, not the missing approval.
        response = self.client.post(f"/api/document/{doc_id}/review", json={"action": "approve"})
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.assertEqual(self.export(doc_id, "pdf").status_code, 409)
        response = self.latest(doc_id)
        self.assertEqual(response.status_code, 409, response.get_data()[:300])
        self.assertTrue(response.get_json()["error"].startswith("Export refused: "), response.get_json())
        rows = self.rows(
            "SELECT result, metadata FROM audit_log WHERE document_id = ? AND action = 'export.redacted_pdf_artifact' ORDER BY id",
            doc_id,
        )
        self.assertEqual(rows[-1][0], "blocked")
        self.assertNotIn("mahkemeye", rows[-1][1].casefold())

    def test_a_copy_saved_before_the_scan_existed_is_refused(self):
        doc_id = self.saved()
        (path,) = self.rows("SELECT file_path FROM document_artifacts WHERE document_id = ? AND artifact_type = 'redacted_pdf'", doc_id)
        Path(path[0]).write_bytes(pdf_bytes("outline"))  # a file no scan has seen, carrying the approved name
        response = self.latest(doc_id)
        self.assertEqual(response.status_code, 409, response.get_data()[:300])
        self.assertTrue(response.get_json()["error"].startswith("Export refused: "))


class ReviewerAddedAndTurkishExportTests(ExportHarness):
    def test_a_reviewer_added_item_that_survives_is_refused(self):
        secret = "Gizli Müvekkil Holding"
        styles = {
            "word/styles.xml": f'<?xml version="1.0"?><w:styles xmlns:w="{W}"><w:style w:styleId="Taraf">'
            f'<w:name w:val="{secret}"/></w:style></w:styles>'
        }
        doc_id = self.upload("taraf.docx", docx_bytes(parts=styles))
        response = self.client.post(f"/api/document/{doc_id}/findings", json={"text": secret})
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.review_and_sign_off(doc_id)
        statuses = {row[0] for row in self.rows("SELECT review_status FROM privacy_findings WHERE document_id = ? AND sample = ?", doc_id, secret)}
        self.assertEqual(statuses, {"added_by_reviewer"})
        response = self.export(doc_id)
        self.assertEqual(response.status_code, 409, response.get_data()[:300])
        self.assertIn("word/styles.xml", response.get_json()["error"])

    def test_a_bracket_shaped_reviewer_replacement_carrying_an_approved_name_is_refused(self):
        doc_id = self.upload("kemal.docx", docx_bytes('<w:p><w:r><w:t>Dosya sahibi kemal bey olarak kayıtlı.</w:t></w:r></w:p>'))
        for text, replacement in (("sahibi kemal bey", "[KEMAL_BEY]"), ("Kemal", None)):
            response = self.client.post(f"/api/document/{doc_id}/findings", json={"text": text, "replacement_text": replacement})
            self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.review_and_sign_off(doc_id)
        response = self.export(doc_id)
        self.assertEqual(response.status_code, 409, response.get_data()[:300])
        self.assertIn("word/document.xml", response.get_json()["error"])

    def test_a_tracked_deleted_field_code_exports_redacted(self):
        deleted_code = (
            '<w:p><w:del w:id="2" w:author="Reviewer" w:date="2000-01-01T00:00:00Z"><w:r>'
            f'<w:delInstrText xml:space="preserve"> HYPERLINK "mailto:{EMAIL}" </w:delInstrText></w:r></w:del></w:p>'
        )
        doc_id = self.signed_off("alan.docx", docx_bytes(deleted_code))
        response = self.export(doc_id)
        self.assertEqual(response.status_code, 200, response.get_data()[:300])
        with ZipFile(BytesIO(response.get_data())) as archive:
            root = ElementTree.fromstring(archive.read("word/document.xml"))
        code = "".join(node.text or "" for node in root.iter(f"{{{W}}}delInstrText"))
        self.assertIn("HYPERLINK", code)
        self.assertNotIn("kurgusal", code.casefold())

    def test_a_utf8_turkish_name_in_page_metadata_refuses_the_pdf(self):
        # Helvetica cannot draw "Ş", so the name reaches the file only through the
        # page's UTF-8 XMP; the reviewer adds it, as for any name detection missed.
        doc_id = self.upload("sukru.pdf", pdf_with_utf8_page_metadata())
        response = self.client.post(f"/api/document/{doc_id}/findings", json={"text": TURKISH_NAME})
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.review_and_sign_off(doc_id, pdf=True)
        response = self.export(doc_id, "pdf")
        self.assertEqual(response.status_code, 409, response.get_data()[:300])
        self.assertIn("object ", response.get_json()["error"])


if __name__ == "__main__":
    unittest.main()
