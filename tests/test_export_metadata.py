"""Reviewed exports must not carry identifying document metadata, and QA must see it.

The review canvas shows the document body; it never shows a DOCX's document
properties or a PDF's Info dictionary and XMP packet. Both exporters used to
ship those unchanged (DOCX) or half-cleared (PDF: Info cleared, XMP kept), and
both QA passes were blind to it: the DOCX one compared Word text parts only and
reported pass, and the PDF one warned on PyMuPDF's synthetic ``format`` key, so
a leaking file and a clean one produced the same warning.

Every name below is synthetic.
"""

import unittest
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from zipfile import ZIP_DEFLATED, ZipFile

import pymupdf

from legal_analyzer.docx_quality import analyze_docx_export_quality
from legal_analyzer.docx_redactor import (
    BLANK_THUMBNAILS,
    DOCX_METADATA_PARTS,
    RedactionTarget,
    contains_case_insensitive,
    is_docx_thumbnail_part,
    redact_docx,
)
from legal_analyzer.pdf_redactor import analyze_pdf_redaction_quality, find_pdf_regions, redact_pdf

CLIENT = "Kumru Yalçınkaya"
AUTHOR = "Av. Tuna Örsdemir"
FIRM = "Örsdemir Hukuk Bürosu"
MANAGER = "Selvi Karaduman"
TEMPLATE = "Orsdemir_Dilekce.dotm"
CUSTOM_NAME = "MuvekkilAdi"
IDENTIFYING = [CLIENT, AUTHOR, FIRM, MANAGER, TEMPLATE, CUSTOM_NAME]

CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
  <Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>
  <Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>
  <Override PartName="/docProps/custom.xml" ContentType="application/vnd.openxmlformats-officedocument.custom-properties+xml"/>
</Types>
"""
RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>
  <Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/>
  <Relationship Id="rId4" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/custom-properties" Target="docProps/custom.xml"/>
</Relationships>
"""
DOCUMENT = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body><w:p><w:r><w:t>Davacı {CLIENT} vekili</w:t></w:r></w:p></w:body>
</w:document>
"""
# Evidence E2: the title carries the approved client name; author and firm are
# in core.xml and app.xml and are never a finding.
LEAKING_CORE = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <dc:title>{CLIENT} ihtarname</dc:title>
  <dc:subject>{CLIENT} alacak</dc:subject>
  <dc:creator>{AUTHOR}</dc:creator>
  <cp:lastModifiedBy>{AUTHOR}</cp:lastModifiedBy>
  <cp:keywords>{CLIENT}</cp:keywords>
  <dc:description>{FIRM}</dc:description>
  <cp:revision>3</cp:revision>
  <dcterms:created xsi:type="dcterms:W3CDTF">2026-01-05T09:00:00Z</dcterms:created>
</cp:coreProperties>
"""
LEAKING_APP = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties" xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes">
  <Template>{TEMPLATE}</Template>
  <Application>Microsoft Office Word</Application>
  <DocSecurity>0</DocSecurity>
  <Company>{FIRM}</Company>
  <Manager>{MANAGER}</Manager>
  <TitlesOfParts><vt:vector size="1" baseType="lpstr"><vt:lpstr>{CLIENT} ihtarname</vt:lpstr></vt:vector></TitlesOfParts>
  <AppVersion>16.0000</AppVersion>
</Properties>
"""
LEAKING_CUSTOM = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/custom-properties" xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes">
  <property fmtid="{{D5CDD505-2E9C-101B-9397-08002B2CF9AE}}" pid="2" name="{CUSTOM_NAME}"><vt:lpwstr>{CLIENT}</vt:lpwstr></property>
</Properties>
"""
# The control: properties a document may carry without identifying anyone.
CLEAN_CORE = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title></dc:title></cp:coreProperties>
"""
CLEAN_APP = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties"><Application>Microsoft Office Word</Application><DocSecurity>0</DocSecurity></Properties>
"""
CLEAN_CUSTOM = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/custom-properties"/>
"""

TARGETS = [RedactionTarget(CLIENT, "[PERSON_1]")]


def page_picture(fmt: str) -> bytes:
    """A stand-in for Word's first-page thumbnail: a small non-blank image."""
    pixmap = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 8, 8), False)
    pixmap.set_rect(pixmap.irect, (20, 20, 20))
    return pixmap.tobytes(fmt)


def make_docx(path: Path, core: str, app: str, custom: str, thumbnail: tuple[str, bytes] | None = None) -> None:
    content_types, rels = CONTENT_TYPES, RELS
    if thumbnail:
        name, _ = thumbnail
        extension = name.rsplit(".", 1)[-1]
        content_types = content_types.replace(
            "<Default Extension=\"xml\"",
            f'<Default Extension="{extension}" ContentType="image/{extension}"/>\n  <Default Extension="xml"',
        )
        rels = rels.replace(
            "</Relationships>",
            '  <Relationship Id="rId5" Type="http://schemas.openxmlformats.org/package/2006/relationships/'
            f'metadata/thumbnail" Target="{name}"/>\n</Relationships>',
        )
    with ZipFile(path, "w", ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("_rels/.rels", rels)
        archive.writestr("word/document.xml", DOCUMENT)
        archive.writestr("docProps/core.xml", core)
        archive.writestr("docProps/app.xml", app)
        archive.writestr("docProps/custom.xml", custom)
        if thumbnail:
            archive.writestr(*thumbnail)


def with_source_properties(source: Path, redacted: bytes) -> bytes:
    """The redacted body with the source's document properties put back.

    This is what the exporter produced before it scrubbed docProps: the text
    parts redacted, every property part copied through unchanged.
    """
    output = BytesIO()
    with ZipFile(source) as original, ZipFile(BytesIO(redacted)) as zin, ZipFile(output, "w", ZIP_DEFLATED) as zout:
        for info in zin.infolist():
            copied = info.filename in DOCX_METADATA_PARTS or is_docx_thumbnail_part(info.filename)
            data = original.read(info.filename) if copied else zin.read(info.filename)
            zout.writestr(info, data)
    return output.getvalue()


def check_status(report: dict, name: str) -> str:
    return next(check["status"] for check in report["checks"] if check["name"] == name)


class ReviewedDocxCarriesNoIdentifyingPropertiesTests(unittest.TestCase):
    def test_export_removes_every_identifying_property(self):
        with TemporaryDirectory() as tmp:
            source = Path(tmp) / "source.docx"
            make_docx(source, LEAKING_CORE, LEAKING_APP, LEAKING_CUSTOM)
            data = redact_docx(source, TARGETS)
            with ZipFile(BytesIO(data)) as archive:
                properties = {name: archive.read(name).decode("utf-8") for name in DOCX_METADATA_PARTS}
                names = set(archive.namelist())
            for name, xml in properties.items():
                for value in IDENTIFYING:
                    self.assertFalse(contains_case_insensitive(xml, value), f"{value!r} survived in {name}")
            # Rewritten, not deleted: the relationships and content types still point at them.
            with ZipFile(source) as archive:
                self.assertEqual(names, set(archive.namelist()))
            # What does not identify anyone is kept.
            self.assertIn("<Application>Microsoft Office Word</Application>", properties["docProps/app.xml"])

    def test_unreadable_property_part_is_replaced_not_shipped(self):
        with TemporaryDirectory() as tmp:
            source = Path(tmp) / "source.docx"
            broken_app = f"<Properties><Company>{FIRM}</Company>"  # unterminated
            make_docx(source, LEAKING_CORE, broken_app, LEAKING_CUSTOM)
            data = redact_docx(source, TARGETS)
            with ZipFile(BytesIO(data)) as archive:
                app = archive.read("docProps/app.xml").decode("utf-8")
            self.assertFalse(contains_case_insensitive(app, FIRM))
            report = analyze_docx_export_quality(source, data, TARGETS, sensitive_samples=[CLIENT])
            self.assertEqual(check_status(report, "Document properties scrubbed"), "pass")

    def test_qa_passes_the_fixed_export(self):
        with TemporaryDirectory() as tmp:
            source = Path(tmp) / "source.docx"
            make_docx(source, LEAKING_CORE, LEAKING_APP, LEAKING_CUSTOM)
            data = redact_docx(source, TARGETS)
            report = analyze_docx_export_quality(source, data, TARGETS, sensitive_samples=[CLIENT])
            self.assertEqual(report["overall_status"], "pass", report["checks"])
            self.assertEqual(report["leakage_count"], 0)
            self.assertEqual(report["metadata_field_count"], 0)


class DocxQaSeesDocumentPropertiesTests(unittest.TestCase):
    def test_qa_fails_the_unscrubbed_export(self):
        """Evidence E2: this exact shape reported overall pass and leakage_count 0."""
        with TemporaryDirectory() as tmp:
            source = Path(tmp) / "source.docx"
            make_docx(source, LEAKING_CORE, LEAKING_APP, LEAKING_CUSTOM)
            leaking = with_source_properties(source, redact_docx(source, TARGETS))
            report = analyze_docx_export_quality(source, leaking, TARGETS, sensitive_samples=[CLIENT])
            self.assertEqual(report["overall_status"], "fail")
            self.assertEqual(check_status(report, "Document properties scrubbed"), "fail")
            self.assertIn("docProps/core.xml: creator", report["metadata_fields"])
            self.assertIn("docProps/app.xml: Company", report["metadata_fields"])
            self.assertIn("docProps/custom.xml: custom property", report["metadata_fields"])

    def test_a_finding_sample_in_the_title_counts_as_leakage(self):
        """leakage_count stamps redacted_verified, so it must see the title too."""
        with TemporaryDirectory() as tmp:
            source = Path(tmp) / "source.docx"
            make_docx(source, LEAKING_CORE, CLEAN_APP, CLEAN_CUSTOM)
            leaking = with_source_properties(source, redact_docx(source, TARGETS))
            report = analyze_docx_export_quality(source, leaking, TARGETS, sensitive_samples=[CLIENT])
            self.assertEqual(report["leakage_count"], 1)
            self.assertEqual(check_status(report, "Sensitive source text removed"), "fail")

    def test_identifying_property_that_is_no_finding_still_fails(self):
        """The author is never a finding sample; the property check alone must catch it."""
        author_only = LEAKING_CORE.replace(f"{CLIENT} ihtarname", "").replace(f"{CLIENT} alacak", "")
        author_only = author_only.replace(f"<cp:keywords>{CLIENT}</cp:keywords>", "")
        with TemporaryDirectory() as tmp:
            source = Path(tmp) / "source.docx"
            make_docx(source, author_only, CLEAN_APP, CLEAN_CUSTOM)
            leaking = with_source_properties(source, redact_docx(source, TARGETS))
            report = analyze_docx_export_quality(source, leaking, TARGETS, sensitive_samples=[CLIENT])
            self.assertEqual(report["leakage_count"], 0)
            self.assertEqual(report["overall_status"], "fail")
            self.assertIn("docProps/core.xml: creator", report["metadata_fields"])

    def test_report_names_fields_without_repeating_their_values(self):
        with TemporaryDirectory() as tmp:
            source = Path(tmp) / "source.docx"
            make_docx(source, LEAKING_CORE, LEAKING_APP, LEAKING_CUSTOM)
            leaking = with_source_properties(source, redact_docx(source, TARGETS))
            report = analyze_docx_export_quality(source, leaking, TARGETS, sensitive_samples=[CLIENT])
            rendered = repr(report)
            for value in IDENTIFYING:
                self.assertFalse(contains_case_insensitive(rendered, value), f"{value!r} echoed in QA report")

    def test_clean_control_is_not_flagged(self):
        with TemporaryDirectory() as tmp:
            source = Path(tmp) / "source.docx"
            make_docx(source, CLEAN_CORE, CLEAN_APP, CLEAN_CUSTOM)
            # Unchanged properties: the check must pass on what was already clean.
            unscrubbed = with_source_properties(source, redact_docx(source, TARGETS))
            report = analyze_docx_export_quality(source, unscrubbed, TARGETS, sensitive_samples=[CLIENT])
            self.assertEqual(check_status(report, "Document properties scrubbed"), "pass")
            self.assertEqual(report["overall_status"], "pass", report["checks"])


class DocxThumbnailIsNotTheSourcePageTests(unittest.TestCase):
    """docProps/thumbnail.* is a picture of the unredacted first page."""

    def test_export_blanks_the_thumbnail(self):
        for name, fmt in (("docProps/thumbnail.jpeg", "jpeg"), ("docProps/thumbnail.png", "png")):
            with self.subTest(name=name), TemporaryDirectory() as tmp:
                source = Path(tmp) / "source.docx"
                make_docx(source, CLEAN_CORE, CLEAN_APP, CLEAN_CUSTOM, thumbnail=(name, page_picture(fmt)))
                data = redact_docx(source, TARGETS)
                with ZipFile(BytesIO(data)) as archive:
                    exported = archive.read(name)
                self.assertNotEqual(exported, page_picture(fmt))
                self.assertEqual(pymupdf.Pixmap(exported).width, 1)  # still a readable image
                report = analyze_docx_export_quality(source, data, TARGETS, sensitive_samples=[CLIENT])
                self.assertEqual(report["overall_status"], "pass", report["checks"])

    def test_qa_fails_an_unchanged_thumbnail(self):
        with TemporaryDirectory() as tmp:
            source = Path(tmp) / "source.docx"
            make_docx(source, CLEAN_CORE, CLEAN_APP, CLEAN_CUSTOM, thumbnail=("docProps/thumbnail.jpeg", page_picture("jpeg")))
            leaking = with_source_properties(source, redact_docx(source, TARGETS))
            report = analyze_docx_export_quality(source, leaking, TARGETS, sensitive_samples=[CLIENT])
            self.assertEqual(report["overall_status"], "fail")
            self.assertIn("docProps/thumbnail.jpeg: page thumbnail unchanged from source", report["metadata_fields"])

    def test_every_thumbnail_format_is_replaced(self):
        """Windows Word writes EMF/WMF thumbnails; a format with no blank is emptied, never shipped."""
        for name in ("docProps/thumbnail.emf", "docProps/thumbnail.wmf", "docProps/thumbnail.tiff"):
            with self.subTest(name=name), TemporaryDirectory() as tmp:
                source = Path(tmp) / "source.docx"
                picture = b"\x01\x00\x00\x00 source page picture " + name.encode()
                make_docx(source, CLEAN_CORE, CLEAN_APP, CLEAN_CUSTOM, thumbnail=(name, picture))
                data = redact_docx(source, TARGETS)
                with ZipFile(BytesIO(data)) as archive:
                    exported = archive.read(name)
                self.assertNotIn(b"source page picture", exported)
                self.assertEqual(exported, BLANK_THUMBNAILS.get("." + name.rsplit(".", 1)[-1], b""))
                report = analyze_docx_export_quality(source, data, TARGETS, sensitive_samples=[CLIENT])
                self.assertEqual(report["overall_status"], "pass", report["checks"])

    def test_qa_fails_an_unchanged_emf_thumbnail(self):
        with TemporaryDirectory() as tmp:
            source = Path(tmp) / "source.docx"
            make_docx(source, CLEAN_CORE, CLEAN_APP, CLEAN_CUSTOM, thumbnail=("docProps/thumbnail.emf", b"\x01\x00\x00\x00EMF page"))
            leaking = with_source_properties(source, redact_docx(source, TARGETS))
            report = analyze_docx_export_quality(source, leaking, TARGETS, sensitive_samples=[CLIENT])
            self.assertEqual(check_status(report, "Document properties scrubbed"), "fail")

    def test_re_exporting_a_blanked_thumbnail_is_not_flagged(self):
        with TemporaryDirectory() as tmp:
            source = Path(tmp) / "source.docx"
            make_docx(source, CLEAN_CORE, CLEAN_APP, CLEAN_CUSTOM, thumbnail=("docProps/thumbnail.jpeg", BLANK_THUMBNAILS[".jpeg"]))
            data = redact_docx(source, TARGETS)
            report = analyze_docx_export_quality(source, data, TARGETS, sensitive_samples=[CLIENT])
            self.assertEqual(check_status(report, "Document properties scrubbed"), "pass")


REVISER = "Av. Pelin Doğanay"
REVISER_INITIALS = "PD"
REVISER_EMAIL = "pelin.doganay@example.com"
REVISION_DATE = "2026-02-17T10:41:00Z"
MATTER = "Yalçınkaya Kira Alacağı"
TEMPLATE_PATH = r"file:///C:\Users\pdoganay\AppData\Roaming\Microsoft\Templates\Dilekce.dotm"
BOUND_ID = "{6A1F0C2E-1B7D-4C51-9E0A-2F3B4C5D6E7F}"
UNBOUND_ID = "{0B9E8D7C-6F5A-4B3C-8D2E-1F0A9B8C7D6E}"
W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
W15 = 'xmlns:w15="http://schemas.microsoft.com/office/word/2012/wordml"'
DS = 'xmlns:ds="http://schemas.openxmlformats.org/officeDocument/2006/customXml"'
REL_NS = 'xmlns="http://schemas.openxmlformats.org/package/2006/relationships"'
CUSTOM_PROPS_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/customXmlProps"

REVISED_DOCUMENT = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document {W}><w:body>
<w:p><w:ins w:id="1" w:author="{REVISER}" w:date="{REVISION_DATE}"><w:r><w:t>Davacı {CLIENT} vekili</w:t></w:r></w:ins></w:p>
<w:p><w:del w:id="2" w:author="{REVISER}" w:date="{REVISION_DATE}"><w:r><w:delText>eski</w:delText></w:r></w:del></w:p>
<w:p><w:r><w:rPr><w:b/><w:rPrChange w:id="3" w:author="{REVISER}" w:date="{REVISION_DATE}"><w:rPr/></w:rPrChange></w:rPr><w:t>talep</w:t></w:r></w:p>
<w:sdt><w:sdtPr><w:dataBinding w:prefixMappings="xmlns:m='urn:example:matter'" w:xpath="/m:matter/m:client" w:storeItemID="{BOUND_ID}"/></w:sdtPr>
<w:sdtContent><w:p><w:r><w:t>{CLIENT}</w:t></w:r></w:p></w:sdtContent></w:sdt>
</w:body></w:document>
"""
COMMENTS = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:comments {W}><w:comment w:id="0" w:author="{REVISER}" w:initials="{REVISER_INITIALS}" w:date="{REVISION_DATE}"><w:p><w:r><w:t>kontrol</w:t></w:r></w:p></w:comment></w:comments>
"""
PEOPLE = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w15:people {W15}><w15:person w15:author="{REVISER}"><w15:presenceInfo w15:providerId="AD" w15:userId="{REVISER_EMAIL}"/></w15:person></w15:people>
"""
SETTINGS_RELS = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships {REL_NS}><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/attachedTemplate" Target="{TEMPLATE_PATH}" TargetMode="External"/></Relationships>
"""
BOUND_ITEM = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<m:matter xmlns:m="urn:example:matter"><m:client>{CLIENT}</m:client></m:matter>
"""
UNBOUND_ITEM = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<p:properties xmlns:p="urn:example:dms"><p:Matter>{MATTER}</p:Matter><p:Owner>{REVISER}</p:Owner></p:properties>
"""
PACKAGE_STORE_PARTS = {
    "word/document.xml": REVISED_DOCUMENT,
    "word/comments.xml": COMMENTS,
    "word/people.xml": PEOPLE,
    "word/_rels/settings.xml.rels": SETTINGS_RELS,
    "customXml/item1.xml": BOUND_ITEM,
    "customXml/item2.xml": UNBOUND_ITEM,
}
REVISION_IDENTIFYING = [REVISER, REVISER_INITIALS, REVISER_EMAIL, REVISION_DATE, MATTER, "pdoganay", CLIENT]


def make_package_store_docx(path: Path) -> None:
    make_docx(path, CLEAN_CORE, CLEAN_APP, CLEAN_CUSTOM)
    with ZipFile(path, "a", ZIP_DEFLATED) as archive:
        rest = {name: xml for name, xml in PACKAGE_STORE_PARTS.items() if name != "word/document.xml"}
        for name, xml in rest.items():
            archive.writestr(name, xml)
        for index, item_id in ((1, BOUND_ID), (2, UNBOUND_ID)):
            archive.writestr(
                f"customXml/_rels/item{index}.xml.rels",
                f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<Relationships {REL_NS}>'
                f'<Relationship Id="rId1" Type="{CUSTOM_PROPS_REL}" Target="itemProps{index}.xml"/></Relationships>',
            )
            archive.writestr(
                f"customXml/itemProps{index}.xml",
                f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<ds:datastoreItem ds:itemID="{item_id}" {DS}/>',
            )
    # make_docx wrote the plain body; swap in the revised one.
    output = BytesIO()
    with ZipFile(path) as zin, ZipFile(output, "w", ZIP_DEFLATED) as zout:
        for info in zin.infolist():
            data = REVISED_DOCUMENT.encode("utf-8") if info.filename == "word/document.xml" else zin.read(info.filename)
            zout.writestr(info, data)
    path.write_bytes(output.getvalue())


def with_source_part(source: Path, redacted: bytes, part: str) -> bytes:
    """The export with one part copied through from the source unscrubbed."""
    output = BytesIO()
    with ZipFile(source) as original, ZipFile(BytesIO(redacted)) as zin, ZipFile(output, "w", ZIP_DEFLATED) as zout:
        for info in zin.infolist():
            zout.writestr(info, original.read(part) if info.filename == part else zin.read(info.filename))
    return output.getvalue()


class ReviewedDocxCarriesNoRevisionOrPackageIdentityTests(unittest.TestCase):
    """Revision and comment authors, people.xml, customXml data islands and the template path."""

    def export(self, tmp: str) -> tuple[Path, bytes]:
        source = Path(tmp) / "source.docx"
        make_package_store_docx(source)
        return source, redact_docx(source, TARGETS)

    def test_export_neutralises_every_store(self):
        with TemporaryDirectory() as tmp:
            source, data = self.export(tmp)
            with ZipFile(BytesIO(data)) as archive:
                parts = {name: archive.read(name).decode("utf-8") for name in PACKAGE_STORE_PARTS}
                names = set(archive.namelist())
            with ZipFile(source) as archive:
                self.assertEqual(names, set(archive.namelist()))
            for name, xml in parts.items():
                for value in REVISION_IDENTIFYING:
                    with self.subTest(part=name, value=value):
                        self.assertFalse(contains_case_insensitive(xml, value), f"{value!r} survived in {name}")
            self.assertIn('Target="Dilekce.dotm"', parts["word/_rels/settings.xml.rels"])
            self.assertIn("[PERSON_1]", parts["customXml/item1.xml"])
            self.assertIn('w:author="Reviewer"', parts["word/comments.xml"])

    def test_qa_passes_the_fixed_export(self):
        with TemporaryDirectory() as tmp:
            source, data = self.export(tmp)
            report = analyze_docx_export_quality(source, data, TARGETS, sensitive_samples=[CLIENT])
            self.assertEqual(report["overall_status"], "pass", report["checks"])
            self.assertEqual(report["metadata_field_count"], 0)

    def test_qa_fails_each_store_left_unscrubbed(self):
        expected = {
            "word/comments.xml": "word/comments.xml: author attribute",
            "word/people.xml": "word/people.xml: userId attribute",
            "word/_rels/settings.xml.rels": "word/_rels/settings.xml.rels: attached template path",
            "customXml/item2.xml": "customXml/item2.xml: unbound data island",
        }
        with TemporaryDirectory() as tmp:
            source, data = self.export(tmp)
            for part, field in expected.items():
                with self.subTest(part=part):
                    report = analyze_docx_export_quality(
                        source, with_source_part(source, data, part), TARGETS, sensitive_samples=[CLIENT]
                    )
                    self.assertEqual(report["overall_status"], "fail")
                    self.assertIn(field, report["metadata_fields"])
                    for value in REVISION_IDENTIFYING:
                        self.assertFalse(contains_case_insensitive(repr(report), value))

    def test_qa_fails_tracked_change_authors_in_the_body(self):
        with TemporaryDirectory() as tmp:
            source, data = self.export(tmp)
            body = REVISED_DOCUMENT.replace(f"Davacı {CLIENT} vekili", "Davacı [PERSON_1] vekili").replace(
                f"<w:t>{CLIENT}</w:t>", "<w:t>[PERSON_1]</w:t>"
            )
            output = BytesIO()
            with ZipFile(BytesIO(data)) as zin, ZipFile(output, "w", ZIP_DEFLATED) as zout:
                for info in zin.infolist():
                    zout.writestr(info, body if info.filename == "word/document.xml" else zin.read(info.filename))
            report = analyze_docx_export_quality(source, output.getvalue(), TARGETS, sensitive_samples=[CLIENT])
            self.assertEqual(report["overall_status"], "fail")
            self.assertIn("word/document.xml: author attribute", report["metadata_fields"])
            self.assertIn("word/document.xml: date attribute", report["metadata_fields"])

    def test_a_bound_item_left_unredacted_counts_as_leakage(self):
        with TemporaryDirectory() as tmp:
            source, data = self.export(tmp)
            leaking = with_source_part(source, data, "customXml/item1.xml")
            report = analyze_docx_export_quality(source, leaking, TARGETS, sensitive_samples=[CLIENT])
            self.assertEqual(report["leakage_count"], 1)
            self.assertEqual(report["overall_status"], "fail")


XMP = f"""<?xpacket begin="" id="W5M0MpCehiHzreSzNTczkc9d"?>
<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
<rdf:Description rdf:about="" xmlns:dc="http://purl.org/dc/elements/1.1/">
<dc:title><rdf:Alt><rdf:li xml:lang="x-default">{CLIENT} ihtarname</rdf:li></rdf:Alt></dc:title>
</rdf:Description></rdf:RDF></x:xmpmeta>
<?xpacket end="w"?>"""
# Page text stays ASCII: base-14 Helvetica cannot encode the Turkish letters.
PDF_CLIENT = "Kumru Yalcinkaya"


def make_pdf(path: Path, info: dict | None = None, xmp: str | None = None) -> None:
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), f"Davaci {PDF_CLIENT} vekili", fontsize=12)
    if info:
        doc.set_metadata(info)
    if xmp:
        doc.set_xml_metadata(xmp)
    doc.save(path)
    doc.close()


def regions_for(source: Path):
    return find_pdf_regions(source, [{"id": 1, "sample": PDF_CLIENT, "category": "natural_person_name"}])


def with_metadata(data: bytes, info: dict | None = None, xmp: str | None = None) -> bytes:
    with pymupdf.open(stream=data, filetype="pdf") as doc:
        if info:
            doc.set_metadata(info)
        if xmp:
            doc.set_xml_metadata(xmp)
        return doc.tobytes()


class RedactedPdfCarriesNoMetadataTests(unittest.TestCase):
    def test_export_clears_info_and_xmp(self):
        """Evidence E3: Info was cleared and the XMP title kept the client name."""
        with TemporaryDirectory() as tmp:
            source = Path(tmp) / "source.pdf"
            make_pdf(source, {"title": f"{PDF_CLIENT} ihtarname", "author": "Av. Tuna Orsdemir"}, XMP)
            data = redact_pdf(source, regions_for(source))
            with pymupdf.open(stream=data, filetype="pdf") as doc:
                self.assertEqual(doc.get_xml_metadata(), "")
                self.assertEqual({k: v for k, v in doc.metadata.items() if v and k not in {"format", "encryption"}}, {})
            report = analyze_pdf_redaction_quality(source, data, regions_for(source), sensitive_samples=[PDF_CLIENT])
            self.assertEqual(check_status(report, "PDF metadata scrubbed"), "pass")
            self.assertEqual(report["metadata_leak_count"], 0)
            self.assertEqual(report["overall_status"], "pass", report["checks"])


class PdfQaSeesMetadataTests(unittest.TestCase):
    def test_xmp_left_in_the_output_fails(self):
        with TemporaryDirectory() as tmp:
            source = Path(tmp) / "source.pdf"
            make_pdf(source)
            leaking = with_metadata(redact_pdf(source, regions_for(source)), xmp=XMP)
            report = analyze_pdf_redaction_quality(source, leaking, regions_for(source), sensitive_samples=[PDF_CLIENT])
            self.assertEqual(check_status(report, "PDF metadata scrubbed"), "fail")
            self.assertEqual(report["overall_status"], "fail")
            self.assertEqual(report["metadata_leak_count"], 1)

    def test_info_left_in_the_output_fails(self):
        with TemporaryDirectory() as tmp:
            source = Path(tmp) / "source.pdf"
            make_pdf(source)
            leaking = with_metadata(redact_pdf(source, regions_for(source)), info={"author": "Av. Tuna Orsdemir"})
            report = analyze_pdf_redaction_quality(source, leaking, regions_for(source), sensitive_samples=[PDF_CLIENT])
            self.assertEqual(check_status(report, "PDF metadata scrubbed"), "fail")
            self.assertNotIn("Orsdemir", repr(report["checks"]))

    def test_clean_control_is_not_flagged(self):
        """The control from E3: no Info, no XMP. It used to warn on PyMuPDF's format key."""
        with TemporaryDirectory() as tmp:
            source = Path(tmp) / "source.pdf"
            make_pdf(source)
            data = redact_pdf(source, regions_for(source))
            report = analyze_pdf_redaction_quality(source, data, regions_for(source), sensitive_samples=[PDF_CLIENT])
            self.assertEqual(check_status(report, "PDF metadata scrubbed"), "pass")
            self.assertEqual(report["metadata_leak_count"], 0)


if __name__ == "__main__":
    unittest.main()
