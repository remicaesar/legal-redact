"""DOCX package-preserving redaction helpers."""

from __future__ import annotations

import html
import re
import struct
from base64 import b64decode
from dataclasses import dataclass, replace
from io import BytesIO
from pathlib import Path
from urllib.parse import unquote
from zipfile import ZIP_DEFLATED, ZipFile
from xml.etree import ElementTree
from xml.sax.saxutils import escape

# `ElementTree` above is used only for serialization (tostring/register_namespace)
# and type hints, which are safe. PARSING attacker-supplied DOCX bytes goes through
# defusedxml: stdlib xml.etree expands internal entities, so a few-KB "billion
# laughs" part exhausts memory. defusedxml refuses DTDs and entity definitions.
from defusedxml.ElementTree import fromstring as safe_fromstring
from defusedxml.common import DefusedXmlException

from legal_analyzer.turkish_names import tr_fold

WORD_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
XML_SPACE_ATTR = "{http://www.w3.org/XML/1998/namespace}space"
TEXT_TAG = f"{{{WORD_NS}}}t"
# Text Word keeps outside the visible runs, redacted with the same targets as
# w:t: a tracked deletion (w:delText) and a field code (w:instrText, or
# w:delInstrText once deleted) -- Word writes a typed email address into a
# HYPERLINK field code as well as into the hyperlink's relationship.
DEL_TEXT_TAG = f"{{{WORD_NS}}}delText"
INSTR_TEXT_TAG = f"{{{WORD_NS}}}instrText"
DEL_INSTR_TEXT_TAG = f"{{{WORD_NS}}}delInstrText"
REDACTED_TEXT_TAGS = (TEXT_TAG, DEL_TEXT_TAG, INSTR_TEXT_TAG, DEL_INSTR_TEXT_TAG)
TAB_TAG = f"{{{WORD_NS}}}tab"
BREAK_TAG = f"{{{WORD_NS}}}br"
PARAGRAPH_TAG = f"{{{WORD_NS}}}p"
DOCX_TEXT_PARTS = {
    "word/document.xml",
    "word/footnotes.xml",
    "word/endnotes.xml",
    "word/comments.xml",
}


def exact_starts(text: str, sample: str) -> list[int]:
    """Every index at which ``sample`` occurs in ``text`` exactly, overlapping ones included."""
    starts: list[int] = []
    if not sample:
        return starts
    index = text.find(sample)
    while index != -1:
        starts.append(index)
        index = text.find(sample, index + 1)
    return starts


@dataclass(frozen=True)
class Occurrence:
    """Where a finding was found, in terms every rendering can resolve: the
    ``index``-th of the ``count`` exact occurrences of its sample in the text
    detection read.

    A recorded offset belongs to the EXTRACTED text and cannot be carried into a
    DOCX part as a number: extraction joins the parts, reads DrawingML text the
    part's ``w:t`` runs do not hold, and strips and truncates. The ordinal can
    be. A rendering resolves it only when its own text holds exactly ``count``
    exact occurrences of the sample -- the same set the detector saw, in the
    same order -- and otherwise the finding has no own position there and is
    covered by the backstop alone, which is how every occurrence was redacted
    before positions were used at all.
    """

    index: int
    count: int

    @classmethod
    def at(cls, text: str, sample: str, start: int | None) -> "Occurrence | None":
        """The occurrence of ``sample`` that starts at ``start`` in ``text``, if it really is there."""
        if not sample or not isinstance(start, int) or text[start : start + len(sample)] != sample:
            return None
        starts = exact_starts(text, sample)
        return cls(starts.index(start), len(starts))

    def locate(self, text: str, sample: str) -> tuple[int, int] | None:
        starts = exact_starts(text, sample)
        if len(starts) != self.count:
            return None
        return starts[self.index], starts[self.index] + len(sample)


@dataclass(frozen=True)
class RedactionTarget:
    """An approved finding as the resolver sees it.

    ``found_at`` is the finding's own occurrence (see ``Occurrence``); a target
    without one -- a reviewer-added finding, a stale offset -- is redacted by the
    backstop only.
    """

    text: str
    replacement: str
    standalone: bool = False
    found_at: Occurrence | None = None


@dataclass(frozen=True)
class RetainedSpan:
    """A finding the reviewer RETAINED: real, and deliberately left in the document."""

    text: str
    found_at: Occurrence


@dataclass(frozen=True)
class HeldMatch:
    """A backstop match left in place because it sits wholly inside a retained finding.

    ``target_index`` indexes the targets, ``retained_index`` the retained spans
    handed to ``resolve_redaction``. Only matches no selected region covers are
    reported: those are the characters that stay.
    """

    start: int
    end: int
    target_index: int
    retained_index: int


@dataclass(frozen=True)
class Resolution:
    regions: list[tuple[int, int, str, int]]
    held: list[HeldMatch]


DETECTOR_SOURCES = frozenset({"detector", "ocr"})


def is_standalone_sample(category: str | None, sample: str | None, source: str | None) -> bool:
    """Whether a finding's sample is matched only as a capitalised whole word.

    True only for the detector's bare-surname mention: every person name the
    detector finds otherwise has at least two words, so a one-word
    natural_person_name from the detector is that mention. Many such surnames
    are ordinary Turkish words or prefixes of them (Bor/borcunu, Kale/kalemine,
    Can/can güvenliği), and matched as a substring widened to word bounds,
    approving one rewrote those words as the person throughout the deliverable.

    A reviewer-added finding, whatever its shape, keeps substring matching:
    the reviewer typed it to have it removed, inflected forms ("Yılmazın")
    included. So does every other sample, which is what catches inflected and
    run-together occurrences of a multi-word name or a number.
    """
    return (
        category == "natural_person_name"
        and source in DETECTOR_SOURCES
        and len((sample or "").split()) == 1
    )


def sample_match(sample: str | RedactionTarget) -> tuple[str, bool]:
    """``(text, standalone)`` for a sensitive sample handed to export QA."""
    if isinstance(sample, RedactionTarget):
        return sample.text, sample.standalone
    return sample, False


@dataclass(frozen=True)
class TextSpan:
    node: ElementTree.Element
    start: int
    end: int
    text: str


# word/glossary/ holds the glossary document -- building blocks, AutoText and
# Quick Parts saved with the file -- which is a second Word document with the
# same part names as the first. Its parts are classified as their main-document
# namesakes: glossary/document.xml carries text the redactor rewrites in place.
GLOSSARY_PREFIX = "word/glossary/"


def is_docx_text_part(name: str) -> bool:
    if name.startswith(GLOSSARY_PREFIX):
        name = "word/" + name[len(GLOSSARY_PREFIX):]
    return name in DOCX_TEXT_PARTS or (
        name.startswith("word/header") and name.endswith(".xml")
    ) or (
        name.startswith("word/footer") and name.endswith(".xml")
    )


# Document properties. Word fills them from the author's account and the firm's
# template, and saves the title a lawyer typed -- often the client's name -- so
# they are identifying metadata the review canvas never shows. A reviewed export
# rewrites all three, rather than redacting the approved targets inside them,
# because what they leak (author, firm, manager) is usually never a finding.
CORE_PROPERTIES_PART = "docProps/core.xml"
APP_PROPERTIES_PART = "docProps/app.xml"
CUSTOM_PROPERTIES_PART = "docProps/custom.xml"
DOCX_METADATA_PARTS = (CORE_PROPERTIES_PART, APP_PROPERTIES_PART, CUSTOM_PROPERTIES_PART)
APP_PROPERTIES_NS = "http://schemas.openxmlformats.org/officeDocument/2006/extended-properties"
# The only app.xml properties an export keeps: they describe the producing
# software and the package, never a person, a firm or the document's subject.
# Everything else -- Company, Manager, Template, HyperlinkBase, TitlesOfParts
# (where Word copies the title) -- is dropped. An allowlist, so a property this
# list has never heard of is dropped rather than shipped.
APP_PROPERTIES_KEPT = frozenset(
    {"Application", "AppVersion", "DocSecurity", "ScaleCrop", "LinksUpToDate", "SharedDoc", "HyperlinksChanged"}
)
EMPTY_CORE_PROPERTIES = (
    b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    b'<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties"'
    b' xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/"'
    b' xmlns:dcmitype="http://purl.org/dc/dcmitype/" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"/>'
)
EMPTY_CUSTOM_PROPERTIES = (
    b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    b'<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/custom-properties"'
    b' xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes"/>'
)


# docProps/thumbnail.* is a picture of the first page as last saved -- the
# unredacted page. It is swapped for a blank image of the same format, which
# keeps its relationship and content type valid. A format with no blank here is
# emptied rather than shipped.
BLANK_THUMBNAILS = {
    ".png": b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAACXBIWXMAAA7EAAAOxAGVKw4bAAAADElEQVR4nGP4//8/AAX+Av4N70a4AAAAAElFTkSuQmCC"
    ),
    ".jpeg": b64decode(
        "/9j/4AAQSkZJRgABAQEAYABgAAD/2wBDAAIBAQEBAQIBAQECAgICAgQDAgICAgUEBAMEBgUGBgYFBgYGBwkIBgcJBwYGCAsICQoKCgoKBggL"
        "DAsKDAkKCgr/2wBDAQICAgICAgUDAwUKBwYHCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgr/wgAR"
        "CAABAAEDAREAAhEBAxEB/8QAFAABAAAAAAAAAAAAAAAAAAAACP/EABQBAQAAAAAAAAAAAAAAAAAAAAD/2gAMAwEAAhADEAAAAX8f/8QAFBAB"
        "AAAAAAAAAAAAAAAAAAAAAP/aAAgBAQABBQJ//8QAFBEBAAAAAAAAAAAAAAAAAAAAAP/aAAgBAwEBPwF//8QAFBEBAAAAAAAAAAAAAAAAAAAA"
        "AP/aAAgBAgEBPwF//8QAFBABAAAAAAAAAAAAAAAAAAAAAP/aAAgBAQAGPwJ//8QAFBABAAAAAAAAAAAAAAAAAAAAAP/aAAgBAQABPyF//9oA"
        "DAMBAAIAAwAAABAf/8QAFBEBAAAAAAAAAAAAAAAAAAAAAP/aAAgBAwEBPxB//8QAFBEBAAAAAAAAAAAAAAAAAAAAAP/aAAgBAgEBPxB//8QA"
        "FBABAAAAAAAAAAAAAAAAAAAAAP/aAAgBAQABPxB//9k="
    ),
}
BLANK_THUMBNAILS[".jpg"] = BLANK_THUMBNAILS[".jpeg"]
# An EMF holding only its header and EMR_EOF: no drawing records.
BLANK_THUMBNAILS[".emf"] = struct.pack(
    "<II4i4iIIIIHHIII2i2i",
    1, 88, 0, 0, 0, 0, 0, 0, 26, 26, 0x464D4520, 0x10000, 108, 2, 1, 0, 0, 0, 0, 1024, 768, 320, 240,
) + struct.pack("<IIIII", 14, 20, 0, 16, 20)
# A WMF holding only its header and META_EOF.
BLANK_THUMBNAILS[".wmf"] = struct.pack("<HHHIHIH", 1, 9, 0x300, 12, 0, 3, 0) + struct.pack("<IH", 3, 0)


def is_docx_metadata_part(name: str) -> bool:
    return name in DOCX_METADATA_PARTS


def is_docx_thumbnail_part(name: str) -> bool:
    return name.startswith("docProps/thumbnail")


def blank_thumbnail(name: str) -> bytes:
    return BLANK_THUMBNAILS.get(Path(name).suffix.lower(), b"")


def thumbnail_still_shows_source(name: str, redacted: bytes, source: bytes | None) -> bool:
    """True when the exported thumbnail is the source's own page picture."""
    return bool(redacted) and redacted == source and redacted not in BLANK_THUMBNAILS.values()


# Word stamps who made each tracked change and comment, and when: w:author,
# w:initials and w:date on w:ins/w:del/w:*PrChange/w:comment, w15:author and a
# presence user id (often an email address) on each word/people.xml entry, and
# w16cex:dateUtc in commentsExtensible. Text redaction never touches attributes,
# so every word/ part has these rewritten to one neutral value by local name.
NEUTRAL_REVISION_DATE = "2000-01-01T00:00:00Z"
NEUTRAL_IDENTITY_ATTRIBUTES = {
    "author": "Reviewer",
    "initials": "R",
    "dateUtc": NEUTRAL_REVISION_DATE,
    "date": NEUTRAL_REVISION_DATE,
    "userId": "",
}
_TAG_RE = re.compile(rb"<[^<>!?/][^<>]*>")
_IDENTITY_ATTRIBUTE_RE = re.compile(
    rb"(\s[\w.-]+:)(" + b"|".join(name.encode() for name in NEUTRAL_IDENTITY_ATTRIBUTES) + rb")(\s*=\s*)(\"[^\"]*\"|'[^']*')"
)


def is_word_xml_part(name: str) -> bool:
    return name.startswith("word/") and name.endswith(".xml") and "/_rels/" not in name


def scrub_identity_attributes(xml_bytes: bytes) -> bytes:
    """Rewrite every revision/comment identity attribute to its neutral value.

    Done on the serialized bytes, inside tags only, so the part's namespace
    prefixes -- which mc:Ignorable names literally -- are left as Word wrote them.
    """

    def neutral(match: re.Match[bytes]) -> bytes:
        value = NEUTRAL_IDENTITY_ATTRIBUTES[match.group(2).decode()]
        return match.group(1) + match.group(2) + match.group(3) + b'"' + value.encode() + b'"'

    return _TAG_RE.sub(lambda tag: _IDENTITY_ATTRIBUTE_RE.sub(neutral, tag.group(0)), xml_bytes)


def identity_attribute_fields(name: str, xml_bytes: bytes) -> list[str]:
    """Name the identity attributes a word/ part still carries with a real value."""
    try:
        root = safe_fromstring(xml_bytes)
    except (ElementTree.ParseError, DefusedXmlException):
        return [f"{name}: unreadable"]
    found = set()
    for element in root.iter():
        for key, value in element.attrib.items():
            attribute = local_name(key)
            if attribute in NEUTRAL_IDENTITY_ATTRIBUTES and value != NEUTRAL_IDENTITY_ATTRIBUTES[attribute]:
                found.add(attribute)
    return [f"{name}: {attribute} attribute" for attribute in sorted(found)]


# The attached template is an external relationship whose target is the path
# Word loaded it from -- typically under the author's own user folder.
SETTINGS_RELS_PART = "word/_rels/settings.xml.rels"
_RELATIONSHIP_RE = re.compile(rb"<Relationship\b[^>]*>")
_TARGET_RE = re.compile(rb'(\sTarget\s*=\s*)("[^"]*"|\'[^\']*\')')
_PATH_SEPARATOR_RE = re.compile(r"[\\/]|%2f|%5c", re.IGNORECASE)


def is_attached_template(relationship_type: str) -> bool:
    return relationship_type.endswith("/attachedTemplate")


def scrub_attached_template(xml_bytes: bytes) -> bytes:
    """Reduce the attached template's target to its bare file name."""

    def bare_target(match: re.Match[bytes]) -> bytes:
        target = match.group(2)[1:-1].decode("utf-8", errors="replace")
        file_name = _PATH_SEPARATOR_RE.split(target)[-1]
        return match.group(1) + b'"' + escape(file_name, {'"': "&quot;"}).encode("utf-8") + b'"'

    def relationship(match: re.Match[bytes]) -> bytes:
        tag = match.group(0)
        if not re.search(rb"""\sType\s*=\s*["'][^"']*/attachedTemplate["']""", tag):
            return tag
        return _TARGET_RE.sub(bare_target, tag)

    return _RELATIONSHIP_RE.sub(relationship, xml_bytes)


def attached_template_fields(xml_bytes: bytes) -> list[str]:
    try:
        root = safe_fromstring(xml_bytes)
    except (ElementTree.ParseError, DefusedXmlException):
        return [f"{SETTINGS_RELS_PART}: unreadable"]
    return [
        f"{SETTINGS_RELS_PART}: attached template path"
        for element in root
        if is_attached_template(element.get("Type") or "") and _PATH_SEPARATOR_RE.search(element.get("Target") or "")
    ]


# An external relationship's target is a URL Word wrote for the reader to
# follow, and Word turns every typed email address into a hyperlink whose
# target is ``mailto:<the address>``. Redacting the visible run left that
# target in word/_rels/document.xml.rels. A target that carries an approved
# sample is replaced with a neutral fragment rather than dropped, so the
# r:id that points at it still resolves and the package stays valid.
NEUTRAL_EXTERNAL_TARGET = "#redacted"
_EXTERNAL_MODE_RE = re.compile(rb"""\sTargetMode\s*=\s*["']External["']""")


def scrub_external_targets(xml_bytes: bytes, targets: list[RedactionTarget]) -> bytes:
    """Neutralise every external relationship target that carries an approved sample."""
    matchers = [SampleMatcher.compile(target.text, target.standalone) for target in targets if target.text]
    words = target_word_matchers(targets)

    def neutral_target(match: re.Match[bytes]) -> bytes:
        value = html.unescape(match.group(2)[1:-1].decode("utf-8", errors="replace"))
        joined = FoldedText.of("\0".join(attribute_value_texts(value)))
        if not (any(matcher.found_in(joined) for matcher in matchers) or value_carries(value, words)):
            return match.group(0)
        return match.group(1) + b'"' + NEUTRAL_EXTERNAL_TARGET.encode() + b'"'

    def relationship(match: re.Match[bytes]) -> bytes:
        tag = match.group(0)
        if not _EXTERNAL_MODE_RE.search(tag):
            return tag
        return _TARGET_RE.sub(neutral_target, tag)

    return _RELATIONSHIP_RE.sub(relationship, xml_bytes)


def target_word_matchers(targets: list[RedactionTarget]) -> list[SampleMatcher]:
    """One matcher per distinct target text, for the attribute-value rules (``value_carries``), which ignore ``standalone``."""
    return [SampleMatcher.compile(text) for text in dict.fromkeys(target.text for target in targets if target.text)]


# Text Word keeps in attribute values, which ``redact_xml_part`` never reads:
# an image's alt text (wp:docPr, and the pic:cNvPr Word mirrors it into), a
# hyperlink's tooltip, a bookmark's name and the anchor that points at it, a
# content control's title (w:alias) and tag, and a document variable. Each is
# rewritten on the serialized bytes, inside tags only, so the part's namespace
# prefixes stay as Word wrote them -- mc:Ignorable names them literally.
#
# What a reader sees (alt text, tooltip, content-control title) gets the body's
# own replacement tokens. What only software reads is renamed or emptied: a
# bookmark becomes ``Redacted_<n>`` everywhere it is named, a content-control
# tag is emptied, and a document variable keeps neither its name nor its value.
# Element and attribute are matched by local name, whatever the prefix.
_TEXT_ATTRIBUTES = {
    b"docPr": frozenset({b"descr", b"title", b"name"}),
    b"cNvPr": frozenset({b"descr", b"title", b"name"}),
    b"hyperlink": frozenset({b"tooltip"}),
    b"hlinkClick": frozenset({b"tooltip"}),
    b"hlinkHover": frozenset({b"tooltip"}),
    b"alias": frozenset({b"val"}),
}
_BOOKMARK_ATTRIBUTES = {b"bookmarkStart": b"name", b"hyperlink": b"anchor"}
_CLEARED_ATTRIBUTES = {b"tag": b"val"}
_DOC_VARIABLE = b"docVar"
_SIMPLE_FIELD = b"fldSimple"
_VECTOR_ELEMENTS = frozenset({*_TEXT_ATTRIBUTES, *_BOOKMARK_ATTRIBUTES, *_CLEARED_ATTRIBUTES, _DOC_VARIABLE})
_ELEMENT_RE = re.compile(rb"<([^\s/>]+)")
_ATTRIBUTE_VALUE_RE = re.compile(rb"""(\s)([^\s=/>]+)(\s*=\s*)("[^"]*"|'[^']*')""")
_FIELD_CODE_RE = re.compile(rb"(<(?:[\w.-]+:)?(?:del)?[iI]nstrText\b[^>]*>)([^<]*)")
# A value the matched-text replacement could not clear (it carried the sample
# percent-encoded, say) is replaced whole with this.
WHOLE_VALUE_REPLACEMENT = "[REDACTED]"


def _local(qualified: bytes) -> bytes:
    return qualified.rpartition(b":")[2]


def _attribute_text(quoted: bytes) -> str:
    return html.unescape(quoted[1:-1].decode("utf-8", errors="replace"))


def _quoted(value: str, quote: bytes) -> bytes:
    entities = {'"': "&quot;"} if quote == b'"' else {"'": "&apos;"}
    return quote + escape(value, entities).encode("utf-8") + quote


def bookmark_renames(archive: ZipFile, targets: list[RedactionTarget]) -> dict[str, str]:
    """``{casefolded old name: new name}`` for every bookmark name or anchor that carries an approved target.

    Package-wide, because a hyperlink in a header can point at a bookmark in the
    body: every part renames the same name to the same new one, so each link
    still resolves. Bookmark start/end pairs are joined by ``w:id``, which is
    left alone. A hidden bookmark (leading underscore) stays hidden.
    """
    words = target_word_matchers(targets)
    if not words:
        return {}
    values: list[str] = []
    for name in archive.namelist():
        if not is_word_xml_part(name):
            continue
        for tag in _TAG_RE.findall(archive.read(name)):
            attribute = _BOOKMARK_ATTRIBUTES.get(_local(_ELEMENT_RE.match(tag).group(1)))
            if attribute is None:
                continue
            values.extend(
                _attribute_text(match.group(4))
                for match in _ATTRIBUTE_VALUE_RE.finditer(tag)
                if _local(match.group(2)) == attribute
            )
    taken = {value.casefold() for value in values}
    renames: dict[str, str] = {}
    counter = 0
    for value in values:
        key = value.casefold()
        if key in renames or not value_carries(value, words):
            continue
        while True:
            counter += 1
            renamed = f"{'_' if value.startswith('_') else ''}Redacted_{counter}"
            if renamed.casefold() not in taken:
                break
        renames[key] = renamed
    return renames


def redact_attribute_text(value: str, targets: list[RedactionTarget], style: str, words: list[SampleMatcher]) -> str:
    """An attribute value with every approved target replaced as in the body; whole, if a target still survives."""
    for start, end, replacement in reversed(collect_replacements(value, targets, style)):
        value = value[:start] + replacement + value[end:]
    return WHOLE_VALUE_REPLACEMENT if value_carries(value, words) else value


def redact_attribute_vectors(
    xml_bytes: bytes, targets: list[RedactionTarget], style: str = "placeholder", bookmarks: dict[str, str] | None = None
) -> bytes:
    """Redact the approved targets in the attribute values listed above, and rename bookmark references in field codes."""
    words = target_word_matchers(targets)
    if not words:
        return xml_bytes
    bookmarks = bookmarks or {}
    style = normalize_style(style)
    variables = 0
    # A REF, PAGEREF or HYPERLINK \l field names its bookmark in the field code.
    old_names = (
        re.compile(r"(?<!\w)(?:" + "|".join(sorted(map(re.escape, bookmarks), key=len, reverse=True)) + r")(?!\w)", re.IGNORECASE)
        if bookmarks
        else None
    )

    def rename_bookmarks(text: str) -> str:
        return old_names.sub(lambda name: bookmarks[name.group(0).casefold()], text) if old_names else text

    def tag(match: re.Match[bytes]) -> bytes:
        nonlocal variables
        source = match.group(0)
        element = _local(_ELEMENT_RE.match(source).group(1))
        if element == _SIMPLE_FIELD:
            return _ATTRIBUTE_VALUE_RE.sub(simple_field_code, source)
        if element not in _VECTOR_ELEMENTS:
            return source
        attributes = list(_ATTRIBUTE_VALUE_RE.finditer(source))
        if element == _DOC_VARIABLE:
            if not any(value_carries(_attribute_text(found.group(4)), words) for found in attributes):
                return source
            variables += 1
            neutral = {b"name": f"Redacted_{variables}", b"val": ""}
        else:
            neutral = {}

        def attribute(found: re.Match[bytes]) -> bytes:
            name, quoted = _local(found.group(2)), found.group(4)
            value = _attribute_text(quoted)
            if element == _DOC_VARIABLE and name in neutral:
                value = neutral[name]
            elif name in _TEXT_ATTRIBUTES.get(element, ()):
                value = redact_attribute_text(value, targets, style, words)
            elif name == _BOOKMARK_ATTRIBUTES.get(element):
                value = bookmarks.get(value.casefold(), value)
            elif name == _CLEARED_ATTRIBUTES.get(element) and value_carries(value, words):
                value = ""
            else:
                return found.group(0)
            if value == _attribute_text(quoted):
                return found.group(0)
            return found.group(1) + found.group(2) + found.group(3) + _quoted(value, quoted[:1])

        return _ATTRIBUTE_VALUE_RE.sub(attribute, source)

    def simple_field_code(found: re.Match[bytes]) -> bytes:
        if _local(found.group(2)) != b"instr":
            return found.group(0)
        value = _attribute_text(found.group(4))
        renamed = rename_bookmarks(value)
        if renamed == value:
            return found.group(0)
        return found.group(1) + found.group(2) + found.group(3) + _quoted(renamed, found.group(4)[:1])

    def field_code(match: re.Match[bytes]) -> bytes:
        text = match.group(2).decode("utf-8", errors="replace")
        renamed = rename_bookmarks(text)
        return match.group(0) if renamed == text else match.group(1) + renamed.encode("utf-8")

    redacted = _TAG_RE.sub(tag, xml_bytes)
    if old_names:
        redacted = _FIELD_CODE_RE.sub(field_code, redacted)
    return redacted


# docProps/ parts other than core, app, custom and the thumbnail are no part of
# a Word package's standard, so nothing in them is the document. Like core and
# custom, each is rewritten rather than redacted: an XML part to an empty root,
# anything else to nothing. It is kept, not deleted, so a relationship or a
# content-type override naming it still resolves.
EMPTY_OTHER_PROPERTIES = b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<Properties/>'


def is_other_docprops_part(name: str) -> bool:
    return (
        name.startswith("docProps/")
        and not name.endswith((".rels", "/"))
        and not is_docx_metadata_part(name)
        and not is_docx_thumbnail_part(name)
    )


def scrub_other_docprops_part(name: str) -> bytes:
    return EMPTY_OTHER_PROPERTIES if name.lower().endswith(".xml") else b""


# customXml/item*.xml are data islands: SharePoint and DMS properties (client,
# matter, author) that the review canvas never shows. An item no content control
# binds to is emptied down to its root element; one that is bound mirrors text
# the document shows, so it is redacted with the same targets as the body.
_CUSTOM_XML_ITEM_RE = re.compile(r"^customXml/item\d+\.xml$")
_STORE_ITEM_ID_RE = re.compile(rb"""storeItemID\s*=\s*["']([^"']*)["']""")


def is_custom_xml_item(name: str) -> bool:
    return bool(_CUSTOM_XML_ITEM_RE.match(name))


def bound_custom_xml_items(archive: ZipFile) -> set[str]:
    """The customXml item parts some content control binds to (w:dataBinding)."""
    names = set(archive.namelist())
    bound_ids = {
        store_id.decode("utf-8", errors="replace").strip().upper()
        for name in names
        if is_word_xml_part(name)
        for store_id in _STORE_ITEM_ID_RE.findall(archive.read(name))
    }
    bound = set()
    for name in names:
        if not is_custom_xml_item(name):
            continue
        rels_name = f"customXml/_rels/{Path(name).name}.rels"
        try:
            rels = safe_fromstring(archive.read(rels_name)) if rels_name in names else None
        except (ElementTree.ParseError, DefusedXmlException):
            rels = None
        for relationship in rels if rels is not None else ():
            props_name = f"customXml/{relationship.get('Target') or ''}"
            if not (relationship.get("Type") or "").endswith("/customXmlProps") or props_name not in names:
                continue
            try:
                props = safe_fromstring(archive.read(props_name))
            except (ElementTree.ParseError, DefusedXmlException):
                continue
            item_id = next((value for key, value in props.attrib.items() if local_name(key) == "itemID"), "")
            if item_id.strip().upper() in bound_ids:
                bound.add(name)
    return bound


def scrub_custom_xml_item(
    xml_bytes: bytes, bound: bool, targets: list[RedactionTarget], style: str
) -> bytes:
    try:
        root = safe_fromstring(xml_bytes)
    except (ElementTree.ParseError, DefusedXmlException):
        return b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<empty/>'
    if not bound:
        namespace, _, tag = root.tag[1:].partition("}") if root.tag.startswith("{") else ("", "", root.tag)
        declaration = f' xmlns="{escape(namespace, {chr(34): "&quot;"})}"' if namespace else ""
        return f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<{tag}{declaration}/>'.encode("utf-8")
    changed = False
    for element in root.iter():
        for attribute in ("text", "tail"):
            value = getattr(element, attribute)
            if not value:
                continue
            replacements = collect_replacements(value, targets, style)
            for start, end, replacement in reversed(replacements):
                value = value[:start] + replacement + value[end:]
            if replacements:
                setattr(element, attribute, value)
                changed = True
    return ElementTree.tostring(root, encoding="utf-8", xml_declaration=True) if changed else xml_bytes


def custom_xml_item_fields(name: str, xml_bytes: bytes, bound: bool) -> list[str]:
    """An unbound data island that still holds anything is identifying metadata."""
    if bound:
        return []
    try:
        root = safe_fromstring(xml_bytes)
    except (ElementTree.ParseError, DefusedXmlException):
        return [f"{name}: unreadable"]
    if len(root) or root.attrib or (root.text or "").strip():
        return [f"{name}: unbound data island"]
    return []


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


# Parts whose text is not a w:t stream the redactor rewrites in place, but which
# the export clears outright: word/people.xml (its author and presence user id
# are neutralised by ``scrub_identity_attributes``) and the customXml items
# (emptied, or redacted with the approved targets when a content control binds
# them), and an Office add-in's parts under word/webextensions/ -- the task
# pane list, and each add-in's reference and the settings it writes as
# we:property values -- which hold their strings in attributes, each redacted
# with the approved targets (``scrub_webextension_part``). ``extraction`` reads their text after the text
# parts so detection covers it, and ``anchor_parts`` lays it out the same way so
# a finding's occurrence count agrees between the two.
PEOPLE_PART = "word/people.xml"
WEBEXTENSIONS_PREFIX = "word/webextensions/"
_WEBEXTENSION_PART_RE = re.compile(r"^word/webextensions/(?:taskpanes|webextension\d*)\.xml$")
DOCX_DATA_PART_ORDER = (PEOPLE_PART, "customXml/", WEBEXTENSIONS_PREFIX)


def is_webextension_part(name: str) -> bool:
    return bool(_WEBEXTENSION_PART_RE.match(name))


def is_docx_data_part(name: str) -> bool:
    return name == PEOPLE_PART or is_custom_xml_item(name) or is_webextension_part(name)


def scrub_webextension_part(xml_bytes: bytes, targets: list[RedactionTarget], style: str) -> bytes:
    """An add-in part with every approved target redacted in its attribute values.

    The add-in's settings are JSON-encoded strings in we:property values; the
    rest of the part is the add-in's id, version and task pane layout, all in
    attributes too. Each value is redacted as an alt text is
    (``redact_attribute_text``) -- a placeholder inside a JSON string keeps it a
    JSON string -- on the serialized bytes, so the prefixes stay as Word wrote
    them. Anything this leaves the output scan still refuses.
    """
    words = target_word_matchers(targets)
    if not words:
        return xml_bytes

    def attribute(found: re.Match[bytes]) -> bytes:
        value = _attribute_text(found.group(4))
        redacted = redact_attribute_text(value, targets, style, words)
        if redacted == value:
            return found.group(0)
        return found.group(1) + found.group(2) + found.group(3) + _quoted(redacted, found.group(4)[:1])

    return _TAG_RE.sub(lambda tag: _ATTRIBUTE_VALUE_RE.sub(attribute, tag.group(0)), xml_bytes)


BIBLIOGRAPHY_NS = "http://schemas.openxmlformats.org/officeDocument/2006/bibliography"
BIBLIOGRAPHY_PERSON = f"{{{BIBLIOGRAPHY_NS}}}Person"
BIBLIOGRAPHY_NAME_PARTS = tuple(f"{{{BIBLIOGRAPHY_NS}}}{part}" for part in ("First", "Middle", "Last"))


def docx_data_part_lines(root: ElementTree.Element) -> list[str]:
    """Every string a data part holds, one per line: attribute values, then element text and tail.

    people.xml keeps its people in attributes, customXml items mostly in element
    text; reading both means no string in either is left unread.

    A bibliography ``b:Person`` is one name stored as separate elements,
    surname first. Its name parts are read as one line, given name first --
    the order the person-name rules match, which never join tokens across a
    line break -- so the source's author is detected as the name it is.
    """
    lines = []
    joined = set()
    for element in root.iter():
        lines.extend(element.attrib.values())
        if element in joined:
            if element.tail:
                lines.append(element.tail)
            continue
        lines.extend(value for value in (element.text, element.tail) if value)
        if element.tag == BIBLIOGRAPHY_PERSON:
            parts = sorted(
                (child for child in element if child.tag in BIBLIOGRAPHY_NAME_PARTS and len(child) == 0),
                key=lambda child: BIBLIOGRAPHY_NAME_PARTS.index(child.tag),
            )
            lines.append(" ".join(part.text.strip() for part in parts if part.text))
            joined.update(parts)
    return [line.strip() for line in lines if line.strip()]


def scrub_metadata_part(name: str, xml_bytes: bytes) -> bytes:
    """Return a document-properties part carrying no identifying metadata.

    The part is rewritten, not deleted: dropping it would leave a dangling
    relationship and a content-type override, and export QA fails a package
    that lost parts. core.xml and custom.xml are emptied outright -- every
    field in them is either identifying or a date. app.xml keeps only
    ``APP_PROPERTIES_KEPT``; a part that does not parse is replaced with an
    empty one, because the alternative is shipping it unread.
    """
    if name == CORE_PROPERTIES_PART:
        return EMPTY_CORE_PROPERTIES
    if name == CUSTOM_PROPERTIES_PART:
        return EMPTY_CUSTOM_PROPERTIES
    try:
        root = safe_fromstring(xml_bytes)
    except (ElementTree.ParseError, DefusedXmlException):
        root = None
    kept = []
    for child in root if root is not None else ():
        tag = local_name(child.tag)
        # A kept property is a plain value. One that grew child elements is not
        # the shape this allowlist vouched for, so it goes.
        if tag in APP_PROPERTIES_KEPT and len(child) == 0:
            kept.append(f"<{tag}>{escape(child.text or '')}</{tag}>")
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        f'<Properties xmlns="{APP_PROPERTIES_NS}" '
        'xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes">'
        f"{''.join(kept)}</Properties>"
    ).encode("utf-8")


def identifying_metadata_fields(name: str, xml_bytes: bytes) -> list[str]:
    """Name the identifying fields a document-properties part still carries.

    Returns field names only, never their values: this feeds an export QA
    report, which must not repeat the text it is checking for. A field counts
    when anything in it is non-blank; every custom property counts, because its
    name alone ("Client", "Matter") can be identifying. A part that cannot be
    parsed counts as one unreadable field -- it cannot be shown clean.
    """
    try:
        root = safe_fromstring(xml_bytes)
    except (ElementTree.ParseError, DefusedXmlException):
        return [f"{name}: unreadable"]
    fields = []
    for child in root:
        tag = local_name(child.tag)
        if name == APP_PROPERTIES_PART and tag in APP_PROPERTIES_KEPT and len(child) == 0:
            continue
        if name == CUSTOM_PROPERTIES_PART:
            # The property's own name may be the identifying part, so it is not echoed.
            fields.append(f"{name}: custom property")
        elif "".join(child.itertext()).strip():
            fields.append(f"{name}: {tag}")
    return fields


def metadata_part_text(xml_bytes: bytes) -> str:
    """Every string in a document-properties part, values and custom-property names."""
    try:
        root = safe_fromstring(xml_bytes)
    except (ElementTree.ParseError, DefusedXmlException):
        return xml_bytes.decode("utf-8", errors="replace")
    names = [element.get("name") or "" for element in root.iter()]
    return "\n".join([*root.itertext(), *names])


def redact_docx(
    source_path: str | Path,
    targets: list[RedactionTarget],
    style: str = "placeholder",
    retained: list[RetainedSpan] | tuple[RetainedSpan, ...] = (),
) -> bytes:
    """Return a redacted DOCX while preserving unrelated package parts.

    ``targets`` and ``retained`` carry document-wide anchors (``Occurrence``,
    against the extracted text); ``anchor_parts`` hands each text part the ones
    that land in it. Text no anchor can reach -- attribute values, custom XML,
    a field code, a tracked deletion -- is redacted by the backstop alone.
    """
    source = Path(source_path)
    output = BytesIO()
    normalized_style = normalize_style(style)
    unanchored = [replace(target, found_at=None) for target in targets]

    try:
        with ZipFile(source, "r") as zin, ZipFile(output, "w", ZIP_DEFLATED) as zout:
            bound_items = bound_custom_xml_items(zin)
            bookmarks = bookmark_renames(zin, unanchored)
            anchored = anchor_parts(zin, targets, retained)
            for info in zin.infolist():
                name = info.filename
                data = zin.read(name)
                # Attributes first: a field code naming a renamed bookmark must be
                # renamed before the body redaction rewrites the same field code.
                if is_word_xml_part(name) and targets:
                    data = redact_attribute_vectors(data, unanchored, normalized_style, bookmarks)
                if is_docx_text_part(name) and targets:
                    part_targets, part_retained = anchored.get(name, (unanchored, []))
                    data = redact_xml_part(data, part_targets, normalized_style, part_retained)
                if is_webextension_part(name) and targets:
                    data = scrub_webextension_part(data, unanchored, normalized_style)
                if is_word_xml_part(name):
                    data = scrub_identity_attributes(data)
                elif is_docx_metadata_part(name):
                    data = scrub_metadata_part(name, data)
                elif is_docx_thumbnail_part(name):
                    data = blank_thumbnail(name)
                elif name.endswith(".rels"):
                    if name == SETTINGS_RELS_PART:
                        data = scrub_attached_template(data)
                    if targets:
                        data = scrub_external_targets(data, unanchored)
                elif is_custom_xml_item(name):
                    data = scrub_custom_xml_item(data, name in bound_items, unanchored, normalized_style)
                elif is_other_docprops_part(name):
                    data = scrub_other_docprops_part(name)
                zout.writestr(info, data)
    finally:
        # Matching compiles each approved target into re's module-level cache,
        # where the pattern string is a readable rendering of the target. Drop it
        # as soon as the export is done, including when the export blew up.
        purge_target_pattern_cache()
    return output.getvalue()


def part_streams(archive: ZipFile) -> tuple[list[tuple[str, int, str]], str]:
    """Every part extraction reads, as ``(name, base, text)``, and the document they make.

    The text parts' visible ``w:t`` streams (``build_part_spans``) are laid end
    to end in the order ``extraction`` reads them, followed by the data parts'
    strings (``docx_data_part_lines``) as extraction appends them, so an
    ``Occurrence`` counted against the extracted text resolves against
    ``document`` the same way. ``base`` is where the part starts in it.
    """
    # extraction imports this module, so its part order is imported here.
    from legal_analyzer.extraction import DOCX_TEXT_PART_ORDER, parts_in_reading_order

    names = [name for name in archive.namelist() if not name.endswith("/")]
    streams: list[tuple[str, int, str]] = []
    cursor = 0
    for name in parts_in_reading_order(names, is_docx_text_part, DOCX_TEXT_PART_ORDER):
        try:
            text = "".join(span.text for span in build_part_spans(safe_fromstring(archive.read(name))))
        except (ElementTree.ParseError, DefusedXmlException):
            text = ""
        streams.append((name, cursor, text))
        cursor += len(text) + 2
    for name in parts_in_reading_order(names, is_docx_data_part, DOCX_DATA_PART_ORDER):
        try:
            text = "\n".join(docx_data_part_lines(safe_fromstring(archive.read(name))))
        except (ElementTree.ParseError, DefusedXmlException):
            text = ""
        streams.append((name, cursor, text))
        cursor += len(text) + 2
    return streams, "\n\n".join(text for _, _, text in streams)


def localize_occurrence(
    streams: list[tuple[str, int, str]], document: str, sample: str, found_at: Occurrence | None
) -> tuple[str, Occurrence] | None:
    """The part a document-wide anchor lands in, and the anchor counted within that part.

    None when the anchor does not resolve: the counts disagree, or the match
    straddles two parts.
    """
    located = found_at.locate(document, sample) if found_at else None
    if located is None:
        return None
    for name, base, text in streams:
        if base <= located[0] and located[1] <= base + len(text):
            local = Occurrence.at(text, sample, located[0] - base)
            return (name, local) if local else None
    return None


def anchor_parts(
    archive: ZipFile,
    targets: list[RedactionTarget],
    retained: list[RetainedSpan] | tuple[RetainedSpan, ...] = (),
) -> dict[str, tuple[list[RedactionTarget], list[RetainedSpan]]]:
    """Each text part's targets and retained spans, with the anchors that land in it made local.

    Anchors are counted against the extracted text and resolved through
    ``part_streams``, which lays the parts out the way extraction reads them. A
    target anchored in a data part gets no position in any text part. A target whose
    anchor does not resolve -- the counts disagree, as they do when extraction
    read DrawingML text or was truncated -- keeps no position anywhere and is
    redacted by the backstop alone; a retained span that does not resolve
    holds nothing back. Both are the fail-closed direction.
    """
    streams, document = part_streams(archive)
    target_places = [localize_occurrence(streams, document, target.text, target.found_at) for target in targets]
    retained_places = [localize_occurrence(streams, document, span.text, span.found_at) for span in retained]
    parts: dict[str, tuple[list[RedactionTarget], list[RetainedSpan]]] = {}
    for name, _, _ in streams:
        parts[name] = (
            [
                replace(target, found_at=place[1] if place and place[0] == name else None)
                for target, place in zip(targets, target_places)
            ],
            [
                replace(span, found_at=place[1])
                for span, place in zip(retained, retained_places)
                if place and place[0] == name
            ],
        )
    return parts


def normalize_style(style: str) -> str:
    return style if style in {"placeholder", "mask"} else "placeholder"


def replacement_for(target: RedactionTarget, style: str) -> str:
    if style == "mask":
        return "[REDACTED]"
    return target.replacement


# The shape of every placeholder token a replacement can be: "[REDACTED]",
# "[PERSON_1]", "[PRIVILEGED_CONTENT_3_REDACTED]", "[MANUAL_SENSITIVE_TEXT_REDACTED]".
# The export scan removes from the text it searches only a replacement the
# system generated and this export wrote, and only when it has this shape. A
# reviewer-typed replacement is never removed, bracket-shaped ("[KEMAL_BEY]") or
# not ("Ali Bey"): that choice is made by the caller (blueprints/export.py,
# written_replacements), not by this pattern.
PLACEHOLDER_TOKEN_RE = re.compile(r"\[[A-Z][A-Z0-9_]*\]")


def redact_xml_part(
    xml_bytes: bytes,
    targets: list[RedactionTarget],
    style: str = "placeholder",
    retained: list[RetainedSpan] | tuple[RetainedSpan, ...] = (),
) -> bytes:
    """Redact text in a Word XML part, including matches split across runs.

    The visible runs (w:t) and each kind of text kept outside them (see
    ``REDACTED_TEXT_TAGS``) are matched as separate streams, so a tracked
    deletion or a field code never shifts the offsets of the visible text the
    review canvas resolves against. Anchors (``targets[i].found_at`` and
    ``retained``) are local to this part's ``w:t`` stream (``anchor_parts``),
    so only that stream resolves them; every other stream gets the backstop.
    """
    try:
        root = safe_fromstring(xml_bytes)
    except (ElementTree.ParseError, DefusedXmlException):
        return xml_bytes

    changed = False
    for text_tag in REDACTED_TEXT_TAGS:
        if next(root.iter(text_tag), None) is None:
            continue
        spans = build_part_spans(root, text_tag)
        full_text = "".join(span.text for span in spans)
        if text_tag == TEXT_TAG:
            replacements = collect_replacements(full_text, targets, normalize_style(style), retained)
        else:
            unanchored = [replace(target, found_at=None) for target in targets]
            replacements = collect_replacements(full_text, unanchored, normalize_style(style))
        if replacements:
            write_replacements_back(spans, replacements)
            changed = True
    if not changed:
        return xml_bytes

    ElementTree.register_namespace("w", WORD_NS)
    return ElementTree.tostring(root, encoding="utf-8", xml_declaration=True)


def build_text_spans(text_nodes: list[ElementTree.Element]) -> list[TextSpan]:
    spans: list[TextSpan] = []
    cursor = 0
    for node in text_nodes:
        node_text = node.text or ""
        spans.append(TextSpan(node=node, start=cursor, end=cursor + len(node_text), text=node_text))
        cursor += len(node_text)
    return spans


def build_part_spans(root: ElementTree.Element, text_tag: str = TEXT_TAG) -> list[TextSpan]:
    """Spans over a Word part, laid out the way ``extraction`` reads the same part.

    ``text_tag`` picks the text stream: the visible ``w:t`` runs by default, or
    one of the other ``REDACTED_TEXT_TAGS`` laid out the same way.

    Concatenating the ``w:t`` nodes alone welds the last word of one paragraph
    onto the first word of the next -- "...HAKİMLİĞİNE" + "Dosya No: ..." became
    the single token "HAKİMLİĞİNEDosya". Two things break on that weld. A target
    can match ACROSS it, or fail to match at a boundary the reviewer can see; and
    ``expand_to_word_bounds`` reads the weld as one word, so redacting "Dosya No:
    2024/1187" swallowed "HAKİMLİĞİNE" with it.

    So paragraph and line breaks are carried as SYNTHETIC spans holding a "\n"
    and no node, and tabs as a "\t", exactly where ``extraction.extract_docx_text``
    puts them: it appends a line on entering each ``w:p`` (elements arrive in
    document order, so the separator precedes that paragraph's runs) and on each
    ``w:br``. The result is that this part's matching text and the text a
    reviewer sees in the canvas are the same string, which is the whole point --
    the same resolver run over both then produces the same decisions.

    A synthetic span owns no ``w:t`` node, so ``write_replacements_back`` never
    writes to one: a paragraph break is not a character the document contains and
    must not be replaced away. A redaction covering one keeps it.
    """
    spans: list[TextSpan] = []
    cursor = 0

    def add(node: ElementTree.Element | None, text: str) -> None:
        nonlocal cursor
        spans.append(TextSpan(node=node, start=cursor, end=cursor + len(text), text=text))
        cursor += len(text)

    for node in root.iter():
        if node.tag == text_tag:
            add(node, node.text or "")
        elif node.tag == TAB_TAG:
            add(None, "\t")
        elif node.tag in (BREAK_TAG, PARAGRAPH_TAG):
            add(None, "\n")
    return spans


# Turkish-aware matching of approved target text: case-insensitive, and folding
# the Turkish diacritics onto their plain letters.
#
# An exact substring search ships the identifier: a document that writes a
# company name in caps in a heading and in title case in the body has only the
# exact-case occurrence removed, and the other survives into the export. Both
# obvious fixes are wrong here.
#
# `text.lower().find(...)` corrupts offsets. `'İ'.lower()` is TWO characters
# ('i' + U+0307 COMBINING DOT ABOVE), so lowercasing changes the string length,
# and every index computed on the lowercased copy is wrong for the original as
# soon as an 'İ' appears before the match. `write_replacements_back` slices the
# ORIGINAL Word text nodes with these (start, end) spans, so a shifted offset
# silently blanks the wrong characters and leaves the identifier in place.
# Turkish legal documents are full of 'İ'.
#
# `re.IGNORECASE` happens to work today but is not a contract. CPython's re
# carries an internal case-equivalence table (sre's _equivalences) that, as of
# 3.13, does relate all four of i/I/ı/İ to each other -- measured, not assumed.
# That is wider than the Unicode *simple* case folding the re docs describe,
# under which 'İ' (U+0130) folds only to itself, and it is an undocumented
# implementation detail: nothing in the re documentation promises 'I' will keep
# matching 'ı'. The security property here is worth more than the six lines
# saved, so the i-family equivalence is spelled out below instead of inherited.
#
# So matching is done with an explicit per-character variant class, run over the
# ORIGINAL text, which makes every match offset valid by construction.

# The i-forms are treated as one equivalence class. Turkish pairs I/ı and İ/i;
# English pairs I/i. This codebase handles Turkish *and* English documents, so
# restricting 'I' to 'ı' alone would miss the English spelling of the same
# entity. The circumflex 'î' joins them rather than forming a group of its own:
# it marks vowel length on the same letter ("hâkim", "Kâzım", "mahkûm") and is
# dropped in running text exactly as freely as the dots are.
TURKISH_I_FORMS = frozenset("iIıİîÎ")

# The same argument extends to the Turkish diacritics. Turkish is routinely typed
# without them -- on ASCII keyboards, in systems that mangle them, in filings that
# simply do not bother -- so the same company appears as "ŞİRKETİ" in the heading
# and "SIRKETI" in the body. Matching only the exact glyphs redacts one and ships
# the other, which is the same bypass as the case one and, in real filings,
# probably the more common of the two. Each group holds every form in both cases
# -- the plain letter and each diacritic variant of it -- so folding and case
# compose without any extra logic: 'Ş' matches s, S, ş and Ş.
#
# This DELIBERATELY over-matches: "Kadir" also matches "Kadır", "acik" also
# matches "açık". That is accepted, not a defect to fix. Redaction is asymmetric
# -- over-redacting removes a word that did not need removing, under-redacting
# publishes an identifier -- so when the two spellings cannot be told apart, the
# tool errs toward removal. Narrowing any of these groups reopens a bypass.
#
# The circumflex vowels â/î/û ("Kâzım", "hâkim", "mahkûm") each join the group of
# their BASE letter instead of getting a group of their own. A separate
# frozenset("uUûÛ") reads as the obvious addition and silently breaks ü: the
# lookup table below is built last-group-wins, so 'u' and 'U' would stop
# resolving to the group holding 'Ü', and the diacritic-free target "unal" would
# no longer match "ÜNAL" -- reopening the bypass in the direction the tests
# happen not to enumerate. test_fold_groups_are_pairwise_disjoint pins this.
TURKISH_FOLD_GROUPS = (
    frozenset("aAâÂ"),
    frozenset("cCçÇ"),
    frozenset("gGğĞ"),
    TURKISH_I_FORMS,
    frozenset("oOöÖ"),
    frozenset("sSşŞ"),
    frozenset("uUüÜûÛ"),
)
TURKISH_FOLD_BY_CHAR = {char: group for group in TURKISH_FOLD_GROUPS for char in group}


def case_variants(char: str) -> tuple[str, ...]:
    """Return every single-character case and Turkish-fold variant of ``char``."""
    group = TURKISH_FOLD_BY_CHAR.get(char)
    if group is not None:
        return tuple(sorted(group))
    # str.upper()/str.lower() can widen a character ('ß'.upper() == 'SS',
    # 'İ'.lower() == 'i̇'); only single characters belong in a class.
    return tuple(sorted({char} | {form for form in (char.upper(), char.lower()) if len(form) == 1}))


def case_insensitive_pattern(text: str, standalone: bool = False) -> re.Pattern[str]:
    """Compile ``text`` into a literal matcher ignoring case and Turkish diacritics.

    ``standalone`` narrows it to whole words starting with a capital letter
    (see ``is_standalone_sample``).

    This is NOT uncached, and an earlier version of this docstring claimed it
    was. ``re.compile`` goes through ``re._compile``, which stores the result in
    the module-level ``re._cache`` and ``re._cache2`` dicts keyed by the pattern
    STRING. That key is the approved target spelled out as character classes --
    ``[Aa][Rr][Dd][Iiİı][CcÇç][ ][Tt]...`` -- from which the target is trivially
    recoverable, and with ``re._MAXCACHE`` at 512 it would sit there for the life
    of the interpreter. Measured on CPython 3.13, not assumed: after
    ``re.purge()``, one call here takes ``len(re._cache)`` from 0 to 1.

    So callers that compile approved-target or finding-sample text must call
    ``purge_target_pattern_cache()`` once the export or QA pass is done. The
    entry points in this package (``redact_docx``, ``analyze_docx_export_quality``,
    ``analyze_pdf_redaction_quality``) already do.
    """
    parts: list[str] = []
    for index, char in enumerate(text):
        variants = case_variants(char)
        if standalone and index == 0:
            variants = tuple(variant for variant in variants if variant.isupper()) or variants
        if len(variants) == 1:
            parts.append(re.escape(variants[0]))
        else:
            parts.append("[" + "".join(re.escape(variant) for variant in variants) + "]")
    pattern = "".join(parts)
    return re.compile(rf"(?<!\w){pattern}(?!\w)" if standalone else pattern)


def find_case_insensitive(text: str, needle: str, standalone: bool = False) -> list[tuple[int, int]]:
    """Return non-overlapping ``(start, end)`` spans of ``needle``, indexed into ``text``.

    ``standalone`` matches only whole words whose first letter is a capital.
    """
    if not needle:
        return []
    return [(match.start(), match.end()) for match in case_insensitive_pattern(needle, standalone).finditer(text)]


def contains_case_insensitive(text: str, needle: str, standalone: bool = False) -> bool:
    return bool(needle) and case_insensitive_pattern(needle, standalone).search(text) is not None


_NEVER_MATCHES = re.compile(r"(?!)")


@dataclass(frozen=True)
class FoldedText:
    """A text prepared once for ``SampleMatcher``: whitespace-normalised, and tr_fold-ed."""

    text: str
    folded: str

    @classmethod
    def of(cls, text: str) -> "FoldedText":
        normalized = " ".join(text.split())
        return cls(normalized, tr_fold(normalized))


@dataclass(frozen=True)
class SampleMatcher:
    """Whether an approved sample survives in a text: the rule the export scan applies.

    Whitespace-normalised on both sides, so a line break, a tab or a no-break
    space inside the surviving text does not hide it. A standalone sample (see
    ``is_standalone_sample``) is the redactor's own capitalised whole-word
    pattern and nothing else. Every other sample matches through either fold
    already used here: the redactor's pattern (case, the Turkish i-family and
    the diacritics), or ``turkish_names.tr_fold`` equality, the case fold
    detection uses. Neither is a subset of the other -- tr_fold alone misses
    "DENIZ" for "Deniz" (ASCII I folds to dotless ı), the pattern alone misses
    a casefold-only equivalence such as the "ﬁ" ligature PDF text extraction
    emits -- and a check that exists to fail closed takes both.

    Compile once and match many texts; callers purge the pattern cache after
    (see ``case_insensitive_pattern``).
    """

    pattern: re.Pattern[str]
    folded: str | None

    @classmethod
    def compile(cls, sample: str, standalone: bool = False) -> "SampleMatcher":
        normalized = " ".join(sample.split())
        if not normalized:
            return cls(_NEVER_MATCHES, None)
        return cls(
            case_insensitive_pattern(normalized, standalone),
            None if standalone else tr_fold(normalized),
        )

    def found_in(self, text: FoldedText) -> bool:
        if self.pattern.search(text.text):
            return True
        return bool(self.folded) and self.folded in text.folded

    def found_as_word_in(self, text: FoldedText) -> bool:
        """``found_in``, but only where no letter or digit stands beside the match: the rule for an XML attribute value.

        Compile the matcher without ``standalone``: an attribute value is matched
        case-insensitively. Both the export scan and ``scrub_external_targets``
        decide on this, so a target the scan would flag is one the scrubber
        neutralises.
        """
        for haystack, find in (
            (text.text, lambda start: _match_span(self.pattern.search(text.text, start))),
            (text.folded, lambda start: _find_span(text.folded, self.folded, start)),
        ):
            start = 0
            while (span := find(start)) is not None:
                begin, end = span
                if (begin == 0 or not haystack[begin - 1].isalnum()) and (end == len(haystack) or not haystack[end].isalnum()):
                    return True
                start = begin + 1
        return False

    def found_as_component_in(self, text: "ComponentText") -> bool:
        """``found_as_word_in``, where a camelCase boundary also bounds a word: the second attribute-value rule.

        A name glued into an identifier with nothing between -- "elaYilmaz",
        "AyseYilmaz" -- has no non-letter beside it, so the whole-word rule
        cannot see it. Here a lowercase letter followed by a capital is a
        boundary too, and a match counts when it covers a CAPITALISED component:
        "Yilmaz" in "elaYilmaz", "Ayse" in "AyseYilmaz", "Ela Yılmaz" across
        "elaYilmaz". A lowercase leading component alone does not ("light" in
        Word's "lightGray"), because every OOXML identifier starts with one.
        """
        for haystack, find, origin in (
            (text.text, lambda start: _match_span(self.pattern.search(text.text, start)), None),
            (text.folded, lambda start: _find_span(text.folded, self.folded, start), text.origin),
        ):
            start = 0
            while (span := find(start)) is not None:
                begin, end = span
                if origin is not None:
                    # A folded character maps back to the one it came from: casefold can widen ("ß" -> "ss").
                    begin, end = origin[begin], origin[end - 1] + 1
                if text.is_component_span(begin, end):
                    return True
                start = span[0] + 1
        return False


@dataclass(frozen=True)
class ComponentText:
    """Attribute values with a space inserted at each camelCase boundary, for ``found_as_component_in``.

    Only values that have such a boundary are kept: the rest are already read
    whole-word. Values are joined by a NUL, as in the export scan.
    """

    text: str
    folded: str
    # For each character of ``folded``, the index in ``text`` it was folded from.
    origin: tuple[int, ...]
    # The indexes in ``text`` of the inserted spaces.
    inserted: frozenset[int]

    @classmethod
    def of(cls, values) -> "ComponentText":
        pieces: list[str] = []
        inserted: set[int] = set()
        cursor = 0
        for value in values:
            normalized = " ".join(value.split())
            if not _has_camel_boundary(normalized):
                continue
            if pieces:
                pieces.append("\0")
                cursor += 1
            for index, char in enumerate(normalized):
                if index and normalized[index - 1].islower() and char.isupper():
                    pieces.append(" ")
                    inserted.add(cursor)
                    cursor += 1
                pieces.append(char)
                cursor += 1
        text = "".join(pieces)
        folded: list[str] = []
        origin: list[int] = []
        for index, char in enumerate(text):
            piece = tr_fold(char)
            folded.append(piece)
            origin.extend([index] * len(piece))
        return cls(text, "".join(folded), tuple(origin), frozenset(inserted))

    def is_component_span(self, begin: int, end: int) -> bool:
        text = self.text
        if (begin > 0 and text[begin - 1].isalnum()) or (end < len(text) and text[end].isalnum()):
            return False
        spans_components = any(index in self.inserted for index in range(begin, end))
        leading_lowercase_alone = (
            end in self.inserted and begin - 1 not in self.inserted and not spans_components and not text[begin].isupper()
        )
        return not leading_lowercase_alone


def _has_camel_boundary(value: str) -> bool:
    return any(value[index - 1].islower() and value[index].isupper() for index in range(1, len(value)))


def value_carries(value: str, words: list[SampleMatcher]) -> bool:
    """Whether an XML attribute value carries an approved sample, by the export scan's attribute rules.

    ``words`` are matchers compiled without ``standalone``. The two rules are
    whole-word (``found_as_word_in``) and capitalised camelCase component
    (``found_as_component_in``), each over the value as written,
    percent-decoded and with underscores as spaces. Every attribute the
    redactor rewrites decides on this, so what it leaves is what the scan
    passes.
    """
    variants = attribute_value_texts(value)
    whole = FoldedText.of("\0".join(variants))
    components = ComponentText.of(variants)
    return any(word.found_as_word_in(whole) or word.found_as_component_in(components) for word in words)


def _match_span(match: re.Match[str] | None) -> tuple[int, int] | None:
    return match.span() if match else None


def _find_span(text: str, sample: str | None, start: int) -> tuple[int, int] | None:
    if not sample:
        return None
    begin = text.find(sample, start)
    return None if begin < 0 else (begin, begin + len(sample))


def attribute_value_texts(value: str) -> tuple[str, str, str]:
    """An XML attribute value as matched: as written, percent-decoded, and with underscores as spaces.

    A bookmark name cannot hold a space, so Word writes the one it was given
    with underscores ("Deniz_Kurgusal").
    """
    return value, unquote(value), value.replace("_", " ")


def purge_target_pattern_cache() -> None:
    """Drop compiled approved-target patterns out of ``re``'s module-level cache.

    ``case_insensitive_pattern`` leaves the target text, spelled as character
    classes, in ``re._cache``/``re._cache2`` as a dict KEY -- readable by anything
    that can read this process. ``re.purge()`` is the documented way to empty
    both, and it empties them for the whole process, not just for us.

    That is a cost, not a correctness problem. Compiled patterns held by
    reference (module-level ``re.compile`` results) are untouched. Call sites that
    pass a pattern STRING and lean on the cache -- privacy.py runs its 25
    DETECTION_RULES that way -- recompile once on their next call: measured at
    4.2ms against 1.8ms warm for one analyze_privacy() pass, once per export.
    An approved identifier sitting in a process-wide dict for the life of the
    interpreter is worth more than that.
    """
    re.purge()


_WORD_CHAR_RE = re.compile(r"\w", re.UNICODE)


def _is_word_char(char: str) -> bool:
    return _WORD_CHAR_RE.match(char) is not None


def expand_to_word_bounds(text: str, start: int, end: int) -> tuple[int, int]:
    """Widen ``(start, end)`` so it never cuts a word in half.

    A target is matched as a bare substring, so it can land in the MIDDLE of a
    word. Turkish is agglutinative and makes that the normal case rather than a
    corner: the party role "davalı" is a literal prefix of "davalıya" and
    "davalıdan", so replacing only the matched characters shipped
    "[PARTY_ROLE_2]ya tebliğ edilmiştir" -- a placeholder welded to an orphaned
    inflectional suffix, which is not text that survives a filing.

    Widening rather than requiring a word boundary is the deliberate choice.
    Requiring a boundary would simply stop redacting "davalıya", and under-
    redaction is the one outcome that must not happen: it is a privacy leak,
    while over-redaction costs a reviewer legibility. Widening also covers the
    other way a match lands mid-word -- a target that is a PREFIX OF A LONGER
    IDENTIFIER ("4561237896" inside "45612378965") would otherwise leave the
    remaining digits in cleartext, and neither export QA check can see that,
    because both look for the whole sample and a surviving fragment is not it.

    The cost, stated: the Turkish case suffix is consumed with the word, so the
    output reads "[PARTY_ROLE_2] tebliğ edilmiştir" rather than
    "[PARTY_ROLE_2]'ya tebliğ edilmiştir". Nothing is invented to replace it --
    writing an apostrophe the source never had would be fabricating text. Where
    the source DOES use the apostrophe Turkish orthography wants for a proper
    noun ("Karaduman'a"), no widening happens at all: an apostrophe is not a
    word character, so the match already ends on a boundary and the suffix is
    left alone.

    Only a run of word characters is crossed, so widening can never reach past
    a space, a period or a bracket into a neighbouring word.
    """
    if start < len(text) and _is_word_char(text[start]):
        while start > 0 and _is_word_char(text[start - 1]):
            start -= 1
    if end > 0 and end <= len(text) and _is_word_char(text[end - 1]):
        while end < len(text) and _is_word_char(text[end]):
            end += 1
    return start, end


# The two ways an approved finding reaches a character, in the order a tie
# between them is broken: its own position wins over a backstop match of the
# same width.
_OWN, _BACKSTOP = 0, 1


def resolve_redaction(
    text: str,
    targets: list[RedactionTarget],
    style: str = "placeholder",
    retained: list[RetainedSpan] | tuple[RetainedSpan, ...] = (),
) -> Resolution:
    """THE post-decision redaction rule, shared by every rendering of a document.

    The browser review canvas and the exported reviewed DOCX both resolve their
    text through this one function, so that the document a lawyer approves is
    the document they receive. Before it existed the canvas replaced each
    finding's recorded OFFSETS while the export replaced every STRING match of
    each approved sample, and they disagreed on the same document with the same
    review decisions -- 5 differing hunks over 27 lines on one petition. (A
    third rendering, the "Preview TXT / Preview DOCX" server preview, has since
    been deleted outright rather than reconciled: it was a rebuilt artifact
    named and headed like the deliverable, and before any review decision its
    body was the unredacted source.)

    An approved finding reaches the text two ways, and each is named here:

    1. Its OWN position -- ``target.found_at`` resolved against this text (see
       ``Occurrence``). That is the place it was found, so it is redacted
       whatever the rest of the document holds, under the finding's own
       placeholder; a tie with a backstop match of the same width goes to it.
       Two bare-surname mentions "Kartal" belonging to two different people
       used to take one placeholder between them, the first target listed.
    2. The BACKSTOP -- every other match of its sample: case- and Turkish-
       folded, a capitalised whole word for a bare surname
       (``is_standalone_sample``), widened to word bounds. It exists for the
       repeats detection missed ("EKİN KARTAL", "ekin kartal" after a detected
       "Ekin Kartal") and it is deliberately as wide as the string matching
       that preceded positions, so nothing is redacted less than before.

    The backstop yields in ONE place: a match lying wholly inside a finding the
    reviewer RETAINED (``retained``) is left as retained, because that span's
    text was decided on its own -- "Kartal" the district inside a retained
    "Ankara Kartal 3. Asliye Hukuk" court is not the approved bare surname
    "Kartal" elsewhere. A dismissed finding does not count: "not sensitive as an
    address" is not "keep every word in it". Such a match is reported in
    ``held`` when nothing else covers it; the reviewed-DOCX export refuses while
    any is held (``app.docx_export_conditions``), and the independent output
    scan would refuse the file anyway, so a held match is shown in the canvas
    and never shipped.

    Regions are sorted, disjoint ``(start, end, replacement, target_index)``
    spans. ``target_index`` indexes ``targets`` and lets a caller attribute a
    region back to the finding that produced it; ``collect_replacements`` drops
    it. ``expand_to_word_bounds`` applies to both kinds.
    """
    retained_spans = [span.found_at.locate(text, span.text) for span in retained]
    matches: list[tuple[int, int, int, int, str, int]] = []
    candidates: list[HeldMatch] = []
    for index, target in enumerate(targets):
        if not target.text:
            continue
        replacement = replacement_for(target, style)
        own = target.found_at.locate(text, target.text) if target.found_at else None
        if own is not None:
            start, end = expand_to_word_bounds(text, *own)
            matches.append((start, end, own[1] - own[0], _OWN, replacement, index))
        for raw_start, raw_end in find_case_insensitive(text, target.text, target.standalone):
            if (raw_start, raw_end) == own:
                continue
            start, end = expand_to_word_bounds(text, raw_start, raw_end)
            holder = next(
                (
                    held_by
                    for held_by, span in enumerate(retained_spans)
                    if span is not None and span[0] <= start and end <= span[1]
                ),
                None,
            )
            if holder is not None:
                candidates.append(HeldMatch(start, end, index, holder))
                continue
            matches.append((start, end, raw_end - raw_start, _BACKSTOP, replacement, index))

    # Widest span wins a tie at the same start, then the widest span BEFORE
    # word-boundary widening, then a finding's own position over a backstop
    # match, then the earliest target. The second term is what keeps
    # attribution honest: a MERSIS number "0123456789012345" and a VKN
    # "1234567890" nested one digit inside it widen to exactly the same span,
    # and without it whichever target happened to be listed first supplied the
    # placeholder -- the same characters would leave, labelled [VKN_1] instead
    # of [MERSIS_1]. Widening is a text-fidelity adjustment, not evidence that a
    # target covered more of the document. The last term only removes the
    # residual dependence on list order.
    selected: list[tuple[int, int, str, int]] = []
    cursor = 0
    for start, end, raw_width, kind, replacement, index in sorted(
        matches,
        key=lambda item: (item[0], -(item[1] - item[0]), -item[2], item[3], item[5]),
    ):
        if end <= cursor:
            continue
        selected.append((max(start, cursor), end, replacement, index))
        cursor = end
    held = [match for match in candidates if not _covered(match.start, match.end, selected)]
    return Resolution(selected, held)


def _covered(start: int, end: int, regions: list[tuple[int, int, str, int]]) -> bool:
    """Whether sorted, disjoint ``regions`` cover every character of ``[start, end)``."""
    cursor = start
    for region_start, region_end, _, _ in regions:
        if region_end <= cursor:
            continue
        if region_start > cursor:
            return False
        cursor = region_end
        if cursor >= end:
            return True
    return cursor >= end


def resolve_redaction_regions(
    text: str,
    targets: list[RedactionTarget],
    style: str = "placeholder",
    retained: list[RetainedSpan] | tuple[RetainedSpan, ...] = (),
) -> list[tuple[int, int, str, int]]:
    """The regions of ``resolve_redaction``: what every rendering replaces."""
    return resolve_redaction(text, targets, style, retained).regions


def collect_replacements(
    text: str,
    targets: list[RedactionTarget],
    style: str = "placeholder",
    retained: list[RetainedSpan] | tuple[RetainedSpan, ...] = (),
) -> list[tuple[int, int, str]]:
    """Return sorted, disjoint ``(start, end, replacement)`` spans covering every match.

    Overlapping matches are CLIPPED, not dropped. Two approved targets really do
    overlap once diacritics fold: in "Yuklenici ACIK KADIR YILMAZ imzaladi.",
    both "AÇIK KADIR" and "KADIR YILMAZ" match, and the earlier-starting one wins
    the sort. Dropping the loser wholesale shipped the part of it the winner does
    not cover -- the surname survived in cleartext, and neither export QA check
    can see that, because both look for the WHOLE sample string and a surviving
    fragment is not it.

    So a match that starts inside the region already selected but ends past it
    still contributes its residual ``(cursor, end)``. A match that ends inside the
    selected region contributes nothing, which is correct: every character of it
    is already being replaced. The result stays sorted and non-overlapping, which
    is what write_replacements_back needs in order to slice the original Word
    text nodes with these offsets.

    The selection itself lives in ``resolve_redaction``, which the browser
    canvas resolves through as well (``redaction_plan.plan_segments``); this
    wrapper only drops the target index it uses for attribution. Keeping one
    implementation is the point -- the export and the canvas disagreeing about
    which characters go is exactly the defect that function exists to close.
    """
    return [
        (start, end, replacement)
        for start, end, replacement, _ in resolve_redaction_regions(text, targets, style, retained)
    ]


def write_replacements_back(spans: list[TextSpan], replacements: list[tuple[int, int, str]]) -> None:
    """Apply replacements to their original Word text nodes without flattening the part.

    Each replacement's text is written exactly once, by the first span carrying a
    real ``w:t`` node that it intersects. That indirection exists because
    ``build_part_spans`` interleaves synthetic paragraph/tab spans owning no
    node: a region starting on one -- or on a node that a preceding region has
    already consumed -- would otherwise have had its placeholder dropped while
    its characters were still removed, which is silent UNDER-redaction with no
    visible marker left behind.
    """
    emitted: set[int] = set()
    for span in spans:
        if span.node is None:
            continue
        if span.end <= span.start and not span.text:
            continue

        intersecting = [
            (index, replacement)
            for index, replacement in enumerate(replacements)
            if replacement[0] < span.end and replacement[1] > span.start
        ]
        if not intersecting:
            continue

        chunks: list[str] = []
        cursor = 0
        for index, (start, end, replacement) in intersecting:
            local_start = max(start - span.start, 0)
            local_end = min(end - span.start, len(span.text))
            if local_start < cursor:
                local_start = cursor
            chunks.append(span.text[cursor:local_start])
            if index not in emitted:
                chunks.append(replacement)
                emitted.add(index)
            cursor = max(cursor, local_end)
        chunks.append(span.text[cursor:])
        span.node.text = "".join(chunks)
        preserve_space_if_needed(span.node)


def preserve_space_if_needed(node: ElementTree.Element) -> None:
    text = node.text or ""
    if text and (text[0].isspace() or text[-1].isspace()):
        node.set(XML_SPACE_ATTR, "preserve")
