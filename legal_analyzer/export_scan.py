"""The reviewed export's last check: do approved items still appear anywhere in the file.

Both QA passes before this one looked where the redactor edits -- the ``w:t``
runs of a DOCX, the page text of a PDF -- so they were structurally unable to see
what it skips. After every finding was approved and the document signed off, a
reviewed DOCX still carried the email in its hyperlink relationship, the name in
a tracked deletion, a field code, an image description, a tooltip, a bookmark
and a document variable; a reviewed PDF kept its outline title, an embedded
attachment, a ``mailto:`` link and a page-label prefix. QA passed every one.

So this scan does not share the redactor's model of where data lives, and must
not grow one. It reads the OUTPUT bytes and searches every place text can be:

* DOCX -- every package member that is XML or a relationship part, whatever its
  name or path, as text nodes (joined within a paragraph or table cell, never
  across one), attribute values (also percent-decoded) and the bodies of
  comments, processing instructions and DTDs; the member names; and any member
  that is itself a package, recursively.
* PDF -- page text, every object's source with its string literals, hex strings
  and names decoded, every stream that is not an image or a font program, the
  outline, embedded files (names, descriptions, contents), link targets,
  annotation and form-field values, page labels, the Info dictionary and XMP.

Two kinds of payload are not searched, in either format, because they are not
text and an approved sample "found" in them would be noise: images, and the
glyph programs of embedded fonts. A three-letter surname matched case-
insensitively occurs by chance in about one megabyte of compressed font data
in ten.

Matching is ``docx_redactor.SampleMatcher`` -- the redactor's own pattern for a
standalone surname, and the case and Turkish folds for everything else -- so
the scan cannot disagree with the redactor about what a match is; it disagrees
only about where to look, which is the point. An XML attribute value is the one
exception, and it has two rules. The first: the sample, case- and Turkish-
folded, as a whole word -- bounded on each side by a character that is neither
a letter nor a digit, or by the end of the value. Every Word package's own
attribute values are identifiers and schema URIs ("Calibri", "schemaLibrary",
".../relationships", "neCell") in which a short name occurs by chance, while a
name a person typed into one ("mailto:ela.yilmaz@", "_Ela_Yilmaz",
"urn:example:Ela") is separated from what surrounds it. The second: a name
glued into an identifier with nothing between ("elaYilmaz", "AyseYilmaz") is
found as a capitalised camelCase component ("Yilmaz"; "Ayse"), never as the
lowercase component every OOXML identifier starts with ("light" in
"lightGray") -- ``SampleMatcher.found_as_component_in``.

Beyond those rules, exact values are not searched, each documented where it is
applied: a placeholder token the system generated and this export wrote
(``scan_docx``); a Word system value on the one attribute it is a system value
of -- a stock theme font, a built-in table style's name, a border's
``w:val="nil"``, an enumeration such as ``w:jc w:val="center"``
(``_SYSTEM_VALUES``); the fixed boolean properties of
``docProps/app.xml`` (``_APP_BOOLEAN_PROPERTIES``); and the XML declaration's
version, encoding and standalone at the start of a part.

A leak is reported as ``{"part", "finding_id", "category"}`` and never carries
the sample: it feeds a refusal message, an audit row and a QA report. A part
whose own name carries an approved sample is reported by position instead. A
part that cannot be read is a leak with ``finding_id`` None and category
``"unreadable"``: it cannot be shown clean, so it fails closed.
"""

from __future__ import annotations

import codecs
import html
import re
import zlib
from dataclasses import dataclass
from io import BytesIO
from urllib.parse import unquote
from zipfile import BadZipFile, ZipFile

import pymupdf  # type: ignore

from legal_analyzer.docx_redactor import (
    PLACEHOLDER_TOKEN_RE,
    ComponentText,
    FoldedText,
    SampleMatcher,
    attribute_value_texts,
    is_standalone_sample,
    purge_target_pattern_cache,
)
from legal_analyzer.privacy import finding_needles

# The review statuses whose text the export removes; their survival is a leak.
SCANNED_REVIEW_STATUSES = frozenset({"approved", "added_by_reviewer"})
UNREADABLE = "unreadable"
# How deep a package nested in a package (an embedded spreadsheet, a DOCX
# attached to a PDF) is opened. Deeper than this is refused unread.
MAX_PACKAGE_DEPTH = 3
# How many decompressed bytes of package members and PDF attachments one scan
# reads. A few kilobytes of zip can declare gigabytes; past this budget a member
# is refused unread rather than inflated into memory.
MAX_SCANNED_BYTES = 256 * 1024 * 1024
# Joins the texts of one part so a match can never span two of them: a NUL is
# neither whitespace nor a word character and never occurs in a sample.
_SEPARATOR = "\0"


@dataclass(frozen=True)
class _Finding:
    """One matching behaviour, and every approved finding row that has it.

    A document carries the same entity in many rows -- one per mention -- and
    each row used to be searched for separately: 750 rows of three names took
    40 s on a 30-page document. Rows whose compiled matchers are identical find
    exactly the same text, so they are searched once and a hit is reported for
    each of them.
    """

    members: tuple[tuple[int | None, str], ...]
    matcher: SampleMatcher
    word: SampleMatcher


def scan_docx(data: bytes, findings, replacements=()) -> list[dict]:
    """The approved findings that still appear anywhere in a DOCX's bytes.

    ``replacements`` are the system-generated strings this export wrote in place
    of the findings, never one a reviewer typed. Those with placeholder
    shape are removed from the text searched: "Er" is inside "[PERSON_1]" and
    "Mo" inside "[AMOUNT_1]", and a refusal on the redaction itself could never
    be cleared. Only an exact token goes; whatever surrounds it is still read.
    """
    tokens = sorted({token for token in replacements if PLACEHOLDER_TOKEN_RE.fullmatch(token)}, key=len, reverse=True)
    return _scan(findings, lambda scanner: scanner.package(data, ""), tokens)


def scan_pdf(data: bytes, findings) -> list[dict]:
    """The approved findings that still appear anywhere in a PDF's bytes."""
    return _scan(findings, lambda scanner: scanner.pdf(data, ""))


def leak_parts(leaks: list[dict]) -> list[str]:
    """The distinct parts leaks were found in: named places first, then numbered PDF objects in order."""
    return sorted({leak["part"] for leak in leaks}, key=_part_order)


def _part_order(part: str) -> tuple:
    numbered = re.search(r"\bobject (\d+)", part)
    return (numbered is not None, int(numbered.group(1)) if numbered else 0, part)


def leaked_item_count(leaks: list[dict]) -> int:
    return len({leak["finding_id"] for leak in leaks if leak["finding_id"] is not None})


def output_scan_check(leaks: list[dict]) -> dict:
    """The QA check row both export QA reports carry for this scan; any leak fails it."""
    parts = leak_parts(leaks)
    unreadable = sum(1 for leak in leaks if leak["finding_id"] is None)
    return {
        "name": "Approved items absent from every part of the file",
        "status": "fail" if leaks else "pass",
        "detail": (
            f"{leaked_item_count(leaks)} approved item(s) still appear and {unreadable} part(s) could not be read, "
            "searching every part of the exported file rather than only the text the redactor edits"
            + (": " + ", ".join(parts[:10]) + (", …" if len(parts) > 10 else "") if parts else "")
            + "."
        ),
    }


def _scan(findings, run, tokens=()) -> list[dict]:
    try:
        groups: dict[tuple, tuple[SampleMatcher, SampleMatcher, list]] = {}
        compiled: dict[tuple[str, bool], tuple[SampleMatcher, SampleMatcher]] = {}
        for finding in map(dict, findings):
            if (finding.get("review_status") or "approved") not in SCANNED_REVIEW_STATUSES:
                continue
            for needle in finding_needles(finding.get("category"), finding.get("sample")):
                sample = " ".join(needle.split())
                if not sample:
                    continue
                standalone = is_standalone_sample(finding.get("category"), needle, finding.get("source"))
                if (sample, standalone) not in compiled:
                    compiled[(sample, standalone)] = (SampleMatcher.compile(sample, standalone), SampleMatcher.compile(sample))
                matcher, word = compiled[(sample, standalone)]
                # Keyed on what the matchers DO, so "Deniz Kurgusal" and "DENİZ
                # KURGUSAL" -- the same pattern and the same fold -- share a search.
                key = (matcher.pattern.pattern, matcher.folded, word.pattern.pattern, word.folded)
                groups.setdefault(key, (matcher, word, []))[2].append((finding.get("id"), finding.get("category") or ""))
        prepared = [_Finding(tuple(members), matcher, word) for matcher, word, members in groups.values()]
        if not prepared:
            return []
        scanner = _Scanner(prepared, tokens)
        run(scanner)
        return sorted(scanner.leaks.values(), key=lambda leak: (leak["part"], str(leak["finding_id"]), leak["category"]))
    finally:
        # Every approved sample was compiled into re's module cache as a
        # readable pattern string; see docx_redactor.case_insensitive_pattern.
        purge_target_pattern_cache()


class _Scanner:
    def __init__(self, findings: list[_Finding], tokens=()):
        self.findings = findings
        self.tokens = re.compile("|".join(map(re.escape, tokens))) if tokens else None
        self.leaks: dict[tuple, dict] = {}
        self.budget = MAX_SCANNED_BYTES

    def spend(self, size: int) -> bool:
        """Take ``size`` decompressed bytes from the scan's budget; False once it would run out."""
        if size > self.budget:
            self.budget = 0
            return False
        self.budget -= size
        return True

    # -- recording ---------------------------------------------------------

    def search(self, part: str, texts) -> None:
        text = FoldedText.of(_SEPARATOR.join(item for item in texts if item))
        if not text.text:
            return
        for finding in self.findings:
            if finding.matcher.found_in(text):
                self.record(part, finding)

    def search_words(self, part: str, texts) -> None:
        """Like ``search``, but a sample counts only as a whole word of one text (see the module docstring).

        The texts are joined by a NUL, which is neither a letter nor a digit and
        never in a sample: a match cannot span two of them, and each one's ends
        bound a word exactly as the end of a separate string would. One search
        per finding, however many attribute values a part has.
        """
        text = FoldedText.of(_SEPARATOR.join(item for item in texts if item))
        if not text.text:
            return
        for finding in self.findings:
            if finding.word.found_as_word_in(text):
                self.record(part, finding)

    def search_components(self, part: str, texts) -> None:
        """Like ``search_words``, by the capitalised camelCase component rule (``SampleMatcher.found_as_component_in``)."""
        text = ComponentText.of(item for item in texts if item)
        if not text.text:
            return
        for finding in self.findings:
            if finding.word.found_as_component_in(text):
                self.record(part, finding)

    def record(self, part: str, finding: _Finding) -> None:
        for finding_id, category in finding.members:
            self.leaks[(part, finding_id)] = {"part": part, "finding_id": finding_id, "category": category}

    def unreadable(self, part: str) -> None:
        self.leaks[(part, None)] = {"part": part, "finding_id": None, "category": UNREADABLE}

    def label(self, prefix: str, name: str, fallback: str) -> str:
        """``prefix + name`` for a standard-shaped part name, else ``prefix + fallback``.

        A member's name is text the document chose, and the label goes into a
        refusal message, an audit row and a QA report. So it is echoed only when
        it has the shape OOXML part names have -- lowerCamel words, digits and
        extensions ("word/footer3.xml", "customXml/itemProps1.xml") -- and holds
        no approved sample; anything else ("docProps/Mehmet Yılmaz.xml") is
        reported by position. "Holds" is tested with the broadest rule there is
        -- every sample as a case-insensitive substring, a bare surname included
        -- because no matching rule may decide that a sample is fit to echo:
        "docProps/kale.xml" is reported by position for an approved "Kale".
        """
        text = FoldedText.of(name)
        if not _STANDARD_PART_NAME.fullmatch(name) or any(finding.word.found_in(text) for finding in self.findings):
            return prefix + fallback
        return prefix + name

    # -- DOCX and any other zip package --------------------------------------

    def package(self, data: bytes, prefix: str, depth: int = 0) -> None:
        try:
            archive = ZipFile(BytesIO(data))
        except (BadZipFile, ValueError, OSError):
            self.unreadable(prefix + "package")
            return
        with archive:
            infos = archive.infolist()
            # A part name is an identifier like an attribute value ("word/endnotes.xml"
            # holds "Er" and "Dal"), so it is matched by the same whole-word rule.
            self.search_words(prefix + "package member names", [info.filename for info in infos])
            self.search(
                prefix + "package comments",
                [archive.comment.decode("utf-8", errors="replace"), *(info.comment.decode("utf-8", errors="replace") for info in infos)],
            )
            for index, info in enumerate(infos, start=1):
                if info.is_dir():
                    continue
                part = self.label(prefix, info.filename, f"member #{index}")
                if not self.spend(info.file_size):
                    self.unreadable(part)
                    continue
                try:
                    member = archive.read(info)
                except Exception:  # a corrupt or encrypted member cannot be shown clean
                    self.unreadable(part)
                    continue
                if member.startswith(b"PK\x03\x04"):
                    if depth + 1 >= MAX_PACKAGE_DEPTH:
                        self.unreadable(part)
                    else:
                        self.package(member, part + "/", depth + 1)
                elif _looks_like_xml(info.filename, member):
                    self.xml(part, member)
                elif (as_text := _text_member(member)) is not None:
                    # Not XML but text all the same -- an altChunk's .htm, .txt
                    # or .rtf, a JSON or CSV someone put in the package.
                    self.search(part, [as_text])

    def xml(self, part: str, data: bytes) -> None:
        text = _decode_xml(data)
        # A declared entity is text assembled at parse time ("&a;&b;" from two
        # declarations), which no search of the source can match. OOXML has no
        # use for one, so a part that declares any cannot be shown clean.
        if text is None or "<!ENTITY" in text:
            self.unreadable(part)
            return
        content, spaced, attributes, markup = [], [], [], []
        element = ""
        in_style = False  # inside a w:style, whose w:name child names the style
        for kind, value, start in _xml_tokens(text):
            if kind == "text" or kind == "cdata":
                piece = value if kind == "cdata" else html.unescape(value)
                if part == "docProps/app.xml" and element in _APP_BOOLEAN_PROPERTIES and piece in ("true", "false"):
                    # A fixed boolean property's value: "Al" is in "false".
                    continue
                content.append(piece)
                spaced.append(piece)
            elif kind == "tag":
                qualified = _ELEMENT_NAME_RE.match(value).group(1)
                name = qualified.rpartition(":")[2]
                # The element a following text node belongs to: none after a closing tag.
                element = "" if value.startswith("/") else name
                if qualified == "w:style":
                    in_style = not value.startswith("/") and not value.rstrip().endswith("/")
                owner = "w:style/w:name" if qualified == "w:name" and in_style else qualified
                if name in _TEXT_BOUNDARY_ELEMENTS:
                    content.append(_SEPARATOR)
                    spaced.append(_SEPARATOR)
                else:
                    spaced.append(" ")
                for attribute in _ATTRIBUTE_RE.finditer(value):
                    raw = attribute.group(1) if attribute.group(1) is not None else attribute.group(2)
                    if raw in _SYSTEM_VALUE_WORDS and raw in _SYSTEM_VALUES.get(
                        (owner, _attribute_name(value, attribute.start())), ()
                    ):
                        # A Word system value on the one attribute it is a system value of (see _SYSTEM_VALUES).
                        continue
                    attributes.extend(attribute_value_texts(html.unescape(raw)))
            elif kind == "instruction" and start == 0 and (match := _XML_DECLARATION_RE.match(value)):
                # The prolog (<?xml version="1.0" standalone="yes"?>) is the
                # parser's, not the document's: neither a text node nor an
                # attribute value, and "Tan" is in "standalone". Only the
                # declaration at offset 0, and only its version, encoding and
                # standalone; anything else written into it is still read, as
                # is any "<?xml" instruction later in the part.
                if value[match.end():].strip():
                    markup.append(html.unescape(value[match.end():]))
            else:
                markup.append(html.unescape(value))
        joined, joined_spaced = "".join(content), "".join(spaced)
        if self.tokens is not None:
            joined, joined_spaced = (self.tokens.sub(_SEPARATOR, view) for view in (joined, joined_spaced))
        self.search(part, [joined, joined_spaced, *markup])
        self.search_words(part, attributes)
        self.search_components(part, attributes)

    # -- PDF -----------------------------------------------------------------

    def pdf(self, data: bytes, prefix: str, depth: int = 0) -> None:
        try:
            doc = pymupdf.open(stream=data, filetype="pdf")
        except Exception:  # PyMuPDF raises its own types for a broken file
            self.unreadable(prefix + "document")
            return
        with doc:
            if doc.needs_pass:
                self.unreadable(prefix + "document")
                return
            # Objects first: they charge every stream to the budget before
            # anything inflates it. The pages' text is then read only if no
            # stream was refused -- an attached PDF's content streams were never
            # opened by the redactor, and get_text would inflate them unbounded.
            refused = len(self.leaks)
            self._pdf_objects(doc, prefix)
            if any(key[1] is None for key in list(self.leaks)[refused:]):
                return
            self._pdf_pages(doc, prefix)
            self._pdf_document_level(doc, prefix)
            self._pdf_embedded_files(doc, prefix, depth)

    def _pdf_pages(self, doc, prefix: str) -> None:
        for number, page in enumerate(doc, start=1):
            where = f"{prefix}page {number}"
            try:
                self.search(f"{where} text", [page.get_text("text")])
                self.search(f"{where} label", [page.get_label()])
                self.search(
                    f"{where} links",
                    [
                        value
                        for link in page.get_links()
                        for key in ("uri", "file", "nameddest", "name")
                        if isinstance(link.get(key), str)
                        for value in (link[key], unquote(link[key]))
                    ],
                )
                self.search(
                    f"{where} annotations",
                    [str(value) for annot in page.annots() for value in (annot.info or {}).values()],
                )
                self.search(
                    f"{where} form fields",
                    [
                        str(value)
                        for widget in page.widgets()
                        for value in (
                            widget.field_name,
                            widget.field_value,
                            widget.field_label,
                            *(widget.choice_values or ()),
                        )
                        if value is not None
                    ],
                )
            except Exception:  # an unreadable page cannot be shown clean
                self.unreadable(where)

    def _pdf_document_level(self, doc, prefix: str) -> None:
        self.search(f"{prefix}outline", [str(entry[1]) for entry in doc.get_toc(simple=False)])
        self.search(
            f"{prefix}page labels",
            [str(rule.get("prefix") or "") for rule in doc.get_page_labels()],
        )
        self.search(f"{prefix}info", [str(value) for value in (doc.metadata or {}).values() if value])
        xmp = doc.get_xml_metadata() or ""
        if xmp.strip():
            self.xml(f"{prefix}xmp", xmp.encode("utf-8"))
        try:
            self.search(f"{prefix}trailer", _pdf_source_texts(doc.pdf_trailer()))
        except Exception:
            self.unreadable(f"{prefix}trailer")

    def _pdf_objects(self, doc, prefix: str) -> None:
        font_programs = _pdf_font_program_xrefs(doc)
        for xref in range(1, doc.xref_length()):
            part = f"{prefix}object {xref}"
            try:
                source = doc.xref_object(xref, compressed=False) or ""
            except Exception:
                self.unreadable(part)
                continue
            texts = _pdf_source_texts(source)
            if doc.xref_is_stream(xref) and xref not in font_programs and not _is_image_stream(source):
                try:
                    stream = self.bounded_stream(doc, xref)
                except Exception:
                    stream = None
                if stream is None:
                    self.unreadable(f"{part} stream")
                elif stream:
                    decoded = stream.decode("latin-1")
                    # An XMP packet or a /PieceInfo payload is UTF-8: read as
                    # Latin-1 alone, every non-ASCII letter of a Turkish name
                    # becomes two characters and the name is never found.
                    texts.extend([decoded, stream.decode("utf-8", errors="replace"), *_pdf_source_texts(decoded)])
                    texts.append(_decode_bom(stream))
            self.search(part, texts)

    def bounded_stream(self, doc, xref: int) -> bytes | None:
        """A stream's decoded bytes, charged to the budget BEFORE PyMuPDF inflates it; None past it.

        A few kilobytes of Flate can inflate to gigabytes, and PyMuPDF has no
        bounded decode. So a FlateDecode stream is first inflated here with a
        cap, only to learn its size; any other filter is charged its worst-case
        expansion up front. Only then is PyMuPDF asked for the decoded bytes,
        which also applies predictors and filter chains.
        """
        raw = doc.xref_stream_raw(xref) or b""
        filters = _pdf_filters(doc, xref)
        if filters and filters[0] in ("FlateDecode", "Fl"):
            inflater = zlib.decompressobj()
            try:
                inflated = len(inflater.decompress(raw, self.budget + 1))
            except zlib.error:
                return None
            bound = inflated * _worst_expansion(filters[1:])
        else:
            bound = len(raw) * _worst_expansion(filters)
        if not self.spend(bound):
            return None
        return doc.xref_stream(xref) or b""

    def _pdf_embedded_files(self, doc, prefix: str, depth: int) -> None:
        for index in range(doc.embfile_count()):
            part = f"{prefix}embedded file #{index + 1}"
            try:
                info = doc.embfile_info(index)
                # The declared size is all there is to charge before PyMuPDF
                # inflates the attachment; one that declares none is not read.
                declared = int(info.get("size") or -1)
                if declared < 0 or not self.spend(declared):
                    self.unreadable(part)
                    continue
                content = doc.embfile_get(index)
            except Exception:
                self.unreadable(part)
                continue
            self.search(
                f"{part} name",
                [str(info.get(key) or "") for key in ("name", "filename", "ufilename", "description", "desc")],
            )
            self.payload(content, part, depth)

    def payload(self, content: bytes, part: str, depth: int) -> None:
        """An attached file: a package or a PDF is opened, anything else read as text."""
        if content.startswith(b"PK\x03\x04") or content.startswith(b"%PDF"):
            if depth + 1 >= MAX_PACKAGE_DEPTH:
                self.unreadable(part)
            elif content.startswith(b"PK"):
                self.package(content, part + "/", depth + 1)
            else:
                self.pdf(content, part + "/", depth + 1)
            return
        self.search(part, [content.decode("utf-8", errors="replace"), content.decode("latin-1"), _decode_bom(content)])


# -- XML --------------------------------------------------------------------

_STANDARD_PART_NAME = re.compile(r"(?:(?:_rels|[a-z][A-Za-z]*\d*(?:\.[a-z]+)*)/)*(?:\[Content_Types\]\.xml|\.rels|[a-z][A-Za-z]*\d*(?:\.[a-z]+)*)")

# (opening delimiter, closing delimiter, token kind), longest opener first.
_XML_MARKUP = (
    ("<!--", "-->", "comment"),
    ("<![CDATA[", "]]>", "cdata"),
    ("<?", "?>", "instruction"),
    ("<!", ">", "declaration"),
    ("<", ">", "tag"),
)
# An attribute's value, after "=". Anchored on the "=" rather than on the
# attribute name: a name pattern like [^\s=]+ retried from every character of a
# long unbroken run is quadratic (measured: 53 s on a 100 KB tag).
_ATTRIBUTE_RE = re.compile(r"""=\s*(?:"([^"]*)"|'([^']*)')""")
_ELEMENT_NAME_RE = re.compile(r"/?([^\s/>]*)")
# The XML declaration's version, encoding and standalone, in the order XML
# requires them; what follows a match is not the declaration's and is read.
_XML_DECLARATION_RE = re.compile(
    r"""xml\s+version\s*=\s*(["'])1\.[0-9]+\1"""
    r"""(?:\s+encoding\s*=\s*(["'])[A-Za-z][A-Za-z0-9._-]*\2)?"""
    r"""(?:\s+standalone\s*=\s*(["'])(?:yes|no)\3)?\s*"""
)
# Word splits a name across runs, never across a paragraph or a table cell.
_TEXT_BOUNDARY_ELEMENTS = frozenset({"p", "tc"})
# The value-level exceptions, each exact.
#
# Word writes some words that are also people's names as whole attribute
# values -- the theme's script fonts ("Shruti", "Mangal", "Latha"), "Times New
# Roman", built-in table styles ("Light Grid"), and the enumerations "auto",
# "single", "double", "center", "left" and "right" -- in every package, so an approved "Mangal" or "Auto" could never
# export. Each such value is skipped only on the attribute where Word defines
# it as a system value, and only exactly: (qualified element, qualified
# attribute) -> the values skipped there. Anywhere else -- a text node, a
# document property, a comment, a document variable, any other attribute --
# the same word is read like any other. Fonts are listed by name, never by
# what looks like a font.
#
# The fonts are the typefaces of Office's stock theme font schemes (one per
# script) and its font table; the theme's major/minor font references
# ("minorEastAsia", whose "Asia" is a capitalised component) are ST_Theme.
_THEME_FONTS = frozenset(
    {
        "Angsana New", "Aparajita", "Arial", "Calibri", "Calibri Light", "Cambria", "Cordia New", "DaunPenh",
        "DokChampa", "Ebrima", "Estrangelo Edessa", "Euphemia", "Gadugi", "Gautami", "Iskoola Pota",
        "Javanese Text", "Kalinga", "Kartika", "Latha", "Leelawadee", "Leelawadee UI", "MV Boli", "Mangal",
        "Microsoft Himalaya", "Microsoft JhengHei", "Microsoft New Tai Lue", "Microsoft Tai Le",
        "Microsoft Uighur", "Microsoft Yi Baiti", "Mongolian Baiti", "MoolBoran", "Myanmar Text", "Nirmala UI",
        "Nyala", "Phagspa", "Plantagenet Cherokee", "Raavi", "Segoe UI", "Shruti", "Sylfaen", "Symbol",
        "Times New Roman", "Tunga", "Vrinda",
    }
)
_THEME_FONT_REFERENCES = frozenset(
    f"{major_minor}{script}" for major_minor in ("major", "minor") for script in ("Ascii", "HAnsi", "EastAsia", "Bidi")
)
# Word's built-in table styles whose names carry "Light", exactly as Word
# writes them: the 2007 "Light Grid/List/Shading" family and the 2013 "Grid
# Table 1 Light" / "List Table 1 Light" ("1" is the only number Word gives a
# Light variant), each plain and in Accent 1-6.
_BUILT_IN_LIGHT_STYLES = frozenset(
    f"{base}{accent}"
    for base in ("Light Grid", "Light List", "Light Shading", "Grid Table 1 Light", "List Table 1 Light")
    for accent in ("", *(f" Accent {number}" for number in range(1, 7)))
)
# The same styles' ids ("LightGrid-Accent1"), whose "Light" is a capitalised
# camelCase component. Exact ids only: any other id is read by that rule.
_BUILT_IN_LIGHT_STYLE_IDS = frozenset(
    f"{base}{accent}"
    for base in ("LightGrid", "LightList", "LightShading", "GridTable1Light", "ListTable1Light")
    for accent in ("", *(f"-Accent{number}" for number in range(1, 7)))
)
_BORDER_ELEMENTS = frozenset(
    f"w:{name}"
    for name in ("top", "bottom", "left", "right", "start", "end", "insideH", "insideV", "between", "bar", "tl2br", "tr2bl", "bdr")
)
_ALIGNMENT = frozenset({"center", "left", "right"})
_AUTO = frozenset({"auto"})
_ROMAN_NUMBER_FORMATS = frozenset({"lowerRoman", "upperRoman"})
_SYSTEM_VALUES: dict[tuple[str, str], frozenset[str]] = {
    # A border's w:val="nil" is Word's "no border": "Nil" is in it.
    **{(element, "w:val"): frozenset({"nil", "single", "double"}) for element in _BORDER_ELEMENTS},
    **{(element, "w:color"): _AUTO for element in _BORDER_ELEMENTS},
    **{("w:rFonts", attribute): _THEME_FONTS for attribute in ("w:ascii", "w:hAnsi", "w:cs", "w:eastAsia")},
    **{
        ("w:rFonts", attribute): _THEME_FONT_REFERENCES
        for attribute in ("w:asciiTheme", "w:hAnsiTheme", "w:eastAsiaTheme", "w:cstheme")
    },
    **{(element, "typeface"): _THEME_FONTS for element in ("a:latin", "a:ea", "a:cs", "a:font")},
    ("w:font", "w:name"): _THEME_FONTS,
    ("w:family", "w:val"): frozenset({"roman", "auto"}),
    ("w:numFmt", "w:val"): _ROMAN_NUMBER_FORMATS,
    ("w:pgNumType", "w:fmt"): _ROMAN_NUMBER_FORMATS,
    ("w:jc", "w:val"): _ALIGNMENT,
    ("w:lvlJc", "w:val"): _ALIGNMENT,
    ("w:tab", "w:val"): _ALIGNMENT,
    ("w:vAlign", "w:val"): frozenset({"center"}),
    ("w:textAlignment", "w:val"): frozenset({"auto", "center"}),
    ("w:u", "w:val"): frozenset({"single", "double"}),
    ("w:color", "w:val"): _AUTO,
    ("w:shd", "w:color"): _AUTO,
    ("w:shd", "w:fill"): _AUTO,
    ("w:spacing", "w:lineRule"): _AUTO,
    ("w:trHeight", "w:hRule"): _AUTO,
    **{(element, "w:type"): _AUTO for element in ("w:tblW", "w:tcW", "w:tblInd", "w:tblCellSpacing", "w:wBefore", "w:wAfter")},
    # A built-in table style's name, both where styles.xml defines the style and
    # where its latent-style list names it. Any other style name is read: a
    # style named after the client is the client's name.
    ("w:lsdException", "w:name"): _BUILT_IN_LIGHT_STYLES,
    ("w:style/w:name", "w:val"): _BUILT_IN_LIGHT_STYLES,
    # ... and where it is defined by id, or referred to.
    ("w:style", "w:styleId"): _BUILT_IN_LIGHT_STYLE_IDS,
    **{
        (element, "w:val"): _BUILT_IN_LIGHT_STYLE_IDS
        for element in ("w:pStyle", "w:rStyle", "w:tblStyle", "w:basedOn", "w:next", "w:link")
    },
}
_SYSTEM_VALUE_WORDS = frozenset(value for values in _SYSTEM_VALUES.values() for value in values)
_APP_BOOLEAN_PROPERTIES = frozenset({"ScaleCrop", "LinksUpToDate", "SharedDoc", "HyperlinksChanged"})
_ATTRIBUTE_NAME_RE = re.compile(r"([^\s=]+)\s*$")


def _attribute_name(tag: str, equals: int) -> str:
    """The qualified name of the attribute whose "=" is at ``equals`` (looks back a bounded distance)."""
    found = _ATTRIBUTE_NAME_RE.search(tag[max(0, equals - 64):equals])
    return found.group(1) if found else ""


def _xml_tokens(text: str):
    """``(kind, value, offset)`` for each run of text and each piece of markup, in one linear pass.

    A hand-written walk with str.find, not a regex of lazy alternatives: an
    unterminated "<!--" made that regex search to the end of the part from every
    "<" after it, which is quadratic. An unterminated token here runs to the end
    of the part and is still searched.
    """
    index = 0
    length = len(text)
    while index < length:
        start = text.find("<", index)
        if start < 0:
            yield "text", text[index:], index
            return
        if start > index:
            yield "text", text[index:start], index
        for opener, closer, kind in _XML_MARKUP:
            if text.startswith(opener, start):
                end = text.find(closer, start + len(opener))
                stop = length if end < 0 else end
                yield kind, text[start + len(opener):stop], start
                index = length if end < 0 else end + len(closer)
                break


_XML_ENCODING_RE = re.compile(rb"""^\s*<\?xml[^>]*\bencoding\s*=\s*["']([A-Za-z0-9._-]+)["']""")
_XML_NAME_SUFFIXES = (".xml", ".rels", ".vml")


# Byte patterns XML's encoding autodetection (XML 1.0, appendix F) recognises
# without a byte-order mark. Decoding UTF-16 as UTF-8 does not fail -- ASCII
# letters come out interleaved with NULs -- so without this a UTF-16 part reads
# as clean text in which no sample can match.
_UNMARKED_ENCODINGS = (
    (b"\x00\x00\x00<", "utf-32-be"),
    (b"<\x00\x00\x00", "utf-32-le"),
    (b"\x00<\x00?", "utf-16-be"),
    (b"<\x00?\x00", "utf-16-le"),
    (b"\x00<", "utf-16-be"),
    (b"<\x00", "utf-16-le"),
)


def _unmarked_encoding(data: bytes) -> str | None:
    return next((encoding for prefix, encoding in _UNMARKED_ENCODINGS if data.startswith(prefix)), None)


def _looks_like_xml(name: str, data: bytes) -> bool:
    if name.lower().endswith(_XML_NAME_SUFFIXES):
        return True
    head = data[:64]
    for bom in (codecs.BOM_UTF8, codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE):
        if head.startswith(bom):
            return True
    return _unmarked_encoding(head) is not None or head.lstrip().startswith(b"<")


def _text_member(data: bytes) -> str | None:
    """A member's text if it is text (by BOM, else NUL-free strict UTF-8); None for binary.

    Strict, because images and font programs are not searched: a short sample
    matched case-insensitively turns up by chance in random bytes.
    """
    decoded = _decode_bom(data, strict=True)
    if decoded:
        return decoded
    if b"\0" in data:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _decode_xml(data: bytes) -> str | None:
    """The part as text, by its BOM, then its declared encoding, then UTF-8; None if none of them reads it."""
    if data.startswith((codecs.BOM_UTF8, codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return _decode_bom(data, strict=True)
    unmarked = _unmarked_encoding(data)
    if unmarked:
        try:
            return data.decode(unmarked)
        except UnicodeDecodeError:
            return None
    declared = _XML_ENCODING_RE.match(data)
    encoding = declared.group(1).decode("ascii") if declared else "utf-8"
    try:
        return data.decode(encoding)
    except (LookupError, UnicodeDecodeError):
        return None


def _decode_bom(data: bytes, strict: bool = False) -> str | None:
    """Text decoded by its byte-order mark; "" without one, None when strict decoding fails."""
    for bom, codec in ((codecs.BOM_UTF8, "utf-8-sig"), (codecs.BOM_UTF16_LE, "utf-16"), (codecs.BOM_UTF16_BE, "utf-16")):
        if data.startswith(bom):
            try:
                return data.decode(codec, errors="strict" if strict else "replace")
            except UnicodeDecodeError:
                return None
    return ""


# -- PDF object source --------------------------------------------------------

_PDF_SYNTAX_RE = re.compile(r"[(<%]")
_PDF_NAME_RE = re.compile(r"/([^\s/<>\[\]()%{}]+)")
_PDF_NAME_ESCAPE_RE = re.compile(r"#([0-9A-Fa-f]{2})")
_PDF_ESCAPES = {"n": "\n", "r": "\r", "t": "\t", "b": "\b", "f": "\f"}


def _pdf_source_texts(source: str) -> list[str]:
    """The string literals, hex strings and names in a PDF object's source, decoded."""
    texts: list[str] = []
    index = 0
    length = len(source)
    while index < length:
        found = _PDF_SYNTAX_RE.search(source, index)
        if not found:
            break
        index = found.start()
        char = source[index]
        if char == "(":
            raw, index = _pdf_literal(source, index + 1)
            texts.extend(_pdf_texts(raw))
        elif char == "<" and source.startswith("<<", index):
            index += 2
        elif char == "<":
            end = source.find(">", index + 1)
            if end < 0:
                break
            digits = re.sub(r"\s", "", source[index + 1:end])
            if re.fullmatch(r"[0-9A-Fa-f]*", digits):
                texts.extend(_pdf_texts(bytes.fromhex(digits + "0" * (len(digits) % 2))))
            index = end + 1
        else:  # "%" opens a comment that runs to the end of the line
            end = source.find("\n", index)
            index = length if end < 0 else end + 1
    texts.extend(
        _PDF_NAME_ESCAPE_RE.sub(lambda match: chr(int(match.group(1), 16)), name)
        for name in _PDF_NAME_RE.findall(source)
        if "#" in name
    )
    return texts


def _pdf_literal(source: str, index: int) -> tuple[bytes, int]:
    """A literal string's bytes, from just after its "(" to just after its ")"."""
    out = bytearray()
    depth = 1
    length = len(source)
    while index < length:
        char = source[index]
        if char == "\\" and index + 1 < length:
            following = source[index + 1]
            if following in "01234567":
                digits = re.match(r"[0-7]{1,3}", source[index + 1:index + 4]).group(0)
                out.append(int(digits, 8) & 0xFF)
                index += 1 + len(digits)
                continue
            if following in "\r\n":  # a line continuation: the break is not part of the string
                index += 3 if source.startswith("\r\n", index + 1) else 2
                continue
            out.extend(_PDF_ESCAPES.get(following, following).encode("latin-1", errors="replace"))
            index += 2
            continue
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return bytes(out), index + 1
        out.extend(char.encode("latin-1", errors="replace"))
        index += 1
    return bytes(out), index


def _pdf_texts(raw: bytes) -> list[str]:
    """A PDF string's text, every way it may be encoded.

    UTF-16 or UTF-8 by its BOM; otherwise both PDFDocEncoding (read as
    Latin-1) and UTF-8, which PDF 2.0 strings and much real-world software
    write without a mark. Each decoding is searched.
    """
    marked = _decode_bom(raw)
    if marked:
        return [marked]
    return [raw.decode("latin-1"), raw.decode("utf-8", errors="replace")]


# The most a filter can multiply its input by. Image codecs are absent: image
# streams are not decoded at all, so a non-image stream using one is refused.
_FILTER_EXPANSION = {
    "FlateDecode": 1032,  # zlib's own ceiling
    "Fl": 1032,
    "LZWDecode": 4096,  # a 12-bit code can emit a 4096-byte table entry
    "LZW": 4096,
    "RunLengthDecode": 128,
    "RL": 128,
    "ASCIIHexDecode": 1,
    "AHx": 1,
    "ASCII85Decode": 1,
    "A85": 1,
    "Crypt": 1,
}


def _pdf_filters(doc, xref: int) -> list[str]:
    """The stream's own /Filter, read as a key (a nested dictionary's /Filter is not it)."""
    kind, value = doc.xref_get_key(xref, "Filter")
    return re.findall(r"/([A-Za-z0-9]+)", value) if kind in ("name", "array") else []


def _worst_expansion(filters: list[str]) -> int:
    factor = 1
    for name in filters:
        if name not in _FILTER_EXPANSION:
            return 1 << 62  # an unknown or image codec: past any budget
        factor *= _FILTER_EXPANSION[name]
    return factor


def _is_image_stream(source: str) -> bool:
    return re.search(r"/Subtype\s*/Image\b", source) is not None


def _pdf_font_program_xrefs(doc) -> set[int]:
    """Streams holding an embedded font's glyph program (FontFile, FontFile2, FontFile3)."""
    programs = set()
    for xref in range(1, doc.xref_length()):
        for key in ("FontFile", "FontFile2", "FontFile3"):
            try:
                kind, value = doc.xref_get_key(xref, key)
            except Exception:
                continue
            if kind == "xref":
                programs.add(int(value.split()[0]))
    return programs
