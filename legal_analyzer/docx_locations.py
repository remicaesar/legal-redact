"""Where each finding sits in a DOCX, in terms Word's own search can reach.

The Word add-in's panel jumps to a finding by searching one story of the open
document (the body, a header or footer, the footnotes or the endnotes) and
selecting the ``index``-th of its ``count`` hits. Text inside a comment cannot be
selected, only the comment's anchor, so a finding in a comment names the
comment (``comment_id``) and counts its occurrence within that comment alone.
A finding carries only offsets into the extracted text, which joins every part,
so the panel cannot tell which story a finding is in, or which of several
identical texts it is.

This resolves that with the export's own anchoring (``docx_redactor.part_streams``
and ``localize_occurrence``, the functions ``anchor_parts`` uses), so the panel
selects the occurrence the reviewed export redacts as that finding's own. It is
navigation only: nothing here decides what is redacted, and a finding this cannot
place is still redacted by the export's backstop.
"""

from __future__ import annotations

from pathlib import Path
from xml.etree import ElementTree
from zipfile import BadZipFile, ZipFile

from defusedxml.common import DefusedXmlException
from defusedxml.ElementTree import fromstring as safe_fromstring

from legal_analyzer.docx_redactor import (
    GLOSSARY_PREFIX,
    WORD_NS,
    Occurrence,
    build_part_spans,
    is_docx_data_part,
    is_docx_text_part,
    localize_occurrence,
    part_streams,
)
from legal_analyzer.extraction import read_capped_member

RELATIONSHIPS_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PACKAGE_RELATIONSHIPS_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
DOCUMENT_RELS_PART = "word/_rels/document.xml.rels"
COMMENTS_PART = "word/comments.xml"

# Word.HeaderFooterType, keyed by the w:type of a w:headerReference/w:footerReference.
HEADER_FOOTER_TYPES = {"default": "Primary", "first": "FirstPage", "even": "EvenPages"}

# Word's search takes at most 255 characters, and gives "^" a meaning of its
# own even with wildcards off ("^p", "^t"). A sample the panel would have to
# rewrite to search for is reported as not searchable instead.
WORD_SEARCH_MAX_CHARS = 255


def story_for_part(name: str) -> str | None:
    """The story of the open document a part's text is shown in, or None."""
    if name.startswith(GLOSSARY_PREFIX):
        return "glossary"
    if is_docx_data_part(name):
        return "data"
    if name == "word/document.xml":
        return "body"
    if name.startswith("word/header"):
        return "header"
    if name.startswith("word/footer"):
        return "footer"
    return {
        "word/footnotes.xml": "footnotes",
        "word/endnotes.xml": "endnotes",
        COMMENTS_PART: "comments",
    }.get(name)


def _relationship_targets(archive: ZipFile) -> dict[str, str]:
    """``r:id`` -> part name, for the main document's relationships."""
    try:
        root = safe_fromstring(read_capped_member(archive, DOCUMENT_RELS_PART))
    except (KeyError, ElementTree.ParseError, DefusedXmlException):
        return {}
    targets: dict[str, str] = {}
    for relationship in root.iter(f"{{{PACKAGE_RELATIONSHIPS_NS}}}Relationship"):
        rel_id, target = relationship.get("Id"), relationship.get("Target") or ""
        if not rel_id or relationship.get("TargetMode") == "External":
            continue
        targets[rel_id] = target.lstrip("/") if target.startswith("/") else f"word/{target}"
    return targets


def header_footer_references(archive: ZipFile) -> dict[str, tuple[int, str]]:
    """Each header/footer part -> the first (section index, Word.HeaderFooterType) showing it.

    Sections are the section breaks in document order -- a paragraph's
    ``w:pPr/w:sectPr`` and the body's own ``w:sectPr`` -- which is the order of
    Word's ``body.sections``. The ``w:sectPr`` a tracked layout change keeps
    inside ``w:sectPrChange`` is the old properties of a section, not one. A
    section with no reference of its own shows the previous section's part,
    which is reachable through that earlier section, so the first reference is
    enough. A part no section references is
    absent: the panel cannot reach it.
    """
    targets = _relationship_targets(archive)
    try:
        root = safe_fromstring(read_capped_member(archive, "word/document.xml"))
    except (KeyError, ElementTree.ParseError, DefusedXmlException):
        return {}
    parents = {child: parent for parent in root.iter() for child in parent}
    breaks = [
        sect_pr
        for sect_pr in root.iter(f"{{{WORD_NS}}}sectPr")
        if parents[sect_pr].tag in (f"{{{WORD_NS}}}pPr", f"{{{WORD_NS}}}body")
    ]
    references: dict[str, tuple[int, str]] = {}
    for section, sect_pr in enumerate(breaks):
        for tag in ("headerReference", "footerReference"):
            for reference in sect_pr.findall(f"{{{WORD_NS}}}{tag}"):
                part = targets.get(reference.get(f"{{{RELATIONSHIPS_NS}}}id") or "")
                kind = HEADER_FOOTER_TYPES.get(reference.get(f"{{{WORD_NS}}}type") or "default")
                if part and kind and part not in references:
                    references[part] = (section, kind)
    return references


def comment_streams(archive: ZipFile) -> list[tuple[int | None, int, str]]:
    """Each comment in ``word/comments.xml`` as ``(w:id, base, text)``.

    ``build_part_spans`` lays a part out in document order, so each comment's
    text is a contiguous slice of the part's, starting at ``base``.
    """
    try:
        root = safe_fromstring(read_capped_member(archive, COMMENTS_PART))
    except (KeyError, ElementTree.ParseError, DefusedXmlException):
        return []
    comments = []
    cursor = 0
    for comment in root.iter(f"{{{WORD_NS}}}comment"):
        text = "".join(span.text for span in build_part_spans(comment))
        raw_id = comment.get(f"{{{WORD_NS}}}id") or ""
        comments.append((int(raw_id) if raw_id.isdigit() else None, cursor, text))
        cursor += len(text)
    return comments


def _in_comment(
    comments: list[tuple[int | None, int, str]], part_text: str, sample: str, occurrence: Occurrence
) -> tuple[int | None, Occurrence | None]:
    """The comment a part-local occurrence lands in, and the occurrence counted within it."""
    located = occurrence.locate(part_text, sample)
    if located is None:
        return None, None
    for comment_id, base, text in comments:
        if base <= located[0] and located[1] <= base + len(text):
            return comment_id, Occurrence.at(text, sample, located[0] - base)
    return None, None


def _checked_within_cap(archive: ZipFile) -> None:
    """Raise ValueError if any part this module reads decompresses past the intake cap."""
    for name in archive.namelist():
        if is_docx_text_part(name) or is_docx_data_part(name) or name == DOCUMENT_RELS_PART:
            read_capped_member(archive, name)


def searchable_in_word(sample: str) -> bool:
    return 0 < len(sample) <= WORD_SEARCH_MAX_CHARS and not any(char in sample for char in "\t\n\r^")


def _unplaced(finding_id: int, searchable: bool) -> dict:
    return {
        "finding_id": finding_id,
        "story": None,
        "part": None,
        "section": None,
        "header_footer_type": None,
        "index": None,
        "count": None,
        "comment_id": None,
        "searchable": searchable,
    }


def finding_locations(source_path: str | Path, text: str, findings: list[dict]) -> list[dict]:
    """Each finding's story and occurrence in the DOCX at ``source_path``.

    ``text`` is the document's source text (``app.document_source_text``) that
    the findings' offsets were recorded against. A finding is placed only where
    its offset still frames its sample there and that occurrence resolves into
    one part (``localize_occurrence``); otherwise every location field is None
    and the panel falls back to showing all matches. A finding in a comment is
    not searchable; it carries the comment's ``w:id`` and its occurrence within
    that comment. A part past the intake cap places nothing. Only ids, part
    names and numbers are returned, never text.
    """
    try:
        with ZipFile(source_path) as archive:
            _checked_within_cap(archive)
            streams, document = part_streams(archive)
            references = header_footer_references(archive)
            comments = comment_streams(archive)
    except (OSError, BadZipFile, ValueError):
        return [_unplaced(finding["id"], searchable_in_word(finding.get("sample") or "")) for finding in findings]

    locations = []
    for finding in findings:
        sample = finding.get("sample") or ""
        searchable = searchable_in_word(sample)
        found_at = Occurrence.at(text, sample, finding.get("start_offset"))
        place = localize_occurrence(streams, document, sample, found_at)
        if place is None:
            locations.append(_unplaced(finding["id"], searchable))
            continue
        part, occurrence = place
        story = story_for_part(part)
        section, header_footer_type = references.get(part, (None, None))
        comment_id = None
        if story == "comments":
            part_text = next(text for name, _, text in streams if name == part)
            comment_id, occurrence = _in_comment(comments, part_text, sample, occurrence)
        reachable = story in {"body", "footnotes", "endnotes"} or (
            story in {"header", "footer"} and section is not None
        )
        locations.append(
            {
                "finding_id": finding["id"],
                "story": story,
                "part": part,
                "section": section,
                "header_footer_type": header_footer_type,
                "index": occurrence.index if occurrence else None,
                "count": occurrence.count if occurrence else None,
                "comment_id": comment_id,
                "searchable": searchable and reachable,
            }
        )
    return locations
